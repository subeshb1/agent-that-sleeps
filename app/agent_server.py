"""A stateful Strands agent that lives inside a Lambda MicroVM.

The process is the state. Conversation history lives in this process's
memory and the agent's notes live on this VM's disk. When the MicroVM
suspends, Firecracker snapshots both; when it resumes, the same process
continues with everything intact. Nothing is serialized to a database,
which is the point of the demo.

Lambda talks to the application through lifecycle hooks: POST requests to
/aws/lambda-microvms/runtime/v1/{run,resume,suspend,terminate}. External
traffic is only forwarded after the /run hook returns 200, so the hook is
also where snapshot-baked values (IDs, seeds) must be regenerated.
"""

import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"

# Snapshot caveat: module level code runs once at image build time and is
# frozen into the snapshot. Anything unique must be (re)generated in the
# /run hook, not here.
STATE = {
    "run_id": "unset-until-run-hook",
    "microvm_id": None,
    "started_at": time.time(),
    "lifecycle_events": [],
}

WORKSPACE = Path("/workspace")
WORKSPACE.mkdir(exist_ok=True)

SYSTEM_PROMPT = """\
You are a personal working assistant with a persistent workspace at /workspace.
Your process stays alive between conversations, so treat your memory as real:
what the user told you earlier in this session, you still know.

Use write_note to keep durable notes when the user shares facts, decisions or
tasks worth keeping. Use read_notes to consult them. Keep replies short and
concrete.
"""


@tool
def write_note(filename: str, content: str) -> str:
    """Write a note to the persistent /workspace directory.

    Use a short kebab-case filename ending in .md.
    """
    path = WORKSPACE / os.path.basename(filename)
    path.write_text(content)
    return f"wrote {path} ({len(content)} bytes)"


@tool
def read_notes() -> dict:
    """Read every note in the /workspace directory."""
    return {p.name: p.read_text() for p in sorted(WORKSPACE.glob("*.md"))}


agent = Agent(
    model=BedrockModel(model_id="us.anthropic.claude-sonnet-5"),
    system_prompt=SYSTEM_PROMPT,
    tools=[write_note, read_notes],
)


def credential_source() -> str:
    """Report where boto3 found AWS credentials, without exposing them."""
    try:
        import boto3

        creds = boto3.Session().get_credentials()
        return creds.method if creds else "none"
    except Exception as error:  # pragma: no cover
        return f"error: {type(error).__name__}"


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, hook: str) -> None:
        STATE["lifecycle_events"].append({"hook": hook, "at": round(time.time(), 2)})
        print(f"lifecycle hook: {hook}", flush=True)

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        self._send(
            200,
            {
                "run_id": STATE["run_id"],
                "microvm_id": STATE["microvm_id"],
                "process_uptime_s": round(time.time() - STATE["started_at"], 1),
                "messages_in_memory": len(agent.messages),
                "notes_on_disk": sorted(p.name for p in WORKSPACE.glob("*.md")),
                "credential_source": credential_source(),
                "lifecycle_events": STATE["lifecycle_events"][-10:],
            },
        )

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"

        if self.path.startswith(HOOK_PREFIX):
            hook = self.path[len(HOOK_PREFIX):]
            self._record(hook)
            if hook == "run":
                # Regenerate everything that must be unique per MicroVM:
                # the snapshot froze module state at image build time.
                payload = json.loads(raw or b"{}")
                STATE["run_id"] = secrets.token_hex(4)
                STATE["microvm_id"] = payload.get("microvmId")
                STATE["started_at"] = time.time()
            return self._send(200, {"ok": True})

        if self.path != "/chat":
            return self._send(404, {"error": "not found"})
        payload = json.loads(raw or b"{}")
        message = payload.get("message", "")
        if not message:
            return self._send(400, {"error": "message is required"})
        started = time.time()
        result = agent(message)
        self._send(
            200,
            {
                "reply": str(result),
                "run_id": STATE["run_id"],
                "messages_in_memory": len(agent.messages),
                "agent_seconds": round(time.time() - started, 2),
            },
        )

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"agent server listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
