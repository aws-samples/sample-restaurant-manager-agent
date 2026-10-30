# infra/proactive-alerting

Cloud-side foundation for **agent-initiated proactive alerting** — spec
`023-proactive-alerting-foundation`
(Phase 0, engine-independent). See [ADR-006](../../docs/adr/ADR-006-proactive-alerting-layer.md).

## What this stack provisions

- **`Alerts` DynamoDB table** (system of record) + a `DedupIndex` GSI, on-demand billing, TTL.
- **`proactive-alerts` EventBridge bus** + a rule on `AlertRaised` that targets the state machine.
- **Step Functions** state machine (Standard) — the alert-policy orchestrator. **Phase 0 Task 1
  ships a pass-through skeleton**; the severity / quiet-hours / dedup / ack-SLA / bounded
  re-surface policy is filled in by spec 023 Task 5 / Task 8.
- **ingest Lambda** (IoT Topic Rule `stores/+/events/+`, excluding `events/ack`): validates the
  event, dedups, persists to `Alerts`, emits `AlertRaised`. **Task 1 ships a shell**; the logic is
  spec 023 Task 4.
- **ack Lambda** (IoT Topic Rule `stores/+/events/ack`): resolves the waiting execution's task
  token. **Task 1 ships a shell**; the logic is spec 023 Task 7.
- **events SQS dead-letter queue** for malformed events (IoT Rule error action + defensive ingest).
- Least-privilege IAM throughout (no `Resource:"*"`, no action wildcards), per the Recommendation
  Engine *Least Privilege Design* (AWS-61).

The additive IoT device-policy grants (`events/*` publish, `notifications/*` subscribe) live in
`infra/iot-core-broker` (spec 023 Task 2), not here.

## Deploy (us-west-2)

```bash
cd infra/proactive-alerting
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PATH=.venv/bin:$PATH cdk synth
PATH=.venv/bin:$PATH cdk deploy
```

The Lambdas are pure-Python (boto3 from the runtime); no container bundling is required.

## Test

```bash
.venv/bin/python -m pytest -q
```
