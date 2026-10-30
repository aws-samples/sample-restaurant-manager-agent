# ADR-005: Adopt Amazon Nova Sonic for the web-app voice interface (harness retained for text/analytics)

## Status

**Accepted** — 2026-06-28. The validation spike that gated this ADR
(spec 024, verdict
2026-06-28)
returned **GO**: Amazon Nova 2 Sonic (`amazon.nova-2-sonic-v1:0`, us-west-2) cleared the
load-bearing properties — **tool-use parity (the 018 make-or-break)**, the **speech-to-speech
audio loop**, and **model access** — validated headlessly with real audio. The product direction
was already locked by stakeholder decision: voice is **web-app only** (no telephony), and
**natural two-way conversation is required from Phase 1**. **One caveat carried into the build:**
the only Python path to the bidirectional API is the **experimental, pre-1.0
`aws-sdk-bedrock-runtime`** SDK (functional, but a maturity/dependency risk). Operator/deploy
confirmations — browser mic UX, on-display proactive (P7), real IoT-round-trip latency, and ARCC
reconciliation — are **entry tasks for the Phase 1 build spec**, not open feasibility risks.

**History**:
- 2026-06-19: Voice explored as a reactive-only "alternate pattern" (Transcribe + Polly
  cascade) in [voice-interface-alternate-pattern.md](../solution-guidance/voice-interface-alternate-pattern.md).
- 2026-06-22: Stakeholder locked the direction — telephony out of scope, web-app-only
  voice, natural two-way required from Phase 1, harness retained for text/analytics. The
  cascade was demoted to a fallback. Proposal rewritten accordingly.
- 2026-06-24: Captured as this ADR (Proposed). Validation spike + Phase 1 spec pending.
- 2026-06-28: Spec 024 Nova Sonic spike returned **GO** (P1 tool-use, P3 audio loop, P5 access
  all PASS live; P4/P6 decided; P2/P7 mechanisms proven, operator/deploy-confirmed). ADR moved
  to **Accepted**; Phase 1 build spec to follow.

## Context

The cloud agent layer today is the **AgentCore Managed Harness** ([ADR-003](ADR-003-agentcore-managed-harness.md),
spec 022), which backs the text `/api/chat` interface and executes store tools over the
IoT Core round-trip ([ADR-004](ADR-004-iot-core-broker-migration.md)). There is no voice
interface in production.

Two needs drive a voice decision:

1. **Reactive voice.** A restaurant general manager often has their hands full and wants
   to *ask* the assistant a question and *hear* the answer, in the web app, without
   typing. The interaction must feel like a conversation — interruptible (barge-in),
   low-latency, two-way — not a walkie-talkie.
2. **Proactive voice.** The new agent-initiated alerting layer ([ADR-006](ADR-006-proactive-alerting-layer.md))
   needs to *speak* an alert into the web app and then let the manager converse and act
   in the same session. A turn-based, record-then-transcribe pattern is a poor fit for an
   urgent spoken alert and follow-up.

### Why a speech-to-speech engine over a cascade

The reactive-only alternate pattern modelled voice as a **cascade**: browser audio →
Amazon Transcribe (STT) → the text agent → Amazon Polly (TTS) → audio. That works and is
a reasonable accessibility/fallback path, but it is inherently **turn-based**: the user
speaks, waits, then hears a reply, with no barge-in and additive STT+inference+TTS
latency. It does not deliver the "natural two-way" requirement.

**Amazon Nova Sonic** is a speech-to-speech model on Amazon Bedrock
(`InvokeModelWithBidirectionalStream`) built for live, interruptible voice with native
tool use. It owns the audio loop end-to-end, supports barge-in, and can call tools
mid-conversation — which maps directly onto both the reactive and proactive needs.

### What the codebase already supports

- **Tool reach is solved.** The gateway Lambdas + `iot-data:Publish` + IoT Topic Rule
  round-trip ([ADR-004](ADR-004-iot-core-broker-migration.md)) already let a cloud
  reasoner invoke store tools. A voice engine reuses this unchanged.
- **A reasoning backend already exists** for text — the harness — and stays.
- **`StoreStateCache`** (last-known store state) is the latency lever for live reads
  during a voice turn; proactive narration needs no round-trip at all because the alert
  payload carries its own data.
- **Harness inline tool specs are a known shape** (spec 022): the lesson that *inline tool
  parameter schemas must match the real store tool signatures* (`item_id` not `item_name`,
  `minutes` not `duration_seconds`, etc.) carries directly into wiring Nova Sonic tool use.

## Decision (proposed)

**Adopt Amazon Nova Sonic (speech-to-speech) as the Phase 1 voice engine for the web app,
retain the AgentCore Managed Harness for text chat and heavy analytical reasoning, and
keep the Transcribe + Polly cascade as a documented fallback only.** Specifically:

1. **Voice loop → Nova Sonic.** The live, two-way voice experience in the web app is a
   Nova Sonic bidirectional session (`InvokeModelWithBidirectionalStream`), with barge-in.
   **Nova Sonic owns its own tool use**, calling the existing gateway/IoT round-trip to
   reach the store. It serves both reactive Q&A and proactive alert follow-up in one
   session.
2. **Text + analytics → harness (unchanged).** The text `/api/chat` interface and heavy
   multi-step analytical reasoning (trend analysis, anomaly explanation) stay on the
   harness per ADR-003. ADR-003 is **narrowed to "text/analytics," not superseded.**
3. **Cascade = fallback only.** Transcribe + Polly is retained as a documented fallback:
   an accessibility/text-equivalent path, and the contingency if the Nova Sonic spike
   finds it unsuitable for this workload.
4. **No telephony.** Voice is delivered **only** through the web app. No phone, SMS, or
   PSTN integration (e.g., no Amazon Connect outbound). This is an explicit scope boundary.
5. **Keep unchanged.** IoT Core transport (ADR-004), the gateway/tools, the
   correlation/dedup model, and the store-side **outbound-only security invariant**
   (see the project README).

## Consequences (proposed)

### Positive
- **Natural two-way UX** — barge-in and live audio meet the Phase 1 requirement that a
  cascade cannot.
- **One session for reactive + proactive** — the same Nova Sonic session answers questions
  and handles spoken alert follow-up, which is exactly what ADR-006 needs.
- **Maximum reuse** — tool reach (gateway + IoT), the security invariant, and the text
  harness are all unchanged; this is additive.
- **Security posture preserved** — voice rides the web app and the existing cloud→store
  path; the store still accepts no inbound connection.

### Negative
- **A newer service on the critical path at launch.** Nova Sonic is the live voice engine;
  its maturity, quotas, and regional availability must be validated (Open Questions).
- **Tool use must be re-wired into Nova Sonic.** The harness inline-tool path does not
  carry over for the voice loop; Nova Sonic's tool declarations must match the **real**
  store tool signatures (the spec-022 parameter-shape lesson). Live round-trip testing is
  required — offline stub tests passed in spec 022 while real signatures had drifted.
- **Two reasoning engines to maintain** — Nova Sonic (voice) and the harness (text/
  analytics) — until/unless a later consolidation. Behavioural parity (same tools, same
  answers) across the two is a maintenance burden.
- **Browser audio surface** — microphone permissions, echo cancellation, and the
  WebRTC/WebSocket transport are new front-end concerns.
- **Latency tail on live tool calls** — a voice turn that triggers a store round-trip
  inherits IoT latency; `StoreStateCache` mitigates reads but write/confirm actions still
  round-trip.

### Neutral
- IoT transport (ADR-004), gateway, dedup, and the simulator are unaffected.
- The harness text path (ADR-003, spec 022) is unchanged in behaviour; this ADR only
  scopes *which* interface it serves.

## Alternatives Considered

- **A. Transcribe + Polly cascade as the primary voice path.** Works and is simpler to
  reason about, but turn-based with no barge-in and additive latency — fails the
  "natural two-way" requirement. **Demoted to fallback**, not primary.
- **B. Harness-only with a TTS bolt-on (speak the text answer).** Rejected: one-way
  narration, not a conversation; no live interruption; awkward for proactive follow-up.
- **C. Telephony (Amazon Connect outbound / SMS).** Rejected: explicitly **out of scope**
  by stakeholder decision. Voice is web-app only.
- **D. Defer voice entirely (text-only).** Rejected: natural two-way voice is a Phase 1
  requirement, and the proactive alert experience depends on spoken delivery.

## Open Questions (for a validation spike — the gate to Accepted)

A tightly-scoped Nova Sonic spike should close these, mirroring the 018/019/021 discipline:

1. **Tool-use wiring & parity.** Can Nova Sonic reliably emit tool-use for the store tools
   and round-trip through the gateway/IoT path, with tool schemas matching the **real**
   store signatures? (Reuse the spec-022 parity harness.)
2. **Barge-in + latency.** Measure conversational latency, and behaviour when a turn
   triggers a live store round-trip vs a `StoreStateCache` read.
3. **Browser transport.** WebRTC vs WebSocket to Bedrock from the web app; microphone
   permission UX; reconnect behaviour.
4. **Session bridging.** Should the Nova Sonic voice session and the harness text session
   share conversation history (one logical session), and how?
5. **Model access / region / cost.** Confirm Nova Sonic availability in the target region
   and the cost/latency envelope for expected usage.
6. **Fallback switch.** Confirm the cascade fallback can be enabled per-user/feature-flag
   without a redesign.

## Relationship to prior decisions

- **ADR-003 (harness):** Narrowed, not superseded. The harness remains the text/analytics
  reasoner; Nova Sonic is added for voice. The spec-022 inline-tool-signature lesson
  applies to Nova Sonic tool wiring.
- **ADR-004 (IoT Core transport):** Unaffected and reused. Voice tool calls ride the same
  gateway/IoT round-trip.
- **ADR-006 (proactive alerting):** Depends on this ADR for the spoken-delivery engine; the
  two are designed together but the proactive **foundation** (ADR-006 Phase 0) is
  engine-independent and can be built first.
- **ADR-002:** Already superseded (single-harness, per the ADR-003 re-evaluation).

## References

- [Voice + proactive architecture proposal](../solution-guidance/voice-and-proactive-architecture-proposal.md) — primary design doc and decision record.
- [Voice interface alternate pattern](../solution-guidance/voice-interface-alternate-pattern.md) — the reactive-only cascade (now the fallback).
- [Reference architecture](../solution-guidance/reference-architecture.md) — current-state baseline.
- [ADR-003](ADR-003-agentcore-managed-harness.md), [ADR-004](ADR-004-iot-core-broker-migration.md).
- 2026-06-22 session handoff.
- Amazon Nova Sonic on Amazon Bedrock — speech-to-speech via `InvokeModelWithBidirectionalStream`.

Note: external documentation content referenced here has been summarized and rephrased
for compliance with licensing restrictions.
