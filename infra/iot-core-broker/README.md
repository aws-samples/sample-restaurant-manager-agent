# IoT Core Broker (spec 020, Phase A)

The connection-less cloud response path for the IoT Core transport migration
([ADR-004](../../docs/adr/ADR-004-iot-core-broker-migration.md), validated by the
019 spike).

**Phase A is additive** — this stack stands up the IoT response path *alongside* Amazon MQ.
Nothing cuts over until Phase B (gateway `iot-data:Publish` + simulator mTLS).

## What this deploys
- `iot-response-handler` Lambda — IoT Topic Rule target. Resolves the `CommandResults`
  row, resumes the waiting durable agent (`SendDurableExecutionCallbackSuccess`), and does
  the monotonic `StoreStateCache` write-through. Reuses the spec-011 dispatch contract;
  only the trigger changes (IoT rule, no MQTT session).
- `iot_response_rule` IoT Topic Rule on `stores/+/responses/+`.

The existing `CommandResults` / `StoreStateCache` tables are referenced via CDK **context**
(`commandResultsTableName` / `storeStateCacheTableName`), never re-created.

> Per-store IoT policy + thing/cert provisioning are **Task 2** (coupled with the R1.3
> client-id↔thing-name decision). Gateway publish swap is **Task 4**; simulator mTLS is
> **Task 5**.

## Offline tests
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## Deploy (additive; safe — does not cut over)
```bash
.venv/bin/pip install -r lambdas/response_handler/requirements.txt -t lambdas/response_handler --upgrade
CDK_DEFAULT_ACCOUNT=<ACCOUNT_ID> cdk deploy --require-approval never
```
`aws-cdk-lib` is pinned to `2.250.0` (has `DurableConfig`/`PYTHON_3_13`, synths with the
installed CDK CLI — do not float; see the 019 spike finding).
