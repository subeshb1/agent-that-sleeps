# agent-that-sleeps

A stateful AI agent that lives inside an [AWS Lambda MicroVM](https://aws.amazon.com/lambda/lambda-microvms/), keeps its conversation history in process memory and its notes on local disk, suspends when you stop talking to it, and resumes mid-conversation when you come back. No database, no session store: the Firecracker snapshot is the persistence layer.

Companion repo for the blog post: [An AI agent that sleeps: Lambda MicroVMs](https://www.subeshbhandari.com/blog/an-ai-agent-that-sleeps-lambda-microvms).

## How it works

```
you ── HTTPS + auth token ──> mvm-xxxx.lambda-microvm.us-east-1.on.aws
                                 │
                          Lambda MicroVM (Firecracker)
                          ├─ agent_server.py  (Strands agent, HTTP server)
                          │    ├─ conversation history   (process memory)
                          │    └─ /workspace notes       (VM disk)
                          ├─ lifecycle hooks  /run /suspend /resume /terminate
                          └─ execution role → Amazon Bedrock (Claude Sonnet 5)

idle 2 min ──> suspended (memory+disk snapshotted, storage-only billing)
new request ──> auto-resume, same process continues, history intact
```

- `app/agent_server.py` is the whole application: a Strands agent with two file tools behind a stdlib HTTP server, plus the four MicroVM lifecycle hooks. The `/run` hook regenerates anything that must be unique per VM, because module-level state is frozen into the snapshot at image build time.
- `app/Dockerfile` is built by the Lambda image builder; the snapshot is taken after the server is up, so launches skip initialization entirely.
- `scripts/setup.sh` creates the artifact bucket, the build role, the execution role (Bedrock only), and the MicroVM image, and times the build.
- `scripts/driver.py` launches, chats, suspends, resumes, and measures everything from your laptop.

## Prerequisites

- An AWS account with Bedrock access to Claude Sonnet 5 in a [MicroVMs region](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html) (this repo assumes us-east-1; Sydney is not supported yet)
- AWS CLI v2.36+ (older versions do not have the `lambda-microvms` namespace)
- Python 3.10+ and [uv](https://docs.astral.sh/uv/)

## Run it

```bash
./scripts/setup.sh                      # bucket, roles, image build (~minutes)
uv run scripts/driver.py run            # launch, prints microvm id + timings
uv run scripts/driver.py chat mvm-xxxx "I'm Subesh. Remember the project deadline is Friday."
uv run scripts/driver.py suspend mvm-xxxx
uv run scripts/driver.py wake-test mvm-xxxx "What's my deadline?"   # auto-resume, timed
uv run scripts/driver.py health mvm-xxxx
```

The `wake-test` sends a request to a suspended VM: Lambda holds the request, restores the snapshot, replays the `/resume` hook, and delivers the message to the same process, which still remembers the conversation.

## Cost model

Running: about $0.0000277 per vCPU-second plus $0.0000037 per GB-second. Suspended: snapshot storage only at $0.08/GB-month. Terminated: nothing. Exact measured numbers for a full demo session are in the blog post.

## Cleanup

```bash
uv run scripts/driver.py terminate mvm-xxxx
aws lambda-microvms delete-microvm-image --image-identifier agent-that-sleeps
aws s3 rb "s3://agent-that-sleeps-<account-id>" --force
aws iam delete-role-policy --role-name agent-that-sleeps-build --policy-name build
aws iam delete-role --role-name agent-that-sleeps-build
aws iam delete-role-policy --role-name agent-that-sleeps-exec --policy-name bedrock
aws iam delete-role --role-name agent-that-sleeps-exec
```

Images have a one-week minimum storage retention, so the image line item bills for seven days even after deletion.

## Security notes

- No credentials live in this repo or in the image. The agent gets AWS access through the MicroVM execution role, scoped to `bedrock:InvokeModel*` and nothing else.
- Every endpoint request requires a JWE auth token minted per MicroVM; there is no unauthenticated access.
- Do not bake secrets into the image: the snapshot persists them. Use the execution role, or pass per-VM configuration through the `/run` hook payload.
