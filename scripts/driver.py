"""Drive the MicroVM agent from a laptop: launch, chat, sleep, wake, measure.

Subcommands:
  run        launch a MicroVM from the image, wait for RUNNING, print timings
  chat       send a message: driver.py chat <microvm-id> "hello"
  health     driver.py health <microvm-id>
  suspend    driver.py suspend <microvm-id>  (explicit sleep)
  resume     driver.py resume <microvm-id>   (explicit wake, timed)
  wake-test  driver.py wake-test <microvm-id> "message"
             sends traffic to a suspended VM and measures auto-resume latency
  terminate  driver.py terminate <microvm-id>
"""

import json
import sys
import time

import boto3
import requests

REGION = "us-east-1"
IMAGE_NAME = "agent-that-sleeps"

mvm = boto3.client("lambda-microvms", region_name=REGION)


def endpoint_and_token(microvm_id: str) -> tuple[str, str]:
    endpoint = mvm.get_microvm(microvmIdentifier=microvm_id)["endpoint"]
    token = mvm.create_microvm_auth_token(
        microvmIdentifier=microvm_id,
        expirationInMinutes=30,
        allowedPorts=[{"allPorts": {}}],
    )["authToken"]["X-aws-proxy-auth"]
    return endpoint, token


def request(microvm_id: str, method: str, path: str, body: dict | None = None):
    endpoint, token = endpoint_and_token(microvm_id)
    started = time.time()
    response = requests.request(
        method,
        f"https://{endpoint}{path}",
        headers={"X-aws-proxy-auth": token},
        json=body,
        timeout=300,
    )
    elapsed = time.time() - started
    return response, elapsed


def wait_for_state(microvm_id: str, want: str) -> float:
    started = time.time()
    while True:
        state = mvm.get_microvm(microvmIdentifier=microvm_id)["state"]
        if state == want:
            return time.time() - started
        if state in ("TERMINATED", "FAILED"):
            sys.exit(f"unexpected state {state}")
        time.sleep(1)


def cmd_run():
    account = boto3.client("sts").get_caller_identity()["Account"]
    started = time.time()
    out = mvm.run_microvm(
        imageIdentifier=f"arn:aws:lambda:{REGION}:{account}:microvm-image:{IMAGE_NAME}",
        executionRoleArn=f"arn:aws:iam::{account}:role/agent-that-sleeps-exec",
        ingressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
        ],
        egressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
        ],
        # Sleep after 2 minutes without traffic; stay asleep up to 8 hours;
        # wake automatically when traffic arrives.
        idlePolicy={
            "autoResumeEnabled": True,
            "maxIdleDurationSeconds": 120,
            "suspendedDurationSeconds": 28800,
        },
        maximumDurationInSeconds=28800,
    )
    microvm_id = out["microvmId"]
    print(f"microvm: {microvm_id}")
    print(f"endpoint: {out['endpoint']}")
    to_running = wait_for_state(microvm_id, "RUNNING")
    print(f"PENDING -> RUNNING in {time.time() - started:.1f}s (poll {to_running:.1f}s)")
    response, elapsed = request(microvm_id, "GET", "/health")
    print(f"first /health in {elapsed:.2f}s: {json.dumps(response.json(), indent=2)}")


def cmd_chat(microvm_id: str, message: str):
    response, elapsed = request(microvm_id, "POST", "/chat", {"message": message})
    body = response.json()
    print(f"[{elapsed:.2f}s total, {body.get('agent_seconds')}s agent] {body.get('reply')}")
    print(f"(run_id={body.get('run_id')}, messages_in_memory={body.get('messages_in_memory')})")


def cmd_health(microvm_id: str):
    response, elapsed = request(microvm_id, "GET", "/health")
    print(f"[{elapsed:.2f}s] {json.dumps(response.json(), indent=2)}")


def cmd_suspend(microvm_id: str):
    mvm.suspend_microvm(microvmIdentifier=microvm_id)
    took = wait_for_state(microvm_id, "SUSPENDED")
    print(f"suspended in {took:.1f}s")


def cmd_resume(microvm_id: str):
    started = time.time()
    mvm.resume_microvm(microvmIdentifier=microvm_id)
    wait_for_state(microvm_id, "RUNNING")
    print(f"explicit resume: SUSPENDED -> RUNNING in {time.time() - started:.1f}s")


def cmd_wake_test(microvm_id: str, message: str):
    state = mvm.get_microvm(microvmIdentifier=microvm_id)["state"]
    print(f"state before request: {state}")
    response, elapsed = request(microvm_id, "POST", "/chat", {"message": message})
    body = response.json()
    print(f"auto-resume + reply in {elapsed:.2f}s ({body.get('agent_seconds')}s of it was the agent)")
    print(f"reply: {body.get('reply')}")
    print(f"(run_id={body.get('run_id')}, messages_in_memory={body.get('messages_in_memory')})")


def cmd_terminate(microvm_id: str):
    mvm.terminate_microvm(microvmIdentifier=microvm_id)
    print("terminate requested")


if __name__ == "__main__":
    commands = {
        "run": cmd_run,
        "chat": cmd_chat,
        "health": cmd_health,
        "suspend": cmd_suspend,
        "resume": cmd_resume,
        "wake-test": cmd_wake_test,
        "terminate": cmd_terminate,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
