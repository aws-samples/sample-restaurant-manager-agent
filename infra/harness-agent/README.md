# harness-agent (spec 022, Task 1)

Least-privilege **execution role** for the AgentCore Managed Harness agent layer,
plus a boto3 **provisioning script** that creates the harness itself.

This is the first task of the spec 022 migration
of the cloud agent layer from durable Lambda to the AgentCore Managed Harness
([ADR-003](../../docs/adr/ADR-003-agentcore-managed-harness.md), Accepted).

> **Additive and reversible.** Deploying this stack creates an unused IAM role;
> running the provision script creates a harness that receives no production
> traffic until the spec 022 cutover (Task 5). Nothing in production changes.

## What this creates

| Resource | How | Why a script and not pure CDK |
|---|---|---|
| Harness **execution role** | CDK (`HarnessAgentStack`) | Standard IAM; CFN-managed. |
| The **harness** itself | `provision/provision_harness.py` (control plane) | The harness has **no stable CloudFormation construct at GA**, so it is created against `bedrock-agentcore-control`. |

The role is scoped to (spec 022 R5.1 / R5.4): Bedrock model invoke on the
configured Sonnet inference profile + its foundation-model ARNs; AgentCore
Memory data-plane on the specific Memory resource ARN only; plus the standard
observability + ECR-public-pull grants the managed runtime requires. It
**deliberately does not** grant `InvokeAgentRuntime`, `InvokeGateway`, `iam:*`,
or any wildcard action.

## Prerequisites

- Python 3.12 (the system Python on Amazon Linux 2023 is 3.9, so use a 3.12 virtualenv).
- AWS credentials for the target AWS account, region `us-west-2`.
- boto3 ≥ 1.43.30 (the GA harness APIs; pinned in `requirements-dev.txt`).

## Setup

```bash
cd infra/harness-agent
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## Synthesize + test (offline)

```bash
PATH=.venv/bin:$PATH cdk synth
.venv/bin/python -m pytest -q
```

The tests assert the role is least-privilege (model + scoped-memory only; no
`InvokeAgentRuntime`, no `InvokeGateway`, no `iam:*`, no `Action: "*"`, and the
model/memory statements are ARN-scoped, never `Resource: "*"`).

## Deploy (operator)

```bash
PATH=.venv/bin:$PATH cdk deploy
# Note the HarnessExecutionRoleArn output.
```

## Provision the harness (operator, after deploy)

```bash
.venv/bin/python provision/provision_harness.py \
  --role-arn <HarnessExecutionRoleArn from the deploy output>
# Idempotent: re-running reuses the existing harness.
# Writes the resolved harness ARN to provision/.harness_arn (gitignored).
```

Tear down the harness (the stack/role are removed with `cdk destroy`):

```bash
.venv/bin/python provision/provision_harness.py --teardown
```

## Enable CloudWatch Transaction Search (one-time, per account — R6.2)

Required once so the harness's per-step tool-invocation traces are queryable.
This is an account-level operator action (console: CloudWatch → Application
Signals/Transaction Search → Enable, or the equivalent API). It is idempotent
and independent of this stack.

## Validation status

- **Offline:** `cdk synth` clean + `pytest` green (role least-privilege assertions).
- **Live (validated 2026-06-18, non-prod account):** stack deployed; harness
  `restaurant_manager_agent-EXAMPLE123` created and `READY`; a no-tool prompt
  streamed `messageStart…messageStop` with `stopReason=end_turn`. **Task 1 live
  criteria met.**

### Gotcha: the GA harness default model is `global.anthropic.claude-sonnet-4-6`

If `create_harness` is called **without** an explicit `model`, the harness uses
the GA default `global.anthropic.claude-sonnet-4-6` (a `global.`-prefixed
inference profile). The first deploy attempt failed at invoke time with
`AccessDeniedException ... bedrock:InvokeModelWithResponseStream on ...
inference-profile/global.anthropic.claude-sonnet-4-6` because the execution role
is scoped to the **pinned** model `us.anthropic.claude-sonnet-4-20250514-v1:0`.

**Rule:** keep the harness model and the role's Bedrock scope consistent.
`provision_harness.py` now passes `model={"bedrockModelConfig": {"modelId": ...}}`
explicitly (defaulting to the pinned model), so the harness never silently
falls back to the shifting GA default. To switch models, change both
`bedrockModelId` in `cdk.json` (role scope) and `--model` on the provision run.

## Where this fits

This harness is the **only** cloud agent runtime. Spec 022 completed in Task 6
(2026-08-05): the chat backend (`simulator/api/harness_backend.py`) drives this
harness on the inline tool path unconditionally, and the durable supervisor +
domain agents — along with the `AGENT_BACKEND` selector that used to choose
between them — were destroyed and deleted. See
tasks.md and
[ADR-003](../../docs/adr/ADR-003-agentcore-managed-harness.md).
