#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# User-data bootstrap for the restaurant simulator instance (spec 004, task 4.1).
#
# Placeholders (rendered by CDK before upload, see infrastructure/...):
#   __REPO_S3_URI__                       s3://bucket/path/to/repo.zip (source bundle)
#   __IOT_CERT_SECRET_ID__                Secrets Manager id of the store's IoT X.509
#                                         device certificate bundle (spec 020 / ADR-004)
#   __REGION__                            aws region (us-west-2)
#   __AGENTCORE_MEMORY_ID__               AgentCore Memory id backing /api/chat/history
#                                         (spec 008); empty string if unset.
#
# The script is intentionally idempotent — running it again on a replacement
# instance produces the same result. Failures are visible in CloudWatch under
# the ``/simulator/user-data`` log group (see cw-agent-config.json).
set -euxo pipefail

# Redirect all bootstrap stdout/stderr to /var/log/user-data.log *and* the
# console; the CW Agent tails the file (see cw-agent-config.json).
exec > >(tee -a /var/log/user-data.log) 2>&1

echo "=== $(date -Iseconds) user-data starting ==="

REGION="__REGION__"
REPO_S3_URI="__REPO_S3_URI__"
IOT_CERT_SECRET_ID="__IOT_CERT_SECRET_ID__"
AGENTCORE_MEMORY_ID="__AGENTCORE_MEMORY_ID__"
# Spec 022: harness wiring (the only chat backend since Task 6).
HARNESS_ARN="__HARNESS_ARN__"
IOT_ENDPOINT="__IOT_ENDPOINT__"
# spec 025: voice edge (Nova Sonic) flag + Web Push application-server public key.
VOICE_ENABLED="__VOICE_ENABLED__"
VAPID_PUBLIC_KEY="__VAPID_PUBLIC_KEY__"
# spec 028: SSM param the store reads for the CURRENT public key (survives rotation).
VAPID_PUBLIC_KEY_PARAM="__VAPID_PUBLIC_KEY_PARAM__"
# spec 026: StoreStateCache last-known-state read fallback (empty table => OFF).
STORE_STATE_CACHE_TABLE="__STORE_STATE_CACHE_TABLE__"
TOOL_READ_CACHE_ENABLED="__TOOL_READ_CACHE_ENABLED__"
# AGENTPERF06 #4: per-tool CloudWatch metrics namespace (empty => emission off).
TOOL_METRICS_NAMESPACE="__TOOL_METRICS_NAMESPACE__"
INSTALL_DIR="/opt/restaurant-sim"
SERVICE_USER="simulator"

# ----------------------------------------------------------------------------
# dnf resilience: on first boot, cloud-init's package refresh (and the
# NodeSource setup) can hold the rpm transaction lock, which made an unguarded
# `dnf install` abort the whole bootstrap with "can't create transaction lock
# on /var/lib/rpm/.rpm.lock (Resource temporarily unavailable)" -> GPG check
# FAILED. Retry dnf with backoff so a transient lock no longer aborts boot.
# ----------------------------------------------------------------------------
dnf_retry() {
  local n=0
  until [ "$n" -ge 6 ]; do
    if dnf "$@"; then return 0; fi
    n=$((n + 1))
    echo "dnf $* failed (attempt $n/6); waiting 15s for the rpm lock to clear..."
    sleep 15
  done
  echo "dnf $* failed after 6 attempts" >&2
  return 1
}

# ----------------------------------------------------------------------------
# 1. System packages: Python 3.12, git, awscli. Amazon Linux 2023 ships
#    Python 3.9 by default; python3.12 (in the standard repos) is required by
#    the Nova Sonic voice SDK (aws-sdk-bedrock-runtime needs Python >=3.12).
# ----------------------------------------------------------------------------
dnf_retry update -y
dnf_retry install -y python3.12 python3.12-pip git tar gzip unzip jq amazon-cloudwatch-agent

# ----------------------------------------------------------------------------
# 2. Node.js 20 for the frontend build. NodeSource's setup script is the
#    standard on Amazon Linux 2023; the ARM64 RPM is published for it. Wrapped
#    in a retry so the rpm-lock race above cannot abort the bootstrap.
# ----------------------------------------------------------------------------
install_nodejs() {
  curl -fsSL https://rpm.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh
  bash /tmp/nodesource_setup.sh
  dnf_retry install -y nodejs
}
install_nodejs || { echo "node install failed; retrying once after 20s..."; sleep 20; install_nodejs; }

# ----------------------------------------------------------------------------
# 3. Create a dedicated service user so the simulator does not run as root.
# ----------------------------------------------------------------------------
id -u "$SERVICE_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"

# ----------------------------------------------------------------------------
# 4. Fetch the source tarball from S3 (the repo is private; CDK uploads it
#    as an Asset and passes the S3 URI in via __REPO_S3_URI__). This
#    avoids needing git credentials on the instance.
# ----------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# CDK Asset bundles are ZIPs; extract them in place.
SRC_TMP="$(mktemp -d)"
aws s3 cp --region "$REGION" "$REPO_S3_URI" "$SRC_TMP/source.zip"
unzip -q "$SRC_TMP/source.zip" -d "$INSTALL_DIR"
rm -rf "$SRC_TMP"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ----------------------------------------------------------------------------
# 5. Python venv + install the simulator package.
# ----------------------------------------------------------------------------
sudo -u "$SERVICE_USER" python3.12 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
# Install the simulator + the voice-edge extra (spec 025): the experimental
# aws-sdk-bedrock-runtime exposes the Nova Sonic bidirectional-stream API
# (stock boto3 lacks it) and pulls in the arm64 awscrt wheel natively on this
# ARM64 instance. Fall back to the base install if the extra cannot resolve so
# a voice-dep hiccup never aborts the bootstrap.
#
# `-c constraints.txt` pins every version (transitive included) to a set proven
# on a live instance. This venv is rebuilt from scratch on every instance
# replacement, and pyproject.toml only declares open lower bounds, so WITHOUT
# the constraints a deploy silently installs whatever is newest that day. On
# 2026-07-31 that resolved mcp to 2.0.0, which moved `mcp.server.fastmcp`; the
# app crash-looped on import while CloudFormation still said UPDATE_COMPLETE.
# See constraints.txt for how to refresh it deliberately.
PIP_CONSTRAINT_ARGS=()
if [ -f "$INSTALL_DIR/constraints.txt" ]; then
  PIP_CONSTRAINT_ARGS=(-c "$INSTALL_DIR/constraints.txt")
  echo "using pinned constraints: $INSTALL_DIR/constraints.txt"
else
  # Loud, because an unconstrained install is how the 2026-07-31 outage happened.
  echo "WARNING: constraints.txt not found - installing UNPINNED dependencies" >&2
fi
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[voice]" "${PIP_CONSTRAINT_ARGS[@]}" \
  || sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR" "${PIP_CONSTRAINT_ARGS[@]}"

# ----------------------------------------------------------------------------
# 6. Frontend build. Vite writes to frontend/dist which the FastAPI app
#    serves at ``/`` when FRONTEND_DIST_DIR resolves to an existing dir.
# ----------------------------------------------------------------------------
sudo -u "$SERVICE_USER" bash -c "cd '$INSTALL_DIR/frontend' && npm ci && npm run build"

# ----------------------------------------------------------------------------
# 7. Environment file for the systemd unit. MQTT is disabled by default
#    (R12.1). Operators enable MQTT by populating the Secrets Manager
#    secret and flipping MQTT_ENABLED to true (R8.4, R12.3) — no redeploy.
#
#    Chat-feature env vars (AGENTCORE_MEMORY_ID, HARNESS_ARN) are rendered
#    from CDK context per specs 015/022; an empty value is emitted
#    when the context is unset so the FastAPI backend logs its
#    "not configured" warning rather than crashing. AWS_REGION is always
#    set so the backend doesn't need to guess the region on a no-IMDS path.
# ----------------------------------------------------------------------------
mkdir -p /etc/restaurant-sim /etc/restaurant-sim/certs
# MQTT config (spec 020 / ADR-004): the transport is AWS IoT Core over X.509 mTLS.
# The single branch below enables it when the store's device certificate bundle
# resolves from Secrets Manager; if it does not, MQTT stays DISABLED (R12.1) --
# that is the unprovisioned-store fallback, and config.py's validator
# short-circuits on `enabled=False`, so leaving the auth fields unset is safe.
#
# There is no Amazon MQ username/password fallback any more (R12.4, 2026-09-13):
# the broker was decommissioned in spec 020, so the branch that read those
# credentials was unreachable and has been removed along with its secret.
MQTT_ENABLED_VALUE="false"
MQTT_AUTH_MODE="userpass"
MQTT_BROKER_ENDPOINT=""
MQTT_CLIENT_CERT_PATH=""
MQTT_CLIENT_KEY_PATH=""
MQTT_CA_BUNDLE_PATH=""
# Transport port + ALPN. Default to the classic 8883 path here; the IoT (mtls)
# branch below flips these to 443 + ALPN (x-amzn-mqtt-ca).
MQTT_BROKER_PORT_VALUE="8883"
MQTT_USE_ALPN_VALUE="false"

if [ -n "$IOT_CERT_SECRET_ID" ]; then
    # Tracing (set -x) would print IOT_JSON -- including the device private
    # key -- to /var/log/user-data.log, which the CloudWatch Agent ships to
    # CloudWatch Logs. Keep it off while the secret is in scope.
    set +x
    # spec 020 mtls path: fetch the IoT device cert bundle and write cert files.
    IOT_JSON=$(aws secretsmanager get-secret-value \
        --secret-id "$IOT_CERT_SECRET_ID" --region "$REGION" \
        --query SecretString --output text 2>/dev/null || true)
    if [ -n "$IOT_JSON" ]; then
        MQTT_BROKER_ENDPOINT=$(echo "$IOT_JSON" | jq -r '.endpoint // ""')
        echo "$IOT_JSON" | jq -r '.certPem // ""'    > /etc/restaurant-sim/certs/device.pem.crt
        echo "$IOT_JSON" | jq -r '.privateKey // ""' > /etc/restaurant-sim/certs/device.private.key
        echo "$IOT_JSON" | jq -r '.caPem // ""'      > /etc/restaurant-sim/certs/AmazonRootCA1.pem
        chown -R "$SERVICE_USER:$SERVICE_USER" /etc/restaurant-sim/certs
        chmod 0640 /etc/restaurant-sim/certs/*
        MQTT_CLIENT_CERT_PATH=/etc/restaurant-sim/certs/device.pem.crt
        MQTT_CLIENT_KEY_PATH=/etc/restaurant-sim/certs/device.private.key
        MQTT_CA_BUNDLE_PATH=/etc/restaurant-sim/certs/AmazonRootCA1.pem
        MQTT_AUTH_MODE="mtls"
        # Default the deployed IoT store to MQTT-over-TLS on 443 via ALPN
        # (x-amzn-mqtt-ca), reusing the outbound-HTTPS path so no separate 8883
        # firewall rule is needed (validated 2026-07-08). Override per-instance
        # by editing /etc/restaurant-sim/env (MQTT_BROKER_PORT=8883 +
        # MQTT_USE_ALPN=false) and restarting — e.g. behind a TLS-inspecting/DPI
        # proxy that only forwards real HTTP on 443 and blocks ALPN-tunneled MQTT.
        MQTT_BROKER_PORT_VALUE="443"
        MQTT_USE_ALPN_VALUE="true"
        if [ -n "$MQTT_BROKER_ENDPOINT" ]; then MQTT_ENABLED_VALUE="true"; fi
    fi
    unset IOT_JSON
    set -x
fi
# R12.4 (2026-09-13): there is exactly ONE transport-credential branch above.
#
# A second branch used to follow it, reading Amazon MQ username/password from a
# placeholder secret. It was UNREACHABLE: the IoT cert secret id is rendered from a
# CDK context value with a non-empty default, so the guard above was permanently
# true. It was dead code that still justified a live secretsmanager:GetSecretValue
# grant on the EC2 role -- both removed together with the secret itself, along with
# the stack output that exported it (spec 004 R8.1, R6.3, R10.6).
#
# This removed the branch from USER-DATA only. The userpass auth mode is still
# supported in simulator/mqtt/client.py for local broker experiments; that is a
# different consumer and is deliberately untouched.
cat > /etc/restaurant-sim/env <<EOF
# Managed by user-data — edits survive reboots but are overwritten on
# instance replacement. See specs 004, 008, 015, and 020 for details.
MQTT_ENABLED=$MQTT_ENABLED_VALUE
MQTT_AUTH_MODE=$MQTT_AUTH_MODE
MQTT_BROKER_ENDPOINT=$MQTT_BROKER_ENDPOINT
MQTT_BROKER_PORT=$MQTT_BROKER_PORT_VALUE
MQTT_USE_ALPN=$MQTT_USE_ALPN_VALUE
MQTT_CLIENT_CERT_PATH=$MQTT_CLIENT_CERT_PATH
MQTT_CLIENT_KEY_PATH=$MQTT_CLIENT_KEY_PATH
MQTT_CA_BUNDLE_PATH=$MQTT_CA_BUNDLE_PATH
STORE_ID=sim-store-001
# Tell FastAPI where the Vite build output lives (simulator/api/app.py).
FRONTEND_DIST_DIR=$INSTALL_DIR/frontend/dist
PYTHONUNBUFFERED=1
# Chat UI: AgentCore Memory id + region.
AGENTCORE_MEMORY_ID=$AGENTCORE_MEMORY_ID
AWS_REGION=$REGION
# Agent backend (spec 022): the AgentCore Managed Harness on the inline tool
# path. This is the only backend -- the durable supervisor was decommissioned
# in Task 6, so there is no AGENT_BACKEND selector to flip.
HARNESS_ARN=$HARNESS_ARN
IOT_ENDPOINT=$IOT_ENDPOINT
# Voice edge (spec 025): Nova Sonic display voice flag + Web Push public key.
VOICE_ENABLED=$VOICE_ENABLED
VAPID_PUBLIC_KEY=$VAPID_PUBLIC_KEY
VAPID_PUBLIC_KEY_PARAM=$VAPID_PUBLIC_KEY_PARAM
# Tool read-cache (spec 026): last-known-state fallback on read timeout. Empty
# table name / "false" disables it (production default).
STORE_STATE_CACHE_TABLE=$STORE_STATE_CACHE_TABLE
TOOL_READ_CACHE_ENABLED=$TOOL_READ_CACHE_ENABLED
# Per-tool CloudWatch metrics (AGENTPERF06 #4): namespace for emission (empty
# disables it). The EC2 role grants PutMetricData on RestaurantSim/*.
TOOL_METRICS_NAMESPACE=$TOOL_METRICS_NAMESPACE
EOF
chmod 0640 /etc/restaurant-sim/env
chown root:"$SERVICE_USER" /etc/restaurant-sim/env

# ----------------------------------------------------------------------------
# 8. systemd unit (copied verbatim from scripts/restaurant-sim.service).
#    See task 4.2 for the unit definition.
# ----------------------------------------------------------------------------
cp "$INSTALL_DIR/infra/simulator-ec2-alb/scripts/restaurant-sim.service" /etc/systemd/system/restaurant-sim.service

# ----------------------------------------------------------------------------
# 9. Ensure /var/log/restaurant-sim.log exists and is writable by the
#    simulator service user. The systemd unit uses StandardOutput=append:
#    (and StandardError=append:) to this file, so the app's output goes ONLY
#    to the file — NOT to the journal. `journalctl -u restaurant-sim` shows
#    only systemd's own unit-lifecycle lines; read /var/log/restaurant-sim.log
#    for app logs. The CloudWatch Agent tails this file into the
#    /simulator/app log group (see cw-agent-config.json).
# ----------------------------------------------------------------------------
touch /var/log/restaurant-sim.log
chown "$SERVICE_USER:$SERVICE_USER" /var/log/restaurant-sim.log
chmod 0640 /var/log/restaurant-sim.log

# rsyslog is intentionally not configured here — with StandardOutput=append:
# in the systemd unit the service writes directly to the flat file, so there
# is no need to pipe through rsyslog from the journal. This keeps the log
# pipeline simple and deterministic.

# ----------------------------------------------------------------------------
# 10. CloudWatch Agent — config is shipped as part of the repo (task 4.3).
# ----------------------------------------------------------------------------
cp "$INSTALL_DIR/infra/simulator-ec2-alb/scripts/cw-agent-config.json" \
   /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# ----------------------------------------------------------------------------
# 11. Start the simulator.
# ----------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable --now restaurant-sim

echo "=== $(date -Iseconds) user-data finished ==="
