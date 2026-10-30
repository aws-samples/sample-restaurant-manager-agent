# ADR-003: Migrate the agent layer to the AgentCore Managed Harness

## Status

**Accepted** — 2026-06-18, after the GA re-evaluation spike (spec 021)
returned **GO** (verdict):
the GA harness executes declared tools on **both** paths (inline + AgentCore Gateway),
returning live store data; boto3 is sufficient (no CLI); same-session continuity works
(no caller-side replay); latency (~11s warm) is within the durable baseline band. This
**supersedes [ADR-001](ADR-001-durable-lambda-for-agents.md)'s agent-layer decision** — the
cloud agent layer will migrate from durable-Lambda functions to the AgentCore Managed
Harness. The store-side security model (IoT Core transport, gateway publish, dedup,
simulator) is unchanged. Migration to be specified in spec 022.

**Migration complete — 2026-08-05.** Spec 022 Task 6 finished the transition: the durable
agent layer was destroyed and its code removed (see
[ADR-001](ADR-001-durable-lambda-for-agents.md) § Status and
[ADR-002](ADR-002-two-tier-agent-architecture.md)). The harness is no longer *the production
backend among two* — it is the **only** agent runtime. The `AGENT_BACKEND` selector is gone,
so the topology is a single harness exposing the store tools on the inline path (the OD2
collapse recorded in ADR-002), with no fallback runtime behind it.

> **Root cause of the earlier rejection**: the 018 NO-GO was a preview gap **compounded by
> a config gotcha** — `allowedTools` plain-names match *builtin* tools only, so listing the
> inline tool there silently hid it ("no tool available"). Omitting `allowedTools` at GA
> made the harness emit a structured `tool_use` immediately.

Previously **Rejected** — 2026-06-16 (revisit at harness GA): the 018 harness spike
returned NO-GO because the preview `InvokeHarness` did not execute declared tools.

**Rejection rationale**: The spike built and exercised both tool-connectivity paths
(inline functions and AgentCore Gateway) live in us-west-2. Via `InvokeHarness` — the
only supported invocation API (invoking the harness-managed AgentCore Runtime directly
is explicitly forbidden) — the harness **did not execute declared tools** on either
path, across three model ids. The model was told a tool existed but the harness never
ran the tool-use loop: Claude 4 hallucinated results, Claude 4.6 honestly reported it
could not execute the tool. P1 (round-trip correctness) failed both paths. Since this
project's agents exist specifically to invoke store tools over MQTT, a harness that does
not execute tools is unusable for the workload. Full evidence:
the 018 spike verdict (2026-06-16).

**What did work** (so the re-evaluation has a head start): harness create/READY/streaming,
AgentCore Memory attach, and an isolated outbound MQTT round-trip that returned real store
data. The blocker is narrowly tool *execution/wiring* via the preview API.

**Revisit trigger**: harness GA, OR AWS preview support confirms a working tool-execution
path (e.g. the `agentcore` CLI/SDK is required even for Gateway tools). If tool execution
is fixed, this ADR's advantages return and it should be re-proposed.

**History**:
- 2026-06-15: Proposed alongside the 018 spike spec.
- 2026-06-16: Rejected after the 018 spike NO-GO verdict (tool execution does not work via
  `InvokeHarness` in preview). Durable-Lambda layer retained.
- 2026-06-18: **Accepted** after the spec-021 GA re-evaluation
  returned GO — P1 PASS on both inline and Gateway paths with live store data, boto3
  sufficient, latency within band. The `allowedTools`-hides-non-builtins gotcha was a key
  contributor to the 018 failure. Supersedes ADR-001's agent-layer decision; migration spec 022 to follow.
- 2026-06-19: **Implemented** via spec 022
  (Tasks 1-5, verdict recorded 2026-06-19).
  Single harness (OD2 — see the ADR-002 re-evaluation below; ADR-002 now marked superseded),
  inline tool path (OD1), same-session memory via the attached resource (OD3 cross-session
  deferred), POST-202+poll preserved (OD4). SP1 7/7 live parity, SP3/SP5/R6 PASS. The harness
  is the production chat backend (`AGENT_BACKEND=harness`); the durable layer is idle pending
  Task 6 decommission. Two operational facts graduated to `architecture-decisions.md`:
  `invoke_harness` IAM needs **both** `InvokeHarness` + `InvokeAgentRuntime`; inline tool
  specs must match real store signatures (not the durable `@tool` stubs).

## Context

The cloud-side agent layer is currently five durable-Lambda functions (a supervisor
plus four insight agents) running a **hand-rolled** reasoning loop assembled from
Strands building blocks — ADR-001 Path B, implemented across specs 009–017 and
operating in production after the spec 015 cutover and spec 016 Fargate decommission.

Since ADR-001 was decided (2026-04-29), AWS released the **AgentCore Managed Harness**
(preview). The harness inverts the build model ADR-001 assumed: instead of writing the
orchestration loop, you declare an agent in config (model, system prompt, tools,
memory, execution limits) and AgentCore runs the loop — reasoning, tool selection,
action execution, response streaming — powered by Strands. Tools connect via inline
functions (client-side execution with a managed pause/resume), AgentCore Gateway, MCP
servers, or the built-in browser/code-interpreter. Memory attaches as an AgentCore
Memory resource and is loaded/saved automatically per session and actor.

**This changes the calculus that produced ADR-001.** ADR-001 explicitly rejected
"Path A — wrap the Strands loop in a durable function" because *cross-invocation
suspend is not a supported Strands mode* and *auditing Strands internals for replay
determinism is fragile*. The managed harness makes both objections AWS's problem: the
harness **is** a managed Strands loop with a first-class suspend/resume primitive
(inline-function `stopReason: tool_use`). The thing ADR-001 called unsolvable in
self-hosted code is now a supported product feature.

## Decision (proposed)

**Migrate the agent layer from durable-Lambda functions to the AgentCore Managed
Harness, contingent on the 018 spike verdict.** Specifically:

- **Replace** the hand-rolled reasoning loop (`agent_common/reasoning_loop.py`), the
  durable-execution callback plumbing, and the five durable agent Lambdas with one (or
  a small number of) harness definition(s) declared in config.
- **Connect to the store** via either inline functions or AgentCore Gateway — the
  spike chooses, by measured latency and complexity. Either way, the MQTT publish is
  initiated outbound from cloud infrastructure the project already runs; the harness
  never gets a path into the store.
- **Adopt harness-managed memory**, attaching the existing
  deployed AgentCore Memory resource. Delete the manual `create_event` /
  `list_events` plumbing in `chat_service.py`.
- **Keep unchanged** the entire store-side security model: gateway Lambdas, MQTT
  broker, correlation topics, dedup cache, dead-letter topic, and the simulator.
- **Defer** the two-tier vs single-harness structure (the ADR-002 question) to the
  post-gate full-migration spec, informed by spike findings.

### Why this reverses ADR-001 — and why that is correct

ADR-001's three cost objections to AgentCore-on-Fargate are re-answered by the
*harness*, which is a different offering than the Fargate-backed AgentCore Runtime
ADR-001 measured:

| ADR-001 objection | Harness answer |
|---|---|
| Idle compute (5 always-on Fargate containers, ~$60–80/mo) | Per-session microVM with configurable idle timeout (default 900s) + pay-per-use; no always-on baseline |
| Custom DynamoDB polling shim (`async_shim.py`) | Inline-function pause/resume is a managed equivalent of `ctx.callback`; the spike also measures a correlation path that removes polling entirely |
| AgentCore lock-in | **Deliberately re-accepted** — see Consequences |
| Path B rationale ("suspend unsupported in Strands") | The harness provides managed suspend/resume; the rationale no longer applies |

The honest framing: this is not "ADR-001 was wrong." ADR-001 was correct for the world
in which it was written (no managed harness existed; self-hosting the loop was the only
way to get scale-to-zero + managed suspend, and durable Lambda was the better self-host
than Fargate). The harness is new information that warrants revisiting the decision.

## Consequences (proposed)

### Positive
- **Deletes the most complex, highest-maintenance code in the project**: the
  hand-rolled reasoning loop and all the durable-execution gotchas it dragged in
  (qualified-ARN IAM wildcards, `ctx.create_callback` shapes, `Decimal` conversion at
  step boundaries, boto3≥1.42 bundling, Python-3.13-only runtime, SDK module-layout
  traps). The agent layer becomes config.
- **Memory becomes free**: the harness manages the AgentCore Memory resource the
  project already uses; `chat_service.py` shrinks.
- **Model/tool/prompt changes become config edits**, not code rewrites.
- **Built-in observability** (traces/logs/metrics per step) without custom marker
  logging.

### Negative
- **Re-accepts AgentCore lock-in** — the exact thing ADR-001 set out to avoid. The
  agent runtime is now a managed AWS service with no portable equivalent. This is a
  deliberate trade: less code and operational burden in exchange for deeper coupling.
  The mitigation is that the *store-side* contract (MQTT tools) stays portable, so the
  expensive, domain-specific half of the system is unaffected by the lock-in.
- **Preview dependency**: the harness is in public preview in `us-west-2`. Expect
  API rough edges; the project must tolerate preview-grade stability for the agent
  layer until GA.
- **Loop control is surrendered**: ADR-002's 4→2 Bedrock-call optimization is no
  longer ours to make; iteration count is bounded by `maxIterations`, not hand-tuned.
- **Backend becomes a harness orchestrator** (inline path) or simplifies to a single
  invoke (Gateway path) — either way `chat_service.py` is rewritten.
- **Throwaway-ish migration churn**: specs 009–017 built the durable layer; this
  largely retires it. The MQTT subscriber may or may not survive (correlation-path
  dependent).

### Neutral
- Gateway Lambdas, broker, simulator, and the secure-remote-tool-invocation model are
  untouched.
- AgentCore Memory role is unchanged in substance (same resource), only in who drives
  it (harness vs `chat_service`).

## Open questions the spike (018) must close

1. **Tool path** — inline functions vs AgentCore Gateway, by measured latency and
   backend complexity. (Spike P5 + complexity notes.)
2. **Correlation path** — reuse subscriber+`CommandResults`+poll, vs synchronous MQTT
   request-response. (Spike correlation comparison.)
3. **Security boundary** — confirm no chosen cell gives the harness an inbound path to
   the store. (Spike P2; a NO-GO trigger.)
4. **Memory continuity** — does attaching the existing resource recall context across
   sessions without caller-side message replay? (Spike P3.)
5. **Decision stability** — does the managed loop route tools reliably enough for
   production, given we cannot audit it? (Spike P4.)
6. **Latency acceptability** — is harness latency within an acceptable band of the
   durable baseline for interactive chat? (Spike P5.)

## Re-evaluation of ADR-002 (two-tier architecture)

ADR-002 chose a two-tier model (supervisor with direct tools + insight agents) to cut
simple queries from 4 to 2 Bedrock calls. **That rationale is largely obsoleted by the
harness**, which owns the loop and its call count. What may survive is *insight-agent
specialization* (focused prompts for analytical tasks), expressible as separate
harnesses. The tier decision is deferred to the full-migration spec and will be
recorded as an amendment here. ADR-002 will get a status note pointing to this ADR.

## Alternatives considered

- **Keep durable Lambda (do nothing).** Viable — it works in production today. Rejected
  (proposed) because the harness deletes substantial bespoke complexity and the memory
  the project already uses is a natural fit. The spike's NO-GO branch is exactly this
  fallback if the harness does not measure up.
- **Harness for new domains only, durable for existing.** Rejected — split-brain agent
  layer with two reasoning models to maintain; worst of both.
- **Wait for harness GA.** A legitimate timing option; recorded so the Accepted version
  can state why now (spike is low-cost, single-domain, and reversible).

## References

- 018 harness spike spec
- [ADR-001](ADR-001-durable-lambda-for-agents.md) — the decision this supersedes (agent layer)
- [ADR-002](ADR-002-two-tier-agent-architecture.md) — re-evaluated above
- AgentCore harness docs: harness overview, configure agents/models, connect to tools,
  persist memory, observability and cost controls (us-west-2 preview).
- The secure remote tool invocation pattern (see the project README) - the store-side security invariant.

Note: external documentation content referenced here has been summarized and rephrased
for compliance with licensing restrictions.
