# ADR-006: Add an agent-initiated proactive alerting layer (IoT events → EventBridge → Step Functions → web-app delivery)

## Status

**Accepted** — 2026-07-06 (Phase 0 was accepted 2026-06-24; the final ARCC gate cleared 2026-07-06). The product direction is **locked by stakeholder decision**
(captured in the [voice + proactive proposal](../solution-guidance/voice-and-proactive-architecture-proposal.md)
and the 2026-06-22 session handoff):
the agent must **initiate** contact when a store alert fires, delivered **into the web
app** (no telephony), with **bounded re-surfacing on the same display** on no-acknowledgment
(there is no on-call to escalate to — decision #4). The **architecture below is
engine-independent** and was the natural first build ("Phase 0").

The Phase 0 spec `023-proactive-alerting-foundation`
is **implemented and end-to-end validated** in the non-prod account (Task 9 GATE passed 2026-06-24):
a simulated equipment failure flows store → IoT → ingest → `Alerts` + `AlertRaised` → Step Functions
policy → `notifications` → the store's in-app inbox, and is acknowledged from the display, resuming
the workflow. Operational decisions #2-#5 are resolved (see Resolved decisions). The final gate —
**ARCC reconciliation** of the new IoT grants — was **cleared 2026-07-06** (see Open Questions). Phase 1
(Nova Sonic voice, Web Push) is a separate effort under [ADR-005](ADR-005-nova-sonic-voice-interface.md).

**History**:
- 2026-06-22: Stakeholder added the proactive requirement (agent-initiated voice on a
  store alert) and locked web-app-only delivery. Captured in the proposal.
- 2026-06-24: Captured as this ADR (Proposed); operational decisions #2-#5 then settled with
  the stakeholder and the Phase 0 spec `023-proactive-alerting-foundation` authored.
- 2026-06-24: Phase 0 **implemented and validated** (spec 023 Tasks 1-9; Task 9 end-to-end GATE
  passed in non-prod). ADR moved to **Accepted (Phase 0)**; remaining gate is ARCC reconciliation.
- 2026-07-06: **ARCC reconciliation completed** (run via the `arcc` CLI; the `search_arcc` MCP tool
  was not loaded in-session). The topic-scoped grants were found compliant; the one Cat A gap — the
  `events/*` handlers trusted the payload `store_id` over the IoT-authenticated topic — was fixed,
  deployed to non-prod (us-west-2), and live-validated. ADR moved to **Accepted**
  (final gate cleared).

## Context

The store↔cloud path today is **request/response only**: cloud agents pull, the store
answers. Equipment alerts already exist in the simulator model — `Alert` with
severity/status, the `acknowledge_alert` tool, and an injectable `equipment_failure`
event — but they **only surface when the cloud pulls** `get_equipment_status`. The store
never publishes anything unsolicited. So today a manager learns about a freezer failure
only if the agent (or a human) happens to ask.

The requirement is the inverse: when a meaningful alert fires at the store, **the system
should reach out to the manager** — speak it into the web app (via [ADR-005](ADR-005-nova-sonic-voice-interface.md))
and let them converse and act.

### What the codebase already supports

- **A validated store→cloud event shape.** The response path is an **IoT Topic Rule
  (`stores/+/responses/+`) → Lambda** ([ADR-004](ADR-004-iot-core-broker-migration.md),
  the `iot-response-handler`). The alert path **mirrors this exactly** with a new
  `stores/+/events/+` rule → ingest Lambda — a known, working pattern, not a new transport.
- **Outbound-only is preserved.** The store publishes alert events over its **existing
  outbound mTLS connection**; no inbound connection is introduced. IoT Core supports this
  natively.
- **Per-device isolation already exists.** The shared fleet policy uses
  `${iot:Connection.Thing.ThingName}`; it needs **one additive grant** for `events/*`
  publish, scoped the same way (`client_id == thing_name == store_id`).
- **Alert data + ack tool exist.** `Alert` severity/status and `acknowledge_alert` are
  reusable; the Schedule domain (`get_current_shifts`) can resolve on-call/contact.
- **`StoreStateCache`** can carry last-known state; proactive narration needs no store
  round-trip because the alert payload carries its own reading.

## Decision (proposed)

**Add a serverless, engine-independent proactive alerting layer that turns a store-side
alert into an agent-initiated, web-app-delivered notification with escalation.**
Specifically:

1. **New store→cloud event topic (new store behaviour).** The store publishes alert events
   to `stores/{store_id}/events/{domain}` over its existing outbound IoT connection. An
   **IoT Topic Rule `stores/+/events/+`** invokes an **ingest Lambda** — the same shape as
   the validated response handler. The shared IoT policy gains an additive
   `${iot:Connection.Thing.ThingName}`-scoped `events/*` publish grant.
2. **Ingest → dedup + persist + emit.** The ingest Lambda deduplicates, persists the alert
   to a new **`Alerts` DynamoDB table**, and emits an `AlertRaised` event to **Amazon
   EventBridge**.
3. **EventBridge → Step Functions orchestrator.** A **Step Functions** state machine owns
   alert *policy*: severity classification, **quiet-hours** (with critical override),
   **debounce/dedup**, **acknowledgment** tracking (`waitForTaskToken`), and — on no-ack within
   the SLA — **bounded re-surface on the same display** (no Schedule-domain contact resolution and
   no multi-person escalation; decision #4).
4. **Compose → deliver (web-app only).** The orchestrator delivers the notification **into the web
   app**. **Phase 0** (implemented): a cloud→store `notifications` relay feeds the store's existing
   **WebSocket** and an **in-app alert inbox** (the durable floor) on the always-on in-store display.
   **Phase 1**: spoken composition via the reasoning core (Nova Sonic per
   [ADR-005](ADR-005-nova-sonic-voice-interface.md); harness for text) and **Web Push** to re-engage
   a backgrounded/closed tab. No telephony.
5. **Engine-independent foundation.** Steps 1–3 (events topic + IoT grant + ingest Lambda +
   `Alerts` table + EventBridge + Step Functions skeleton + policy) are **Phase 0** and can
   be built and validated **before** the voice engine exists — delivery can start as an
   in-app inbox entry and grow into spoken delivery.
6. **Keep unchanged.** The outbound-only security invariant, IoT transport, gateway/tools,
   correlation/dedup model; reuse `acknowledge_alert`.

```
 store sensor alarm
   -> store publishes stores/{id}/events/{domain}   (outbound mTLS, NEW behaviour)
   -> IoT Topic Rule stores/+/events/+              (mirrors the responses rule)
   -> ingest Lambda  (dedup, persist Alerts table)
   -> EventBridge (AlertRaised)
   -> Step Functions (severity / quiet-hours / debounce / ack / bounded re-surface)
   -> web-app delivery (notifications relay -> WebSocket push + in-app inbox; Phase 1: voice / Web Push)
   -> manager acknowledges  (else bounded re-surface on the same display, then expire)
```

## Consequences (proposed)

### Positive
- **Delivers the agent-initiated requirement** — the system reaches out instead of waiting
  to be asked.
- **Reuses a validated pattern** — the `events` path is the proven `responses` Rule→Lambda
  shape; the security model and transport are unchanged and only additively extended.
- **Engine-independent foundation** — Phase 0 can ship and be validated before the voice
  engine, de-risking the program.
- **Serverless / scale-to-zero** — IoT Rule + Lambda + EventBridge + Step Functions match
  the project's serverless posture (ADR-001/ADR-004 spirit).
- **Web-app-only keeps blast radius small** — no PSTN/SMS provider, no new external egress.

### Negative
- **The store now publishes unsolicited events** — genuinely new behaviour plus a new IoT
  publish grant. This is a **security surface** (event authenticity, topic-scoping,
  payload validation) that needed **ARCC reconciliation** — completed 2026-07-06 (see Status /
  Open Questions).
- **New moving parts** — `Alerts` table, EventBridge bus/rules, and a Step Functions state
  machine, each with its own failure modes and tests.
- **Compliance ownership** — resolved (decision #5): owned by the web-app/feature team. Phase 0 uses
  `Alerts`-table TTL retention and no raw audio; audio/transcript retention and **Web Push consent**
  are Phase 1 (with ADR-005). ARCC reconciliation of the new IoT grants completed 2026-07-06.
- **Re-engagement when the app is closed** — resolved (decisions #2/#3): the always-on in-store
  display keeps the live WebSocket channel the norm, with the in-app inbox as the durable floor;
  Web Push is deferred to Phase 1.

### Neutral
- IoT transport (ADR-004), gateway, dedup model, and the simulator core are unaffected.
- Reasoning/compose reuses ADR-003 (text) and ADR-005 (voice); this ADR is about the
  *pipeline*, not the reasoner.

## Alternatives Considered

- **A. Poll-only (status quo).** The cloud periodically pulls `get_equipment_status`.
  Rejected as the proactive mechanism: not agent-initiated, adds latency and steady-state
  cost, and does not scale to a fleet.
- **B. Telephony / SMS alerts (Amazon Connect outbound, SNS SMS).** Rejected: **out of
  scope** (web-app-only). Also adds external egress and consent/compliance surface.
- **C. IoT Rule → SNS/email directly (no reasoner).** Rejected: bypasses the agent, so no
  policy (quiet hours, dedup, escalation), no contact resolution, and no spoken/conversa-
  tional follow-up. It is a dumb pipe, not an agent-initiated experience.
- **D. IoT Rule → Step Functions directly (drop EventBridge).** Considered. EventBridge is
  retained for a decoupled event bus (future fan-out: multiple consumers, archived events,
  schema registry) at the cost of one hop. Revisit if the extra hop proves unjustified.
- **E. Push the alert decisioning to the store.** Rejected: keeps store thin and the
  policy/escalation logic centralized in the cloud where the schedule/contact data lives.

## Resolved decisions

Baked into spec `023` and **validated by
Phase 0** (Task 9 GATE passed 2026-06-24).

**Product decisions (2026-06-24):**
- **#2 Re-engagement** — in-app inbox (floor) + live WebSocket push when connected; Web Push
  deferred to Phase 1.
- **#3 Deployment surface** — always-on in-store display only (not personal devices).
- **#4 Alert policy** — **no on-call to escalate to**: no Schedule-domain contact resolution and
  no multi-person chain. Retained: severity gating (warning+ notify, info inbox-only), quiet hours
  + critical override, dedup by (store, equipment, condition) within ~15 min, and an ack SLA
  (critical 5 min / warning 30 min), then **bounded re-surface on the same display**.
- **#5 Compliance** — owned by the web-app/feature team; `Alerts`-table TTL retention; no raw audio
  in Phase 0.

**Technical decisions (per the spec 023 design, as built):**
- `Alerts` schema + dedup key → `(store, domain, source, condition)` within a ~15-min window, GSI
  `DedupIndex`, an atomic window-bucketed conditional write.
- Step Functions → **Standard** (long `waitForTaskToken` SLA waits); ack resumes via
  `SendTaskSuccess`; no-ack re-surfaces up to `maxRenotify` then `expired`.
- Push channel → the **simulator's existing WebSocket**, fed by a cloud→store `notifications` relay
  (justified by #3); no managed cloud WebSocket in Phase 0. Web Push (VAPID) is Phase 1.

## Open Questions

- **ARCC reconciliation** for the new `events/*` / `notifications/*` IoT grants (least-privilege,
  topic-scoped) and event authenticity/validation — **RESOLVED 2026-07-06** (run via the `arcc` CLI;
  the `search_arcc` MCP tool was not loaded in-session). Grants judged compliant (X.509 mTLS minimum
  bar; per-thing least-privilege; SAX-04 envelope validation); the one Cat A gap — handlers trusted
  the payload `store_id` over the authenticated topic (cross-tenant injection/suppression) — was
  fixed, deployed, and live-validated. See the
  validated finding
  and todo `2026-06-28-arcc-reconcile-proactive-iot-grants`.
- **Phase 1**: Web Push consent and audio/transcript retention (with [ADR-005](ADR-005-nova-sonic-voice-interface.md)).

## Relationship to prior decisions

- **ADR-004 (IoT Core transport):** Reused and **additively extended** with the
  `events/*` topic + grant; the `responses` Rule→Lambda pattern is the template.
- **ADR-005 (Nova Sonic voice):** Provides spoken delivery + conversational follow-up;
  ADR-006's Phase 0 foundation is independent of it and can ship first.
- **ADR-003 (harness):** Reused for text composition/analytics.
- **ADR-001/ADR-002:** Unaffected (ADR-002 already superseded).

## References

- [Voice + proactive architecture proposal](../solution-guidance/voice-and-proactive-architecture-proposal.md) — primary design doc, Figure 1 (target architecture) and Figure 2 (alert-to-voice sequence).
- [Reference architecture](../solution-guidance/reference-architecture.md) — current-state baseline (IoT + harness).
- [ADR-004](ADR-004-iot-core-broker-migration.md) — the IoT transport + the `responses` Rule→Lambda pattern this mirrors.
- [ADR-005](ADR-005-nova-sonic-voice-interface.md) — the voice/compose engine.
- 2026-06-22 session handoff.

Note: external documentation content referenced here has been summarized and rephrased
for compliance with licensing restrictions.
