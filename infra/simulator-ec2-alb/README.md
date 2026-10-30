# Simulator on EC2 + ALB (CDK)

Single-command AWS deploy for the restaurant simulator. Provisions a VPC, one ARM64 EC2 instance running the simulator behind an **internal** Application Load Balancer fronted by **Amazon CloudFront**, with **Cognito authentication enforced at the edge** by a Lambda@Edge (`cognito-at-edge`) function. See spec 004 for the base EC2/ALB stack and spec 029 for the CloudFront edge-auth design, which **supersedes** spec 007's ALB-terminated Cognito auth (no domain, no Route 53, no ACM).

Access the dashboard at the **CloudFront URL** (`https://<CloudFrontDomainName>/`, a stack output); unauthenticated visitors are redirected to the Cognito Hosted UI. The internal ALB has no public entry point — it is reachable only through CloudFront via a VPC origin.

## Prerequisites

- AWS credentials pointing at your dev account, with permission to create VPCs, EC2 instances, ALBs, CloudFront distributions, Lambda@Edge functions, Cognito user pools, IAM roles, Secrets Manager secrets, and CloudWatch log groups.
- Python 3.12 on your workstation (use a 3.12 virtualenv; the system Python on Amazon Linux 2023 is 3.9).
- Node 20+ and npm — required to vendor the edge-auth Lambda's dependencies (`lambdas/edge-auth/bundle.sh`) before deploy, and for the CDK CLI.
- Both `us-west-2` **and** `us-east-1` bootstrapped for CDK v2 (`us-east-1` hosts the Lambda@Edge support stack): `cdk bootstrap aws://<ACCOUNT_ID>/us-west-2 aws://<ACCOUNT_ID>/us-east-1`.

## Install and synth

From this directory:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PATH=.venv/bin:$PATH cdk synth --quiet
```

> **Name the stack on every `cdk deploy` / `diff` / `destroy`.** This app synthesizes
> **two** stacks — `SimulatorEc2AlbStack` and the `us-east-1`
> `edge-lambda-stack-*` support stack that CDK adds for the Lambda@Edge
> authenticator — so a bare command fails with *"Since this app includes more than
> a single stack, specify which stacks to use."* `cdk synth` is the exception: it
> synthesizes all stacks and needs no argument.

Tests:

```bash
PATH=.venv/bin:$PATH pytest tests/ -v
```

## Deploy

### 1. Vendor the edge-auth Lambda dependencies

```bash
lambdas/edge-auth/bundle.sh          # npm ci --omit=dev (pure JS; no container)
```

### 2. Two-phase bootstrap (first deploy only)

Lambda@Edge forbids environment variables, so the Cognito IDs + CSRF secret are injected into the edge function's bundled config **at synth** from CDK context (decision D3-A). Because those IDs don't exist until Cognito is first created, the initial rollout is two phases:

**Phase 1 — create everything except the edge association** (empty `edgeCognito*` context).
Note the three IDs are **pinned in `cdk.json`** for the account this was developed
against, so a fresh clone is already in the phase-2 shape; to genuinely run phase 1
on a new account you must blank them explicitly:

```bash
PATH=.venv/bin:$PATH cdk deploy SimulatorEc2AlbStack \
  -c edgeCognitoUserPoolId= \
  -c edgeCognitoAppClientId= \
  -c edgeCognitoUserPoolDomain=
```

With those empty, no edge function is synthesized, so `SimulatorEc2AlbStack` is the
only stack in the app and the stack name is optional here — it is shown for
consistency with every other command on this page.

The distribution comes up **without** auth (no edge function associated yet). Capture the Cognito IDs it created:

```bash
aws cloudformation describe-stacks --stack-name SimulatorEc2AlbStack \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'||OutputKey=='CognitoAppClientId'].[OutputKey,OutputValue]" --output text
# Hosted UI domain prefix defaults to restaurant-sim-<ACCOUNT_ID>
#   => <prefix>.auth.us-west-2.amazoncognito.com
```

**Phase 2 — associate the edge authenticator** by passing the captured values as context (pin them in `cdk.json` or pass `-c`). Also set a strong `edgeCsrfNonceSecret` — it is kept out of git (written only into the gitignored `config.local.json` sidecar at synth):

```bash
PATH=.venv/bin:$PATH cdk deploy SimulatorEc2AlbStack \
  -c edgeCognitoUserPoolId=us-west-2_xxxxxxxxx \
  -c edgeCognitoAppClientId=xxxxxxxxxxxxxxxxxxxxxxxxxx \
  -c edgeCognitoUserPoolDomain=restaurant-sim-<ACCOUNT_ID>.auth.us-west-2.amazoncognito.com \
  -c edgeCsrfNonceSecret="$(openssl rand -hex 32)"
```

After the one-time bootstrap, **routine deploys need no `-c`** (but still need the
stack name — `cdk deploy SimulatorEc2AlbStack`): the three non-secret `edgeCognito*` IDs are pinned in `cdk.json`, and the CSRF secret persists in the gitignored `config.local.json` sidecar — the stack reuses it when `-c edgeCsrfNonceSecret` is absent, and **fails loud** rather than baking an empty secret. Deploying from a **fresh machine or CI** that has no sidecar requires passing `-c edgeCsrfNonceSecret=...` once (or copying the sidecar over), since the secret is intentionally never committed. Budget ~10–15 minutes on a cold account (longest leg is the EC2 user-data bootstrap) plus a few minutes for CloudFront + the Lambda@Edge replica to propagate.

On success the stack prints these outputs:

| Output | Consumer |
|---|---|
| `CloudFrontDomainName` | The distribution domain (`dXXXX.cloudfront.net`) |
| `CloudFrontUrl` | Browse to `https://<CloudFrontDomainName>/` — the public entry point (Cognito login enforced) |
| `CognitoUserPoolId` | Phase-2 context + auth debugging |
| `CognitoAppClientId` | Phase-2 context + auth debugging |
| `InstanceId` | `aws ssm start-session --target <InstanceId>` |
| `Region` | Always `us-west-2` |
| `VpcId` | (formerly consumed by the spec 005 Amazon MQ broker — decommissioned in spec 020) |
| `Ec2SecurityGroupId` | (formerly the spec 005 broker ingress rule — decommissioned in spec 020) |

> The internal ALB's DNS name is intentionally **not** a stack output (spec 029) — CloudFront is the entry point.
>
> `MqttSecretArn` was **removed 2026-09-13** (spec 004 R10.6) along with the placeholder
> `restaurant-sim/mqtt/credentials` secret it exported. That secret existed for the Amazon MQ
> `userpass` path retired in spec 020 / ADR-004; it was empty in every deployment and nothing read
> it, while still carrying a `secretsmanager:GetSecretValue` grant on the EC2 role. The deployed
> store authenticates to AWS IoT Core over mTLS using `restaurant-sim/iot/{store_id}/cert`, which is
> provisioned by [`infra/iot-core-broker`](../iot-core-broker/) and deliberately **not** owned by
> this stack, so `cdk destroy` here cannot revoke a store's identity. The `userpass` auth mode itself
> is still supported in `simulator/mqtt/client.py` for local broker experiments.

## Smoke test

```bash
CF=$(aws cloudformation describe-stacks --stack-name SimulatorEc2AlbStack \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" --output text)

# 1. Unauthenticated request is redirected to the Cognito Hosted UI (302).
curl -sS -o /dev/null -w "%{http_code}\n" "https://${CF}/"             # → 302 (edge auth)

# 2. The redirect Location points at the Cognito Hosted UI.
curl -sS -D - -o /dev/null "https://${CF}/" | grep -i '^location:'      # → .../oauth2/authorize?...

# 3. Full login + WebSocket (/ws) are validated interactively in a browser:
#    log in via the Hosted UI, then confirm live tick updates on the dashboard.
```

The internal ALB is not directly reachable — that is the point: all access flows through CloudFront and the Lambda@Edge Cognito gate.

## MQTT / store transport

A **provisioned** store connects automatically at boot — `MQTT_ENABLED=false` is the fallback for a
store with no certificate, not the steady state (spec 004 R12.1). User-data fetches the device
certificate bundle from Secrets Manager, writes it to `/etc/restaurant-sim/certs/`, and enables the
bridge over mTLS on port 443 with ALPN.

> **Superseded 2026-09-13.** This section previously told the operator to `put-secret-value` broker
> credentials into a placeholder secret via the `MqttSecretArn` output. Both the secret and the output
> were removed with the Amazon MQ transport (spec 020 / ADR-004), so those commands would now resolve
> an empty ARN and fail. There is no username/password path on the deployed instance any more.

**To connect a store that is not yet provisioned**, issue it an identity — this creates the IoT thing,
attaches the shared thing-variable policy, and stores the certificate bundle at
`restaurant-sim/iot/{store_id}/cert`:

```bash
# see infra/iot-core-broker/ for the full provisioning flow
python infra/iot-core-broker/provision/provision_store.py --store-id sim-store-001
```

Then replace or reboot the instance so user-data re-runs, or fetch the bundle and restart in place:

```bash
INSTANCE=$(aws cloudformation describe-stacks --stack-name SimulatorEc2AlbStack \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
aws ssm start-session --target "$INSTANCE"

# on the instance
sudo systemctl restart restaurant-sim
sudo tail -n 50 /var/log/restaurant-sim.log      # expect: mqtt connected ... port=443 alpn=...
```

Note the log path: the systemd unit sends stdout/stderr to `/var/log/restaurant-sim.log`, so
`journalctl -u restaurant-sim` carries only unit-lifecycle lines and **will not** show the connect
message. The CloudWatch Agent tails the same file into the `/simulator/app` log group.

Rotating or re-issuing a certificate needs **no `cdk deploy`** — the instance resolves the secret by
id at boot (spec 004 R8.4). To point the stack at a different secret, set the `iotCertSecretId`
context value.

## Destroy

```bash
PATH=.venv/bin:$PATH cdk destroy SimulatorEc2AlbStack
```

Removes every resource created by the stack. `cdk destroy` handles the two log groups because they are set to `RemovalPolicy.DESTROY` (non-prod posture). Bootstrap assets uploaded to the `cdk-*` staging bucket are left in place; that bucket is shared across stacks in the account.

> **The store's IoT certificate secret deliberately survives `cdk destroy`.** Since 2026-09-13 this
> stack creates no Secrets Manager secret at all (spec 004 R8.1, AC7.2); `restaurant-sim/iot/{store_id}/cert`
> is owned by [`infra/iot-core-broker`](../iot-core-broker/) so that tearing down the hosting stack
> cannot revoke a store's identity. Deleting it is a separate, deliberate action — see the root
> README's teardown section.

> **Lambda@Edge deletion lag:** CloudFront replicates the edge function to edge locations and deletes those replicas asynchronously. `cdk destroy` may report that the edge function (in the `us-east-1` support stack) cannot be deleted for a period — often 15–60+ minutes — after the distribution is removed. This is expected: retry `cdk destroy` later, or delete the lingering `edge-lambda-stack-*` support stack once the replicas have drained.

## Project layout

```
infra/simulator-ec2-alb/
├── app.py                          # CDK app entrypoint (env=us-west-2)
├── cdk.json                        # CDK config + recommended feature flags
├── requirements.txt                # Runtime deps (aws-cdk-lib, constructs)
├── requirements-dev.txt            # Adds pytest
├── infrastructure/
│   └── simulator_ec2_alb_stack.py  # The one CloudFormation stack
├── lambdas/
│   └── edge-auth/                  # Lambda@Edge Cognito authenticator (spec 029; cognito-at-edge)
│       ├── index.js                # viewer-request handler
│       ├── config.json             # empty placeholder (real values injected at synth into gitignored config.local.json)
│       ├── bundle.sh               # vendors node_modules (npm ci --omit=dev)
│       └── index.test.js           # node --test unit test
├── scripts/
│   ├── user_data.sh                # First-boot bootstrap (installs Python, Node, builds frontend, starts systemd unit)
│   ├── restaurant-sim.service      # systemd unit copied to /etc/systemd/system/
│   └── cw-agent-config.json        # CloudWatch Agent file-tail config
└── tests/
    ├── conftest.py                 # sys.path shim
    └── test_stack.py               # CDK assertion tests (specs 004 / 026 / 029 + guards)
```

## Troubleshooting

- **Deploy hangs on the EC2 instance waiting for signal** — nothing does `cfn-signal`; the wait is the target-group health check coming up. Watch `/simulator/user-data` in CloudWatch Logs for bootstrap progress.
- **ALB target shows unhealthy** — user-data may still be running (`pip install -e .` and `npm ci` are the slowest steps). Give it 5 minutes; if still unhealthy, SSM in and check `systemctl status restaurant-sim` + `journalctl -u restaurant-sim`.
- **`curl http://<alb>/` returns 404** — means the simulator is up but `frontend/dist/` isn't on disk. Check `/var/log/user-data.log` for `npm run build` failure.
- **`cdk deploy` fails with `Current credentials could not be used to assume 'arn:aws:iam::...:role/cdk-*'`** — the account isn't bootstrapped in `us-west-2`. Run `cdk bootstrap aws://<account>/us-west-2`.
