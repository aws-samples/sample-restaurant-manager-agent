# ADR-001: Use AWS Lambda Durable Functions for agents, replacing Bedrock AgentCore on Fargate

## Status

Accepted — 2026-04-29. Open question #1 closed 2026-04-30 via spec 010 POC.

**Agent-layer decision SUPERSEDED by [ADR-003](ADR-003-agentcore-managed-harness.md)** —
2026-06-19. The cloud agent layer was migrated from durable-Lambda functions to the
AgentCore Managed Harness (spec 022,
verdict recorded 2026-06-19).
This ADR's analysis is retained as a point-in-time record of why durable Lambda was chosen
in the no-managed-harness world.

**DECOMMISSIONED — 2026-08-05.** Spec 022 Task 6 is complete: the durable supervisor and the
four domain agents are **gone**, not merely idle. `DurableSupervisorStack`,
`DurableEquipmentAgentStack` and `DurableRemainingAgentsStack` were destroyed (zero durable
Lambdas, log groups or IAM roles remain in the non-prod account), their packages under `infra/` were
deleted, and the `AGENT_BACKEND` selector plus the durable `ChatService` were removed from the
simulator backend. **There is no longer a durable rollback path** — reverting would mean
restoring this code from git and redeploying. The gate was a Task 5 GO parity verdict plus a
soak showing zero invocations across all five durable agents over 30 days.

**History**:
- 2026-04-28: Proposed.
- 2026-04-29: Accepted after POC validation (spec 009, verdict GO-WITH-CHANGES). See [POC Validation Results](#poc-validation-results) below.
- 2026-04-30: Open question #1 (Bedrock determinism inside `ctx.step`) resolved GO-WITH-CHANGES via spec 010 POC. See [Open Question #1 Resolution](#open-question-1-resolution) below.
- 2026-06-19: Agent-layer decision superseded by ADR-003 (spec 022 cutover). Durable layer idle pending decommission.

## Context

The cloud MCP gateway (FEAT-006, spec 006) runs five Strands agents — one supervisor and four domain agents (POS, equipment, inventory, schedule) — on Bedrock AgentCore Runtime, packaged as Fargate-backed container images. Each domain agent invokes its corresponding gateway Lambda to publish a command over MQTT to a store, then waits for the response. The "wait" is implemented as a polling loop against a DynamoDB `CommandResults` table (see `infra/cloud-mcp-gateway/containers/agent_common/async_shim.py`), with fallback to a `StoreStateCache` on timeout.

This architecture has three costs that accumulate:

1. **Idle compute**. Five AgentCore/Fargate containers run continuously, even when no prompts are in flight. At dev scale this is ~$60–80/month in baseline cost that does no useful work between invocations.
2. **Custom polling infrastructure**. The `async_shim.py` module re-implements async result-awaiting on top of DynamoDB polling. Each tool invocation reads the table every 250 ms–2 s until a terminal status appears. This adds latency (minimum 250 ms even for fast responses), DynamoDB read volume, and code to maintain.
3. **AgentCore lock-in**. The async bridge uses AgentCore's `add_async_task()` / `complete_async_task()` primitives, tying the agent loop to that runtime.

The user expressed a mild preference for a serverless architecture and asked whether AWS Lambda Durable Functions could replace Fargate for the agent workloads.

### What AWS Lambda Durable Functions provide

AWS Lambda Durable Functions (see the [official documentation](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)) is a checkpoint-and-replay programming model layered on Lambda. Key operations relevant to this architecture:

- **`ctx.step(...)`** — execute and checkpoint a unit of work with automatic retries. Persisted result is returned on replay without re-executing.
- **`ctx.callback(...)`** — suspend execution and wait for an external system to call `SendDurableExecutionCallbackSuccess` via the Lambda API. No compute is billed during the wait. Structurally equivalent to Step Functions `waitForTaskToken` but native to Lambda.
- **`ctx.parallel(...)`** — fan out multiple operations concurrently with a single checkpoint.
- **`ctx.invoke(...)`** — call another Lambda and checkpoint the result.
- **Execution timeout** — up to **one year** total elapsed time per durable execution. Individual Lambda invocations are still capped at 15 minutes; the SDK terminates and re-invokes across the boundary.

The `Callback` operation is a near-perfect structural match for the MQTT publish-and-wait pattern: the domain agent invokes the gateway Lambda, registers a callback, and the Fargate response handler (or its serverless replacement) completes the callback when the MQTT reply lands. This eliminates the DynamoDB poll loop entirely.

### What the Strands SDK provides

The Strands Agents SDK ([agent-loop documentation](https://strandsagents.com/docs/user-guide/concepts/agents/agent-loop/index.md)) implements a standard agentic loop: invoke model → select tool → execute tool → feed result back into model → repeat until the model produces a final answer.

The loop is owned by the SDK. Calling `agent("prompt")` runs the full loop internally in a single Python process. The SDK emits lifecycle events before and after model calls and tool executions, and supports `agent.cancel()` for external termination, but does not expose a "suspend here and resume later in a new process" primitive.

## Decision

**Migrate the five agents (supervisor + four domain agents) from Bedrock AgentCore on Fargate to AWS Lambda Durable Functions. Replace the Strands SDK's `Agent` loop with a hand-rolled reasoning loop built from Strands' lower-level building blocks (Path B).**

Specifically:

- **Reuse** from Strands:
  - The `BedrockModel` client (or equivalent model clients).
  - The `@tool` decorator for tool schema generation.
  - The MCP tool adapters where applicable.
  - Tool specs and type hints.

- **Replace** from Strands:
  - The `Agent(...)` orchestrator and its internal event loop.
  - The tool executor (Strands runs tools synchronously in-process; durable functions need each tool call wrapped in `ctx.step` or `ctx.callback`).

- **Hand-roll** the reasoning loop as a durable-function handler:
  - Each model call is a `ctx.step`.
  - Each synchronous tool call is a `ctx.step`.
  - Each async MCP-over-MQTT tool call uses `ctx.callback`, with the callback ID written to `CommandResults` where the response handler can find it.
  - Fan-out across tool calls within a single model turn uses `ctx.parallel`.
  - Supervisor → domain agent calls use `ctx.invoke`.

- **Keep unchanged**: the four gateway Lambdas (already a good fit for Lambda), the DynamoDB tables, the MQTT topic layout, and the store-side protocol.

- **Defer**: the response handler remains on Fargate for now. Its only change is to call `SendDurableExecutionCallbackSuccess` instead of updating `CommandResults.status`. A future ADR may address swapping Amazon MQ for AWS IoT Core to eliminate the Fargate component entirely.

### Why Path B over Path A (wrapping the Strands loop)

Path A — keeping `Agent(...).invoke(...)` and integrating durable semantics through Strands hooks — was considered and rejected because:

1. **Cross-invocation suspend is not a supported Strands mode.** The Strands agent loop is a generator/coroutine that runs to completion in a single Python process. Its `cancel()` primitive is fire-and-forget termination, not suspend-and-resume. Making it survive a Lambda invocation boundary mid-cycle would require deep, unsupported integration with `strands.event_loop.event_loop`.
2. **Determinism audit burden is high.** Durable-function replay requires that any code outside a `ctx.step` be a pure function of handler inputs and prior checkpoint results. Strands internals may generate UUIDs, read wall clocks, or perform other non-deterministic operations that would need to be identified and isolated. With the loop owned by the SDK, this audit is both difficult and fragile to upstream changes.
3. **Upstream drift risk.** Any deep integration with Strands internals would break on Strands version bumps.

Path B gives up the `Agent(...)` one-liner but keeps all the building blocks where Strands genuinely adds value (model abstraction, tool schema, MCP adapters). The hand-rolled loop is a well-understood state machine (roughly four stop reasons: `end_turn`, `tool_use`, `max_tokens`, `cancelled`) implementable in approximately 50–100 lines.

## Consequences

### Positive

- **Real scale-to-zero.** Agents incur no compute cost between prompts. For bursty dev traffic, this is a significant cost reduction vs. continuously-running Fargate containers.
- **Callback replaces polling.** `async_shim.py` and the minimum-250 ms poll-interval floor go away. Response latency improves; DynamoDB read volume drops.
- **Up to one-year execution window.** The 30 s per-tool timeout never hits a platform ceiling. Future "human in the loop" approval flows become feasible without architectural changes.
- **Pay only for active execution.** No compute billed during MQTT wait.
- **Determinism by construction.** Every model call, tool call, and fan-out is a first-class checkpoint. No hidden non-determinism inside a framework's loop.
- **Alignment with the project's serverless posture.** Reduces the infrastructure surface (fewer Fargate services, fewer VPC concerns for agents).
- **No AgentCore lock-in** for the agent runtime. Bedrock model access is still required but is accessed via `boto3`.

### Negative

- **Loss of the `Agent(...)` programming model.** Developers write the loop explicitly rather than relying on `agent("prompt")`. This is a real ergonomic cost.
- **Durable Functions is a new service.** Fewer public examples and battle-tested patterns than mature alternatives. Expect rough edges.
- **Rewrite of the agent containers.** All five agents (supervisor + four domain) are reimplemented. Existing code in `infra/cloud-mcp-gateway/containers/` is largely obsolete.
- **Replay-determinism discipline required.** Anything outside a `ctx.step` or other durable operation must be a pure function of inputs and prior checkpoints. Wall clocks, UUIDs, external I/O, and mutable global state must all be wrapped. This is a programming-model shift the team has to internalize.
- **Bedrock integration is DIY.** Uses `boto3` calls to Bedrock inside `ctx.step`. AgentCore's agent-to-agent invocation is replaced by `ctx.invoke` between Lambda functions.
- **Response handler stays on Fargate for now.** Full serverless requires a second, broader change (Amazon MQ → AWS IoT Core) tracked separately.
- **Debugging replay takes getting used to.** Each step runs during replay but returns its cached result. Print statements execute on every replay.

### Neutral

- **Gateway Lambdas unchanged.** They were already a good fit.
- **DynamoDB role shifts.** `CommandResults` becomes a lookup table for callback IDs (used briefly during the MQTT round-trip) rather than a long-lived correlation store polled by agents. `StoreStateCache` keeps its current role as the stale-fallback cache; the durable function's `ctx.callback` timeout branch reads from it.
- **IAM surface grows and shifts.** Agent Lambdas need `lambda:SendDurableExecutionCallbackSuccess` / `Failure`, plus `CheckpointDurableExecution` / `GetDurableExecutionState`. Fargate task role for agents goes away.

## Alternatives Considered

### Alternative 1: Keep current AgentCore on Fargate architecture

**Rejected.** Works today but does not address idle cost or the custom polling shim. Does not align with the user's stated serverless preference.

### Alternative 2: Move agents to Step Functions + Lambda

**Rejected.** Step Functions provides equivalent `waitForTaskToken` semantics to durable-function callbacks. However:
- The agent reasoning loop becomes an explicit state machine in Amazon States Language, which is a much larger semantic shift than writing the loop in Python.
- Step Functions charges per state transition; for agent workloads with many tool calls per prompt, this adds up.
- Debugging an ASL state machine is harder than debugging Python.
- Durable Functions achieves the same outcome with a smaller semantic shift.

### Alternative 3: Path A — wrap the Strands loop in a durable function

**Rejected.** See "Why Path B over Path A" above. Three strikes: cross-invocation suspend is not a supported Strands mode, replay-determinism audit of SDK internals is fragile, and upstream drift risk is high.

### Alternative 4: Full serverless in one step (agents → Durable Functions AND Amazon MQ → IoT Core)

**Deferred, not rejected.** This is the endpoint but not the next step. Swapping the broker invalidates spec 005 and requires reconfiguring the store-side simulator (spec 004) for mutual TLS against IoT Core. That is a separate architectural decision of its own scope. The agent migration can proceed independently and deliver most of the cost/latency benefit; the broker swap can follow in a later ADR.

## Open Questions

The four open questions below were identified when this ADR was proposed. Their status after the POC (spec 009, 2026-04-29) is noted inline.

1. ~~Does Strands' `BedrockModel` client have any hidden non-deterministic behavior (implicit UUIDs, timestamps) that would need to be wrapped when used inside `ctx.step`?~~ **Resolved: GO-WITH-CHANGES (2026-04-30).** Spec 010 POC invoked Bedrock `converse()` inside a durable step and confirmed the raw response dict round-trips through the SDK's JSON checkpoint without any mutation or filtering required. `@tool`-decorated functions are also safe to call inside `ctx.step`. See [Open Question #1 Resolution](#open-question-1-resolution) below.
2. ~~Can `ctx.parallel` efficiently fan out Bedrock calls for multi-tool turns, or does the model-call concurrency need manual throttling?~~ **Not resolved by POC.** Same reason. Deferred.
3. ~~How does the response handler's Fargate → `SendDurableExecutionCallbackSuccess` path perform at the expected response latencies?~~ **Resolved: GO.** POC measured warm callback resume at 446 ms; the API call itself is sub-second. Response handler performance is not a blocker.
4. ~~What is the real cold-start impact on first-prompt latency for the supervisor Lambda? Bedrock calls dominate total latency, but worth measuring.~~ **Partially resolved.** POC measured 5.0 s from async invoke to `callback_id` persisted in DynamoDB on cold start, 2.5 s warm. Includes Lambda init + gateway invoke + DynamoDB write. Acceptable for interactive use — Bedrock model calls (10–30 s typical) will still dominate end-to-end latency.

## Validation Plan

Before committing the full migration, prove the architecture with a one-agent POC:

1. Single domain agent (equipment) as a durable function using Path B.
2. One MCP-over-MQTT tool (`get_equipment_status`) using `ctx.callback`.
3. Existing gateway Lambda and response handler, with the response handler modified to call `SendDurableExecutionCallbackSuccess`.
4. Measure: end-to-end latency, cold-start impact, replay behavior under simulated failure, DynamoDB read volume.

POC scope: one to two days of work. If successful, proceeds to a spec covering the full migration.

## POC Validation Results

**Spec**: 009-durable-lambda-agent-spike
**Verdict finding**: an internal design record
**Verdict**: **GO-WITH-CHANGES**

All five correctness properties passed:

| Property | Result |
|---|---|
| P1 — Callback round-trip correctness | PASS |
| P2 — Callback token persisted before suspend | PASS |
| P3 — Idempotent replay on happy path | PASS |
| P4 — `CommandResults` schema remains additive | PASS |
| P5 — Timeout path exercised | PASS |

**Latency (measured end-to-end, includes harness overhead)**:
- Cold start: 12.6 s
- Warm: 8.4 s
- Timeout: 43.2 s (including the 30 s callback timeout itself)

**Latency (measured architectural round-trip, excluding harness polling)**:
- Cold start suspend: 5.0 s — resume: 2.1 s
- Warm suspend: 2.5 s — resume: 446 ms

The verdict is GO-WITH-CHANGES (not GO) because the end-to-end numbers include harness overhead that a production caller would not incur. The actual architectural round-trip is fast.

### Implementation gotchas discovered during the POC

Six issues were discovered while implementing the POC. The full-migration spec must account for all of them. These are captured here so future readers of this ADR don't have to re-discover them.

1. **Python 3.13+ is required.** Durable functions do not support Python 3.12 or earlier. Use `aws_lambda.Runtime.PYTHON_3_13` (or 3.14). Attempting to use 3.12 fails at CloudFormation create time with: *"You cannot use a managed runtime that does not support a durable configuration."*

2. **IAM qualified-ARN wildcards are required.** All durable execution IAM actions operate on qualified ARNs that include the function version and execution ID, not the bare function ARN. Policies must include both:
   ```
   resources=[
       function.function_arn,          # e.g. arn:aws:lambda:...:function:my-fn
       f"{function.function_arn}:*",   # covers :live, :1, :N/durable-execution/.../..., etc.
   ]
   ```
   Applies to `lambda:CheckpointDurableExecution`, `lambda:GetDurableExecutionState`, `lambda:SendDurableExecutionCallbackSuccess`, and `lambda:SendDurableExecutionCallbackFailure`.

3. **Callback result arrives as a JSON string.** `callback.result()` returns whatever was passed as the `Result` blob in `SendDurableExecutionCallbackSuccess`. If the sender serializes with `json.dumps(dict).encode()`, the receiver gets a JSON string, not a dict. The handler must `json.loads(...)` it before returning or using.

4. **DynamoDB `Decimal` values must be converted before returning from durable steps.** DynamoDB returns all numeric types as `decimal.Decimal`. The durable SDK serializes step return values via JSON, which raises `TypeError: Object of type Decimal is not JSON serializable`. Any step that reads DynamoDB must convert `Decimal` to native `int` or `float` before returning. This surfaced in the POC's timeout-fallback branch that reads `StoreStateCache`.

5. **Lambda runtime's bundled boto3 lags.** The boto3 pre-installed in the Python 3.13 Lambda runtime may not include the durable execution API methods (`send_durable_execution_callback_success`, etc.). Bundle a recent boto3 (≥1.42) explicitly in the deployment package of any Lambda that calls these APIs. This affects the response handler and any agent that uses `ctx.invoke` between durable functions.

6. **SDK module paths are `.exceptions`, not `.errors`.** The Python durable execution SDK puts exception classes (including `CallbackError`) under `aws_durable_execution_sdk_python.exceptions`. The design pseudocode in spec 009 initially assumed `.errors`; this fails at import time. Also note `CallbackConfig` and `Duration` live under `aws_durable_execution_sdk_python.config`.

### Other API notes from the POC

- The `ListDurableExecutionsByFunction` API accepts only **one status filter at a time**. Calling with `Statuses=["SUCCEEDED", "FAILED"]` raises `ValidationException`. Call it separately for each status needed.
- Durable functions with `ExecutionTimeout > 15 minutes` **cannot be invoked synchronously**. Callers must use `InvocationType: "Event"` and fetch results via `get_durable_execution`.
- Versions and aliases are required for durable functions. The CDK `aws_lambda.Function` construct needs a `current_version` + `aws_lambda.Alias` combination; invoke via the alias ARN.

### What the POC did not test

- **Bedrock model calls inside `ctx.step`.** The POC did not invoke Bedrock. Open question #1 remains open — whether `BedrockModel` introduces hidden non-determinism (UUIDs, timestamps) that would break replay. Must be validated in the full-migration spec or a follow-up POC.
- **Multi-tool parallel fan-out (`ctx.parallel`).** Deferred to full migration.
- **Supervisor → domain-agent calls (`ctx.invoke`).** Deferred to full migration.
- **Replay behavior under simulated failure.** The POC only tested the happy path + callback timeout. Replay under platform-induced mid-execution failure was not exercised.
- **Amazon MQ → IoT Core migration.** Explicitly deferred to a separate ADR.

## Open Question #1 Resolution

**Spec**: 010-durable-bedrock-spike
**Verdict finding**: an internal design record
**Verdict**: **GO-WITH-CHANGES**

A second, narrow POC addressed the one open question spec 009 deliberately deferred: whether Bedrock `converse()` responses embed hidden non-determinism that would break durable-function replay semantics. Spec 010 deployed a single durable Lambda that ran a three-step agent cycle entirely inside durable execution:

1. First Bedrock `converse()` call (checkpointed in `ctx.step`)
2. A `@strands.tool`-decorated Python function (`get_weather`) invoked with the model-requested arguments
3. Second Bedrock `converse()` call that fed the tool result back to the model

### Property results

| Property | Result | Basis |
|---|---|---|
| P1 — Replay determinism | INDETERMINATE | Execution completed inside a single Lambda invocation; replay across invocation boundaries was not naturally observed. Design-allowed per the "INDETERMINATE + all others PASS → GO-WITH-CHANGES" branch. |
| P2 — `@tool` integration | PASS | `tool_call` step returned a well-formed `ToolCallRecord` with `tool_name="get_weather"` and a structured `tool_output`. |
| P3 — No hidden non-determinism | PASS | Both Bedrock responses round-tripped through `json.dumps` without raising. `Status == SUCCEEDED` is itself strong evidence — any non-serializable field would have failed the step. |
| P4 — Step ordering | PASS | Observed step order `["first_bedrock", "tool_call", "second_bedrock"]` exactly matched the handler's orchestration. |

### Reusable facts extracted

1. **Raw `boto3.converse()` responses checkpoint cleanly.** The full response dict — including `ResponseMetadata.RequestId`, `metrics.latencyMs`, `output.message`, `usage`, and `stopReason` — serializes through the durable SDK's JSON checkpoint without mutation or filtering. No custom scrubber needed at the step boundary.
2. **Strands `@tool`-decorated functions are safe inside `ctx.step`.** The `@strands.tool` decorator's `tool_spec` attribute is generated at import time and does not interfere with replay semantics. Full-migration agents can use a plain callable registry (`{"tool_name": fn, ...}`) rather than wrapping the Strands event loop. This concretely confirms the viability of Path B.

Both facts are recorded in the project's architecture-decisions notes under "AWS Lambda Durable Functions - Bedrock integration notes" for future reference.

### Known limitation — P1 observability

Spec 010's P1 is INDETERMINATE (not PASS) because `DurableContext` in the current Python SDK does not expose an `execution_id` attribute. `measure.py` filters CloudWatch events by execution_id, so it cannot currently distinguish a true single-invocation execution from one where the filter matched zero events despite markers being in the log group. For the full-migration spec, `measure.py` should either broaden the filter to use the Lambda request ID as a proxy for invocation boundaries, or wait for the SDK to expose `context.execution_id`. Tracked as a project todo.

The P1 INDETERMINATE result does not block the verdict — P3 and the `SUCCEEDED` status together already demonstrate that no replay-breaking non-determinism reached the SDK's serializer. What P1 cannot yet demonstrate is that a multi-invocation execution replays each step's cached result without re-execution; that test remains for the full migration.

## References

- [AWS Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html) — feature overview
- [Durable Execution SDK](https://docs.aws.amazon.com/lambda/latest/dg/durable-execution-sdk.html) — primitives
- [Callback operation](https://docs.aws.amazon.com/durable-execution/sdk-reference/operations/callback/index.html) — external-signal suspension
- [Determinism during replay](https://docs.aws.amazon.com/durable-execution/patterns/best-practices/determinism/index.html) — programming-model constraint
- [Durable functions or Step Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-step-functions.html) — comparison
- [Strands Agents — agent loop](https://strandsagents.com/docs/user-guide/concepts/agents/agent-loop/index.md) — loop semantics
- [Strands Agents — Lambda deployment](https://strandsagents.com/docs/user-guide/deploy/deploy_to_aws_lambda/index.md) — existing Lambda deployment guidance
- `docs/planning/features/FEAT-006-cloud-mcp-gateway.md` — the architecture this ADR supersedes
- Spec 006 (cloud MCP gateway) - the spec that implemented the superseded architecture
- `infra/cloud-mcp-gateway/containers/agent_common/async_shim.py` — the polling shim this migration eliminates

Note: external documentation content in this ADR has been summarized and rephrased for compliance with licensing restrictions.
