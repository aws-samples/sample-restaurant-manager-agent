# ADR-002: Two-Tier Agent Architecture — Supervisor Direct Tool Access

**Status**: Accepted — **superseded in practice by [ADR-003](ADR-003-agentcore-managed-harness.md) / spec 022** (2026-06-19); the two-tier implementation was **decommissioned 2026-08-05** (spec 022 Task 6 — the supervisor and insight-agent stacks were destroyed and their code deleted)  
**Date**: 2026-05-07  
**Deciders**: project maintainers  
**Source**: Spec 017 (Streamline Agent Flow)

> **Superseded note (2026-06-19):** the two-tier split (supervisor + insight agents) was
> chosen to cut simple queries from 4 Bedrock calls to 2. The AgentCore Managed Harness owns
> the reasoning loop and its call count, so that optimization is no longer the project's to
> make. Spec 022 (OD2) collapsed the agent layer to a **single harness** exposing all 16
> store tools, with analytical guidance folded into the system prompt. Insight-agent
> *specialization* (separate harnesses) remains a documented future option if analytical
> answers ever measurably degrade. This ADR is retained as a point-in-time record.

## Context

The original multi-agent architecture (specs 012–014) uses a strict hierarchy: the supervisor routes every query to a domain agent, which then calls Bedrock twice (tool selection + response formatting) before returning to the supervisor (which calls Bedrock once more to format the final answer). This results in 4 Bedrock `converse()` calls per query, totaling ~9.4s of LLM latency — the dominant bottleneck in end-to-end response time (~21s cold, ~12s warm).

Performance analysis (2026-05-06) showed that for simple queries like "how many beef patties?" the domain agent's two Bedrock calls are pure overhead: the supervisor already knows which tool to call, and the raw tool result can be formatted directly by the supervisor.

## Decision

Adopt a **two-tier agent architecture**:

**Tier 1 — Supervisor with direct tool access**: The supervisor has all 16 MCP tools in its `toolConfig` and calls gateway Lambdas directly for simple queries (reads, status checks, direct updates). This reduces simple queries from 4 to **2 Bedrock calls**.

**Tier 2 — Insight Agents for complex analysis**: The four domain agents are renamed and refocused as insight agents (Inventory Insights Agent, POS Insights Agent, Equipment Insights Agent, Schedule Insights Agent). They handle trend analysis, predictions, anomaly detection, and multi-step reasoning. The supervisor routes to them only when the query requires analytical depth.

The supervisor decides routing (simple vs complex) autonomously based on system prompt guidance — no hard-coded routing rules.

## Consequences

### Positive

- **~50% latency reduction** for simple queries (2 Bedrock calls instead of 4).
- **Single-hop MQTT path** for simple queries (supervisor → gateway → simulator), eliminating the domain agent Lambda cold start (~3.4s).
- **Insight agents can specialize** in analytical tasks with richer prompts and tools (e.g., time-range usage reports).
- **Backward compatible** — insight agents retain their existing tool access and can still be invoked directly.

### Negative

- **Supervisor complexity increases** — it now has 20 tools (16 direct + 4 insight agents) and needs DynamoDB access for the callback flow.
- **Supervisor IAM scope widens** — needs `lambda:InvokeFunction` on 4 gateway Lambdas + 4 insight agent Lambdas, plus DynamoDB access to `CommandResults` and `StoreStateCache`.
- **Larger supervisor prompt** — listing 16 tools + routing guidance increases token usage per Bedrock call (marginal cost).

### Neutral

- Domain agent Lambdas remain deployed; they are not deleted. They serve as insight agents and can be invoked for complex queries.
- The MQTT subscriber, gateway Lambdas, and simulator are unaffected (except the additive consumption log enhancement).

## Alternatives Considered

### A: Keep the current 4-call architecture, optimize with provisioned concurrency

Reduces cold starts (~7s) but doesn't address the fundamental 4-call overhead (~9.4s LLM time). Rejected because LLM latency is the dominant bottleneck, not cold starts.

### B: Collapse everything into the supervisor (no insight agents)

The supervisor handles ALL queries directly with 2 Bedrock calls. Rejected because complex analytical queries (trend analysis, predictions) benefit from a dedicated agent with a focused system prompt and multi-turn reasoning capability. A single supervisor prompt covering all 16 tools + analytical instructions would be too large and unfocused.

### C: Pre-compute tool selection in the supervisor without Bedrock

Use a keyword-matching heuristic to select tools without a Bedrock call. Rejected because it's brittle, doesn't handle ambiguous queries, and loses the model's ability to call multiple tools in parallel for multi-domain queries.
