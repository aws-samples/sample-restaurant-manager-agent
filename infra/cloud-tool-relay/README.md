# Cloud MCP Gateway (Gateway Lambdas + DynamoDB for Durable Agents)

A single AWS CDK stack that provisions the cloud half of the secure remote tool invocation architecture as it exists after the AgentCore / Fargate takedown in spec 016: **four MCP gateway Lambdas** (`pos-mcp-gateway`, `equipment-mcp-gateway`, `inventory-mcp-gateway`, `schedule-mcp-gateway`) and **two DynamoDB tables** (`CommandResults`, `StoreStateCache`).

The gateway Lambdas are invoked by the durable Lambda domain agents provisioned by specs 012 and 013. Each gateway publishes MCP commands to the store over the connection-less `iot-data:Publish` data plane (**AWS IoT Core**, topic `stores/{id}/commands/{domain}`) and writes a pending record to `CommandResults`; responses are resolved asynchronously by the `iot-response-handler` Lambda (`infra/iot-core-broker`, spec 020), which also reflects last-known state into `StoreStateCache` for graceful degradation. The **current agent-runtime story** — the durable supervisor and the four durable domain agents — lives under FEAT-009. Read FEAT-009 for the end-to-end flow; this stack only owns the shared gateway + DynamoDB layer.

> **Transport note (spec 020, 2026-06-18):** the gateway used to publish over MQTT to an Amazon MQ broker (spec 005) and responses were handled by the durable `mqtt-response-subscriber` (spec 011). Both were decommissioned; the gateway now publishes via `iot-data:Publish` to AWS IoT Core and there is no broker secret or `TRANSPORT` flag any more. See [ADR-004](../../docs/adr/ADR-004-iot-core-broker-migration.md).

**Spec:** 006-cloud-mcp-gateway-cdk (the agent-runtime portions are superseded; see the banner at the top of that spec's `design.md`).

---

## Prerequisites

1. **IoT Core response path deployed.** Deploy `infra/iot-core-broker` (spec 020) so the `iot-response-handler` Lambda + IoT Topic Rule (`stores/+/responses/+`) exist to resolve gateway responses. This stack no longer imports any Amazon MQ broker exports (the broker was decommissioned).

2. **CDK bootstrapped.** Run `cdk bootstrap aws://<ACCOUNT_ID>/us-west-2` if you haven't already.

3. **AWS credentials configured.** Your CLI credentials must have sufficient permissions to create Lambda functions, DynamoDB tables, and IAM roles.

4. **Region: `us-west-2`.** The stack is hard-pinned to `us-west-2` and refuses to deploy in any other region.

5. **Python 3.11+** for the CDK app and scripts. The local dev venv uses Python 3.12 per the project's steering guidance.

---

## Deploy

From the `infra/cloud-tool-relay/` directory:

```bash
# Activate the virtual environment (if not already)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Deploy
cdk deploy --outputs-file cdk-outputs.json
```

**Expected duration:** a few minutes on a first deploy. There are no Docker image builds in the post-takedown stack — just four Lambda functions and two DynamoDB tables. Subsequent deploys with no code changes are significantly faster.

Stack outputs are written to `cdk-outputs.json` in this directory. Retained outputs:

| Output | Description |
|--------|-------------|
| `PosGatewayLambdaArn` | ARN of the POS gateway Lambda |
| `EquipmentGatewayLambdaArn` | ARN of the equipment gateway Lambda |
| `InventoryGatewayLambdaArn` | ARN of the inventory gateway Lambda |
| `ScheduleGatewayLambdaArn` | ARN of the schedule gateway Lambda |
| `CommandResultsTableName` | DynamoDB table for correlation tracking |
| `StoreStateCacheTableName` | DynamoDB table for fallback cache |
| `Region` | `us-west-2` |

---

## Invoking the Gateway Lambdas

In production the gateway Lambdas are invoked by the durable Lambda domain agents (spec 012, spec 013). Each durable domain agent is itself invoked by the durable supervisor (spec 014) via `ctx.invoke` against the domain agent's `:live` alias. Browser-side prompts reach the supervisor through the FastAPI chat backend described in spec 015.

For local debugging, the four gateway Lambdas can be invoked directly with the AWS CLI — this is the usual way to prove a gateway can publish an IoT command in isolation from the durable-agent stack:

```bash
aws lambda invoke \
  --function-name equipment-mcp-gateway \
  --payload '{"tool":"get_equipment_status","store_id":"sim-store-001","args":{}}' \
  --cli-binary-format raw-in-base64-out \
  --region us-west-2 \
  --no-cli-pager \
  response.json
```

The invocation returns once the command has been published to IoT Core (`iot-data:Publish`) and a pending row written to `CommandResults`. The actual response lands asynchronously when the store replies on the correlated IoT topic (`stores/{id}/responses/{cid}`) and the `iot-response-handler` Lambda writes the result back into `CommandResults`.

> **Historical (removed in spec 016):**
>
> Prior to spec 016 the cloud-mcp-gateway stack also provisioned five Bedrock AgentCore Runtime agents (`supervisor_agent`, `pos_agent`, `equipment_agent`, `inventory_agent`, `schedule_agent`) and a Fargate response handler. The AgentCore agents exposed an `invoke_agent_runtime` entry point; the Fargate handler was an always-on MQTT subscriber. Both were replaced in FEAT-009 and torn down in spec 016. The former invocation examples are kept below as historical record only.
>
> **Option 1: AgentCore Console** — open the Bedrock AgentCore console, find the supervisor agent, and use the built-in test interface.
>
> **Option 2: `bedrock-agentcore` CLI**
>
> ```bash
> aws bedrock-agentcore invoke-agent-runtime \
>   --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/supervisor_agent-EXAMPLE123" \
>   --runtime-session-id "$(uuidgen)-manual-test" \
>   --payload '{"prompt": "What is the current equipment status at sim-store-001?"}' \
>   --region us-west-2
> ```
>
> **Option 3: Minimal Python harness**
>
> ```python
> import boto3, json, uuid
>
> client = boto3.client("bedrock-agentcore", region_name="us-west-2")
>
> SUPERVISOR_RUNTIME_ARN = (
>     "arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/"
>     "supervisor_agent-EXAMPLE123"
> )
>
> response = client.invoke_agent_runtime(
>     agentRuntimeArn=SUPERVISOR_RUNTIME_ARN,
>     runtimeSessionId=f"{uuid.uuid4()}-test",
>     payload=json.dumps({"prompt": "Give me a full status report for sim-store-001"}).encode(),
> )
>
> body = response.get("response", b"")
> if hasattr(body, "read"):
>     body = body.read()
> print(body.decode("utf-8") if isinstance(body, bytes) else body)
> ```

---

## Exercising the round-trip (against the IoT Core simulator)

The real store-side simulator (spec 004 EC2 instance, connected to **AWS IoT Core**
over mutual TLS) is what answers gateway commands. Invoke any gateway Lambda (see
above) and watch `CommandResults` in DynamoDB flip from `pending` to `complete` once
the simulator replies on `stores/{id}/responses/{cid}` and the `iot-response-handler`
Lambda (`infra/iot-core-broker`) resolves the record.

> **Removed (spec 020):** the old `scripts/mqtt_responder.py` and
> `scripts/local_responder_with_ddb.py` helpers were paho-based responders that
> connected to the now-decommissioned Amazon MQ broker. They were deleted in spec 020.
> To exercise the path without the EC2 simulator, run the simulator locally with
> `MQTT_ENABLED=true` + `MQTT_AUTH_MODE=mtls` pointed at the IoT Core endpoint (see the
> repo-root [`README.md`](../../README.md) MQTT bridge section).

### Observing Timeout Fallbacks

To observe the timeout and fallback behavior:

1. **With the simulator online**, invoke a gateway Lambda once against a store id (this populates `StoreStateCache` via the `iot-response-handler` write-through).
2. **Take the simulator offline** (stop the EC2 simulator service, or disconnect the local bridge).
3. **Invoke the durable domain agent again** with the same tool call. The agent will time out waiting on its `ctx.callback` and fall back to the cached data from step 1. The response will include a staleness disclaimer (e.g., "last known at X, may be stale").
4. **For a tool with no cache** (e.g., if `StoreStateCache` is empty for a domain), the agent returns an explicit "no data available" error.

> **Historical (removed in spec 016):**
>
> The spec used to ship an `e2e_test_final.py` script under `scripts/` that started its own embedded responder, invoked the supervisor via `invoke_agent_runtime`, and validated the full Fargate-era flow end to end. Both that script and `fan_out_concurrency_test.py` were removed in spec 016. End-to-end validation in the durable architecture now flows through `scripts/measure_integration.py` (spec 015) against the FastAPI `/api/chat` path; see spec 015's tasks.md for the invocation details.

---

## Debugging with DynamoDB

### Inspecting `CommandResults`

The `CommandResults` table tracks every tool invocation by `correlation_id`. Useful for debugging whether commands are being published and responses are being resolved.

```bash
# List recent items (scan — fine for dev)
aws dynamodb scan \
  --table-name CommandResults \
  --region us-west-2 \
  --max-items 10 \
  --no-cli-pager

# Look up a specific correlation ID
aws dynamodb get-item \
  --table-name CommandResults \
  --key '{"correlation_id": {"S": "<YOUR_CORRELATION_ID>"}}' \
  --region us-west-2 \
  --no-cli-pager
```

**Status values:**
- `pending` — command published, waiting for response
- `complete` — response received and resolved
- `error` — response received with an error status
- `failed` — gateway Lambda failed to publish (e.g., IoT Core `iot-data:Publish` error)

Items expire via TTL (`expires_at`) after 5 minutes, so stale pending records clean themselves up.

### Inspecting `StoreStateCache`

The `StoreStateCache` table holds the last-known-good result per domain. This is what durable domain agents fall back to on timeout.

```bash
# Check cached state for equipment domain
aws dynamodb get-item \
  --table-name StoreStateCache \
  --key '{"store_domain_key": {"S": "sim-store-001#equipment"}}' \
  --region us-west-2 \
  --no-cli-pager

# Scan all cached domains
aws dynamodb scan \
  --table-name StoreStateCache \
  --region us-west-2 \
  --no-cli-pager
```

Each item contains `last_known_state` (the cached result), `last_updated_at` (epoch seconds), and `last_correlation_id`.

---

## Iterating on Gateway Lambda Code

When you modify gateway Lambda code under `lambdas/`, `cdk deploy` rebuilds and redeploys the affected function. No container image builds are involved.

```bash
# Edit gateway code, e.g. lambdas/gateway/main.py or lambdas/shared/*.py

# Redeploy — CDK detects the code change and updates the function
cdk deploy --outputs-file cdk-outputs.json
```

A redeploy with no code changes produces no resource updates (idempotent).

---

## CloudWatch Logs

| Component | Log Group |
|-----------|-----------|
| Gateway Lambdas | `/aws/lambda/pos-mcp-gateway`, `/aws/lambda/equipment-mcp-gateway`, `/aws/lambda/inventory-mcp-gateway`, `/aws/lambda/schedule-mcp-gateway` |
| Durable domain + supervisor agents | See specs 012, 013, 014 |
| IoT response handler | `/aws/lambda/iot-response-handler` — see [`infra/iot-core-broker`](../iot-core-broker/) (spec 020) |

All gateway log groups have 14-day retention.

**Useful things to look for:**
- Gateway Lambda logs: correlation IDs, IoT publish confirmations, errors.
- Durable agent logs: `ctx.callback` register / resolve events, fallback triggers.

**Important:** No long-lived credentials are involved on the gateway path — it publishes via the connection-less `iot-data:Publish` data plane (IAM-authorized, no broker secret).

---

## Cost Breakdown

Approximate steady-state monthly cost in a single-user dev environment (`us-west-2`), excluding Bedrock per-token charges, for the **post-takedown** stack:

| Line Item | Estimated Monthly Cost |
|-----------|----------------------|
| CloudWatch Logs (gateway log groups, 14-day retention) | ~$1 |
| DynamoDB on-demand (both tables, dev volume) | negligible |
| Lambda invocations (gateway Lambdas, ad-hoc use) | negligible |
| **Total infrastructure owned by this stack (pre-Bedrock)** | **~$1–2/month** |

Bedrock Claude Sonnet 4 charges (incurred by the durable supervisor and domain agents in their own stacks) are per-token and come on top. For occasional demo invocations, expect single-digit dollars per month across the whole durable path.

---

## Destroy

```bash
cd infra/cloud-tool-relay
cdk destroy
```

**Expected duration:** a few minutes. The teardown removes the gateway Lambdas, their IAM roles, and both DynamoDB tables (data is lost).

**What gets cleaned up:**
- All four gateway Lambda functions and their IAM roles
- Both DynamoDB tables (`CommandResults` and `StoreStateCache`) — data is lost
- Gateway CloudWatch log groups

**What is NOT affected:**
- The IoT Core response path (`infra/iot-core-broker`, spec 020 — separate stack)
- The simulator from spec 004 (separate stack)
- The durable domain agents / durable supervisor (separate stacks from FEAT-009)
- Any resources outside this stack

**Verification after destroy:**

```bash
# Confirm no orphaned resources with the spec tag
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Spec,Values=006-cloud-mcp-gateway-cdk \
  --region us-west-2 \
  --no-cli-pager
```

This should return an empty resource list.

---

## Architecture Overview

```
User → FastAPI /api/chat (EC2 + ALB, Cognito-authenticated)
         └→ lambda.invoke(Event) → Durable Supervisor (:live alias)
              ├→ ctx.invoke → Durable POS Agent → pos-mcp-gateway (Lambda) → iot-data:Publish → IoT Core → Store (mTLS)
              ├→ ctx.invoke → Durable Equipment Agent → equipment-mcp-gateway (Lambda) → iot-data:Publish → IoT Core → Store (mTLS)
              ├→ ctx.invoke → Durable Inventory Agent → inventory-mcp-gateway (Lambda) → iot-data:Publish → IoT Core → Store (mTLS)
              └→ ctx.invoke → Durable Schedule Agent → schedule-mcp-gateway (Lambda) → iot-data:Publish → IoT Core → Store (mTLS)

Store → IoT Core responses → IoT Topic Rule (stores/+/responses/+) → iot-response-handler (Lambda) → CommandResults (DynamoDB)
                                                                                                    → StoreStateCache (DynamoDB)

Durable domain agents suspend via ctx.callback keyed on CommandResults.callback_id,
and fall back to StoreStateCache on timeout.
```

This stack owns the **gateway Lambdas** and the **two DynamoDB tables** in the middle of that diagram. Everything else belongs to a sibling stack — see FEAT-009 for the cross-stack map.

For the full architecture diagrams of the original FEAT-006 design (historical), see the design document — which is now prefaced by a supersession banner pointing at FEAT-009 and spec 016.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `cdk deploy` fails / responses never resolve | IoT response path not deployed | Deploy `infra/iot-core-broker` (spec 020) — the `iot-response-handler` + Topic Rule must exist |
| Tool calls always time out | Store/simulator offline, or not connected to IoT Core | Confirm the simulator is connected over mTLS (`stores/{id}/commands/+`) and the IoT Topic Rule is enabled |
| `CommandResults` shows `status=failed` | Gateway Lambda `iot-data:Publish` failed | Check the gateway role's `iot:Publish` grant and the Lambda logs |
| Stale data in responses | Store was offline; agent fell back to `StoreStateCache` | Expected behavior — bring the store back online for live data |
