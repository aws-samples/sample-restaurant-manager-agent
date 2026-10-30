# Web Push re-engagement (spec 025 Task 1d)

Cloud half of Web Push delivery for the always-on display. **Additive** to the
spec-023 proactive pipeline: the in-app inbox stays the delivery floor, spoken
delivery is the connected path, and this push fires only for a
backgrounded/closed display.

## Components

- **PushSubscriptions** DynamoDB table — per-store browser subscriptions
  (`PK=STORE#{id}`, `SK=SUB#{endpoint-hash}`, TTL).
- **VAPID secret** (Secrets Manager) — the application-server private key +
  subject. The CDK creates the *resource*; the *value* is provisioned
  out-of-band (`provision/gen_vapid.py`), never in the template/source.
- **registrar** Lambda — IoT rule `stores/+/push-subscriptions`; upserts a
  subscription relayed by the store (idempotent on a stable endpoint hash).
- **push-sender** Lambda — IoT rule `stores/+/push-requests/+`; on a store
  re-engagement request (emitted only when the display is NOT connected), sends
  a VAPID Web Push to every subscription for the store and prunes 403 (VAPID key
  mismatch after rotation) / 404 / 410.
- **rotator** Lambda (spec 028) — EventBridge-scheduled (default 60 days) and
  on-demand; regenerates the VAPID keypair into the secret and mirrors the new
  **public** key to the SSM parameter `/restaurant-sim/web-push/vapid-public-key`.
- **VAPID public-key SSM parameter** (spec 028; non-secret) — the store backend
  reads the *current* public key from here so it survives rotation without a
  redeploy. The private key stays in the secret; the EC2 never reads the secret.

Least privilege: scoped DynamoDB + `secretsmanager:GetSecretValue` on the VAPID
secret ARN only; arm64; no `Resource:"*"`. The browser holds no AWS credential;
the VAPID private key never leaves the cloud.

## Deploy (us-west-2)

```bash
cd infra/web-push
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 1. Vendor the push-sender + rotator native deps for arm64 (one-time QEMU reg first on x86):
#    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
scripts/bundle.sh

# 2. Deploy the stack (creates the table, VAPID secret resource, Lambdas, rules,
#    rotation schedule).
PATH=.venv/bin:$PATH cdk deploy

# 3. Provision the VAPID keypair into the secret + the PUBLIC key into SSM.
.venv/bin/python provision/gen_vapid.py --subject mailto:ops@example.com --put \
    --secret-id restaurant-sim/web-push/vapid \
    --public-key-param /restaurant-sim/web-push/vapid-public-key --region us-west-2
#    The store backend reads the public key from the SSM param (spec 028), so it
#    stays current across rotation with no redeploy. `vapidPublicKey` on the EC2
#    stack is now only a local-dev/bootstrap fallback env.
```

## Store-side grants (additive, in `infra/iot-core-broker`)

The store publishes the browser subscription and (when the display is offline) a
re-engagement request over its outbound IoT connection, so the shared fleet
policy must allow `iot:Publish` on:

- `stores/${iot:Connection.Thing.ThingName}/push-subscriptions`
- `stores/${iot:Connection.Thing.ThingName}/push-requests/*`

These are **outside** the `events/` prefix, so the spec-023 ingest rule does not
match them.

## VAPID key rotation (spec 028)

Per ARCC "Implement strong secrets management" (AWS-59), the VAPID secret is
rotated **automatically on a schedule ≤90 days** (default **60**; context
`vapidRotationDays`) by the **rotator** Lambda, and can be rotated **on demand**.

**What happens on rotation (hard cutover):** the rotator writes a fresh keypair
to the secret and the new public key to the SSM parameter. Subscriptions bound to
the *previous* public key then fail with **403** and are pruned by the
push-sender. The store backend serves the new public key, and the display
**silently re-subscribes** with it on next load (`syncPushSubscription()` — no
re-consent). Because the always-on display's primary path is WebSocket/voice and
Web Push is only the backgrounded-tab fallback, the brief per-display
re-subscribe is acceptable.

**Runbook:**
- *Scheduled* — nothing to do; the EventBridge rule `web-push-vapid-rotation`
  invokes the rotator every `vapidRotationDays`.
- *On-demand / urgent* — invoke the rotator directly, then confirm:
  ```bash
  aws lambda invoke --function-name web-push-vapid-rotator --payload '{}' \
      --region us-west-2 /dev/stdout
  aws secretsmanager describe-secret --secret-id restaurant-sim/web-push/vapid \
      --region us-west-2 --query LastChangedDate
  aws ssm get-parameter --name /restaurant-sim/web-push/vapid-public-key \
      --region us-west-2 --query Parameter.Value
  ```
- *Compromise* — rotate on-demand as above; the old key is invalid immediately
  (old subs 403 → pruned). No secret value appears in source, the template,
  UserData, the browser, or logs (the rotator marker logs the public key only).

**Threat-model note (rotation mechanism, ARCC exit criteria):** the VAPID keypair
is a long-lived Web Push application-server signing key — not an AWS credential
(so it cannot be an IAM role) and not a Secrets Manager *Managed Rotation* type.
It is rotated by a scheduled custom Lambda (this package) with least-privilege IAM
(`GetSecretValue`+`PutSecretValue` on the secret, `PutParameter` on the public-key
param). The **private key is cloud-only**: only the rotator and push-sender read
the secret; the store/EC2 reads only the public-key SSM parameter
(`ssm:GetParameter` on that ARN, never the secret). Rotation is unit-tested in
`tests/test_rotator.py`.

## Tests

```bash
.venv/bin/python -m pytest -q   # 29: CDK assertions + registrar + push-sender + rotator (pywebpush mocked)
```
