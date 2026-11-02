#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One-command deployment of the Restaurant Manager Agent sample.
#
# Deploys every stack in dependency order (the same steps as the README's
# "Manual Deployment" section), creates the account-specific resources the
# stacks need (AgentCore Memory, the AgentCore harness, a store identity, the
# Web Push VAPID keys), runs the simulator's two-phase CloudFront edge-auth
# deploy, and validates the result.
#
# Safe to re-run: every step either skips work that is already done or
# updates it in place.
#
# Usage:
#   scripts/deploy.sh [--yes] [--skip-web-push] [--allow-bpa-exclusion]
#                     [--store-id ID] [--push-subject mailto:ADDR]
#
# Runs on macOS, Linux, and Windows via WSL2. Native Windows shells (Git Bash,
# PowerShell) are not supported: each stack's cdk.json runs .venv/bin/python.

set -euo pipefail

# ============================================
# CONFIGURATION
# ============================================
# Every stack and provisioning script in this repository is pinned to us-west-2.
readonly AWS_REGION="us-west-2"
# The simulator's Lambda@Edge authenticator is deployed to us-east-1 by CDK.
readonly EDGE_REGION="us-east-1"

STORE_ID="${STORE_ID:-sim-store-001}"
# Contact URI sent to browser push services with every Web Push message.
PUSH_SUBJECT="${PUSH_SUBJECT:-}"
SKIP_WEB_PUSH="${SKIP_WEB_PUSH:-false}"
ASSUME_YES="${ASSUME_YES:-false}"
ALLOW_BPA_EXCLUSION="${ALLOW_BPA_EXCLUSION:-false}"

# AgentCore Memory holds the chat history the dashboard reads back. Only
# short-term events are used, so no long-term memory strategies are configured.
readonly MEMORY_NAME="restaurant_manager_agent_memory"
readonly MEMORY_EVENT_EXPIRY_DAYS=30

readonly SIM_STACK="SimulatorEc2AlbStack"
readonly VAPID_SECRET_ID="restaurant-sim/web-push/vapid"
readonly VAPID_PUBLIC_KEY_PARAM="/restaurant-sim/web-push/vapid-public-key"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly INFRA="$REPO_ROOT/infra"

usage() {
    sed -n '/^# Usage:/,/^# PowerShell/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)        ASSUME_YES=true ;;
        --skip-web-push) SKIP_WEB_PUSH=true ;;
        --allow-bpa-exclusion) ALLOW_BPA_EXCLUSION=true ;;
        --store-id)      STORE_ID="${2:?--store-id needs a value}"; shift ;;
        --push-subject)  PUSH_SUBJECT="${2:?--push-subject needs a value}"; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

# ============================================
# HELPERS
# ============================================
step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    # confirm "question" -> returns 0 for yes. Defaults to yes.
    [ "$ASSUME_YES" = "true" ] && return 0
    local reply
    read -r -p "$1 [Y/n] " reply
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

stack_exists() {
    aws cloudformation describe-stacks --region "$2" --stack-name "$1" >/dev/null 2>&1
}

stack_output() {
    # stack_output STACK KEY -> prints the value, or nothing if absent.
    local value
    value="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text 2>/dev/null || true)"
    [ "$value" = "None" ] && value=""
    printf '%s' "$value"
}

setup_venv() {
    # Create (once) and refresh the per-stack virtualenv that cdk.json expects.
    local dir="$1"
    # Rebuild a venv that exists but can't run pip (for example, one left
    # behind by an interrupted or failed earlier run).
    if [ -e "$dir/.venv" ] && ! "$dir/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
        info "Removing broken virtualenv in ${dir#"$REPO_ROOT"/}"
        rm -rf "$dir/.venv"
    fi
    if [ ! -x "$dir/.venv/bin/python" ]; then
        info "Creating virtualenv in ${dir#"$REPO_ROOT"/}"
        "$PYTHON" -m venv "$dir/.venv"
    fi
    "$dir/.venv/bin/pip" install --quiet --upgrade pip
    "$dir/.venv/bin/pip" install --quiet -r "$dir/requirements-dev.txt"
}

save_context() {
    # save_context STACK_DIR key=value...
    # Merge account-specific values into the stack's gitignored cdk.context.json,
    # where the stacks and provisioning scripts look for them. Saving them there
    # (rather than passing -c each time) means later deploys and `cdk destroy`
    # work without re-supplying them.
    local dir="$1"; shift
    "$dir/.venv/bin/python" - "$dir/cdk.context.json" "$@" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    data[key] = value
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
}

cdk_deploy() {
    # cdk_deploy STACK_DIR [cdk args...]
    local dir="$1"; shift
    (cd "$dir" && PATH="$dir/.venv/bin:$PATH" cdk deploy --require-approval never "$@")
}

# ============================================
# PLATFORM DETECTION
# ============================================
case "$(uname -s)" in
    Darwin*) PLATFORM="macos" ;;
    Linux*)
        if grep -qi microsoft /proc/version 2>/dev/null; then PLATFORM="wsl"; else PLATFORM="linux"; fi ;;
    MINGW*|MSYS*|CYGWIN*)
        die "Native Windows shells are not supported. Run this script from WSL2." ;;
    *) die "Unsupported platform: $(uname -s)" ;;
esac
ARCH="$(uname -m)"

# ============================================
# PREREQUISITE CHECKS
# ============================================
step "Checking prerequisites (platform: $PLATFORM/$ARCH)"

require() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required. $2"
}
require aws     "Install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
require node    "Install Node.js 18 or later: https://nodejs.org/"
require npm     "npm ships with Node.js: https://nodejs.org/"
require cdk     "Install the AWS CDK CLI: npm install -g aws-cdk"
require git     "Install Git: https://git-scm.com/downloads"
require openssl "Install OpenSSL with your package manager."

# Python 3.12+ (the voice SDK and the stacks' lock files require it).
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3.12 python3.13 python3.14 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            PYTHON="$candidate"; break
        fi
    done
fi
[ -n "$PYTHON" ] || die "Python 3.12 or later is required (set PYTHON=/path/to/python3.12 to choose one)."
# Use the interpreter's real path. Some installers (uv, pyenv shims) put a
# symlink on the PATH; a venv built from that symlink can't find the standard
# library ("No module named 'encodings'") and ensurepip fails.
PYTHON="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
info "Python: $("$PYTHON" --version)"

if [ "$SKIP_WEB_PUSH" != "true" ]; then
    # A container CLI bundles the Web Push Lambda dependencies for arm64.
    # Shell aliases (for example docker -> finch) don't apply inside scripts,
    # so honour $DOCKER, then use the first CLI whose engine is running.
    if [ -n "${DOCKER:-}" ]; then
        command -v "$DOCKER" >/dev/null 2>&1 || die "DOCKER=$DOCKER was not found on the PATH."
        "$DOCKER" info >/dev/null 2>&1 || die "$DOCKER is installed but its engine isn't running. Start it, or re-run with --skip-web-push."
    else
        found_cli=""
        for candidate in docker finch podman; do
            command -v "$candidate" >/dev/null 2>&1 || continue
            found_cli="$candidate"
            if "$candidate" info >/dev/null 2>&1; then DOCKER="$candidate"; break; fi
        done
        if [ -z "${DOCKER:-}" ]; then
            [ -n "$found_cli" ] ||
                die "A container CLI (Docker, Finch, or Podman) is required to bundle the Web Push Lambdas. Install one, or re-run with --skip-web-push."
            die "Found a container CLI, but no engine is running (tried docker, finch, podman). Start one (for example 'finch vm start'), set DOCKER=<cli>, or re-run with --skip-web-push."
        fi
    fi
    export DOCKER
    info "Container CLI: $DOCKER"
fi

git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    die "Run this script from a git clone of the repository (the Lambda bundling scripts use git ls-files)."

aws sts get-caller-identity >/dev/null 2>&1 ||
    die "No working AWS credentials. Configure them (for example 'aws configure' or 'aws login') and re-run."
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"

aws bedrock-agentcore-control list-memories --region "$AWS_REGION" --max-results 1 >/dev/null 2>&1 ||
    die "Cannot call Amazon Bedrock AgentCore in $AWS_REGION. Update the AWS CLI to the latest v2 release and check your IAM permissions."

# VPC Block Public Access (BPA) blocks CloudFront VPC origins, which the
# simulator dashboard uses. If BPA is on, the simulator stack can create BPA
# exclusions for the ALB's private subnets, but only with explicit consent.
BPA_MODE="$(aws ec2 describe-vpc-block-public-access-options --region "$AWS_REGION" \
    --query VpcBlockPublicAccessOptions.InternetGatewayBlockMode --output text 2>/dev/null || echo unknown)"
BPA_EXCLUSIONS_ALLOWED="$(aws ec2 describe-vpc-block-public-access-options --region "$AWS_REGION" \
    --query VpcBlockPublicAccessOptions.ExclusionsAllowed --output text 2>/dev/null || echo unknown)"
case "$BPA_MODE" in
    off|None|unknown|"") BPA_ON=false ;;
    *) BPA_ON=true ;;
esac

# ============================================
# CONFIRMATION
# ============================================
step "Deployment plan"
info "Account:  $ACCOUNT_ID"
info "Identity: $CALLER_ARN"
info "Region:   $AWS_REGION (plus a Lambda@Edge support stack in $EDGE_REGION)"
info "Store ID: $STORE_ID"
info "Web Push: $([ "$SKIP_WEB_PUSH" = "true" ] && echo skipped || echo included)"
cat <<EOF

    This creates billable resources, including an EC2 instance, a CloudFront
    distribution, Lambda functions, DynamoDB tables, and Amazon Bedrock usage.
    It also creates IAM roles. Deploy into a non-production account.

    Before continuing, make sure that in $AWS_REGION:
      - Amazon Bedrock model access is enabled for the Anthropic Claude model in
        infra/harness-agent/cdk.json (bedrockModelId) and for Amazon Nova Sonic.
      - CloudWatch Transaction Search is enabled (needed for harness traces).

EOF
confirm "Deploy to account $ACCOUNT_ID?" || { echo "Cancelled."; exit 0; }

USE_BPA_EXCLUSION=false
if [ "$BPA_ON" = "true" ]; then
    [ "$BPA_EXCLUSIONS_ALLOWED" = "allowed" ] ||
        die "VPC Block Public Access is on in $AWS_REGION ($BPA_MODE) and exclusions are not allowed (likely set by your AWS Organization). CloudFront VPC origins can't work under BPA, so the simulator can't deploy here. Use another account, or ask your administrator."
    cat <<EOF

    VPC Block Public Access is on in $AWS_REGION (mode: $BPA_MODE). It blocks
    CloudFront VPC origins, which the simulator dashboard uses to reach its
    internal load balancer.

    To deploy, the simulator stack can add BPA exclusions (allow-bidirectional)
    for the load balancer's two PRIVATE subnets only. Those subnets have no
    route to the internet gateway, and the load balancer still accepts traffic
    only from CloudFront. The account's BPA setting and every other subnet
    stay as they are. The exclusions are deleted with the stack.

    Check that adding exclusions is allowed by whoever manages this account's
    security settings before you continue.

EOF
    if [ "$ALLOW_BPA_EXCLUSION" = "true" ]; then
        USE_BPA_EXCLUSION=true
    elif [ "$ASSUME_YES" = "true" ]; then
        die "Re-run with --allow-bpa-exclusion to create the exclusions non-interactively, or use an account without BPA."
    else
        read -r -p "Create BPA exclusions for the simulator's private subnets? [y/N] " reply
        case "$reply" in
            [yY]*) USE_BPA_EXCLUSION=true ;;
            *) die "Cancelled. The simulator can't deploy while BPA blocks CloudFront VPC origins." ;;
        esac
    fi
fi

if [ "$SKIP_WEB_PUSH" != "true" ] && [ -z "$PUSH_SUBJECT" ]; then
    if [ "$ASSUME_YES" = "true" ]; then
        PUSH_SUBJECT="mailto:ops@example.com"
    else
        read -r -p "Contact email for Web Push (sent to browser push services) [ops@example.com]: " push_email
        PUSH_SUBJECT="mailto:${push_email:-ops@example.com}"
    fi
fi

START_TIME=$(date +%s)

# ============================================
# CDK BOOTSTRAP
# ============================================
step "Checking CDK bootstrap in $AWS_REGION and $EDGE_REGION"
for region in "$AWS_REGION" "$EDGE_REGION"; do
    if stack_exists CDKToolkit "$region"; then
        info "$region already bootstrapped"
    else
        info "Bootstrapping $region"
        cdk bootstrap "aws://$ACCOUNT_ID/$region"
    fi
done

# ============================================
# 1. STORE TRANSPORT (AWS IoT Core)
# ============================================
step "1/7 Deploying the store transport (IotCoreBrokerStack)"
setup_venv "$INFRA/iot-core-broker"
cdk_deploy "$INFRA/iot-core-broker"

step "2/7 Provisioning the store identity for $STORE_ID"
if aws iot describe-thing --region "$AWS_REGION" --thing-name "$STORE_ID" >/dev/null 2>&1 &&
    aws secretsmanager describe-secret --region "$AWS_REGION" \
        --secret-id "restaurant-sim/iot/$STORE_ID/cert" >/dev/null 2>&1; then
    info "IoT thing and certificate secret already exist; skipping"
else
    (cd "$INFRA/iot-core-broker" && .venv/bin/python provision/provision_store.py --store-id "$STORE_ID")
fi
IOT_ENDPOINT="$(aws iot describe-endpoint --region "$AWS_REGION" --endpoint-type iot:Data-ATS \
    --query endpointAddress --output text)"
info "IoT endpoint: $IOT_ENDPOINT"

# ============================================
# 2. CLOUD TOOL RELAY
# ============================================
step "3/7 Deploying the cloud tool relay (CloudMcpGatewayStack)"
setup_venv "$INFRA/cloud-tool-relay"
cdk_deploy "$INFRA/cloud-tool-relay"

# ============================================
# 3. AGENTCORE MEMORY + HARNESS
# ============================================
step "4/7 Creating the AgentCore Memory resource"
# Prefer a memory already recorded for this checkout (it may predate this
# script and have a different name), then one this script created earlier.
MEMORY_ARN=""
HARNESS_CTX="$INFRA/harness-agent/cdk.context.json"
if [ -s "$HARNESS_CTX" ]; then
    saved_arn="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("memoryArn",""))' "$HARNESS_CTX" 2>/dev/null || true)"
    if [ -n "$saved_arn" ]; then
        saved_status="$(aws bedrock-agentcore-control get-memory --region "$AWS_REGION" \
            --memory-id "${saved_arn##*/}" --query memory.status --output text 2>/dev/null || true)"
        case "$saved_status" in ACTIVE|CREATING) MEMORY_ARN="$saved_arn" ;; esac
    fi
fi
if [ -z "$MEMORY_ARN" ]; then
    MEMORY_ARN="$(aws bedrock-agentcore-control list-memories --region "$AWS_REGION" \
        --query "memories[?starts_with(id, '${MEMORY_NAME}-') && status!='DELETING'] | [0].arn" \
        --output text)"
fi
if [ -n "$MEMORY_ARN" ] && [ "$MEMORY_ARN" != "None" ]; then
    info "Reusing existing memory: $MEMORY_ARN"
else
    MEMORY_ARN="$(aws bedrock-agentcore-control create-memory --region "$AWS_REGION" \
        --name "$MEMORY_NAME" \
        --description "Chat history for the Restaurant Manager Agent sample" \
        --event-expiry-duration "$MEMORY_EVENT_EXPIRY_DAYS" \
        --query memory.arn --output text)"
    info "Created memory: $MEMORY_ARN"
fi
MEMORY_ID="${MEMORY_ARN##*/}"
for _ in $(seq 1 60); do
    status="$(aws bedrock-agentcore-control get-memory --region "$AWS_REGION" \
        --memory-id "$MEMORY_ID" --query memory.status --output text)"
    case "$status" in
        ACTIVE) info "Memory is ACTIVE"; break ;;
        FAILED) die "Memory $MEMORY_ID failed to create. Check it with: aws bedrock-agentcore-control get-memory --region $AWS_REGION --memory-id $MEMORY_ID" ;;
        *) sleep 5 ;;
    esac
done
[ "$status" = "ACTIVE" ] || die "Timed out waiting for memory $MEMORY_ID to become ACTIVE."

step "5/7 Deploying the agent layer (HarnessAgentStack + AgentCore harness)"
setup_venv "$INFRA/harness-agent"
save_context "$INFRA/harness-agent" "memoryArn=$MEMORY_ARN"
cdk_deploy "$INFRA/harness-agent"
ROLE_ARN="$(stack_output HarnessAgentStack HarnessExecutionRoleArn)"
[ -n "$ROLE_ARN" ] || die "HarnessAgentStack has no HarnessExecutionRoleArn output."
(cd "$INFRA/harness-agent" && .venv/bin/python provision/provision_harness.py \
    --role-arn "$ROLE_ARN" --memory-arn "$MEMORY_ARN")
HARNESS_ARN_FILE="$INFRA/harness-agent/provision/.harness_arn"
[ -s "$HARNESS_ARN_FILE" ] || die "provision_harness.py did not record a harness ARN in $HARNESS_ARN_FILE."
HARNESS_ARN="$(cat "$HARNESS_ARN_FILE")"
info "Harness: $HARNESS_ARN"

# ============================================
# 4. PROACTIVE ALERTING
# ============================================
step "6/7 Deploying proactive alerting (ProactiveAlertingStack)"
setup_venv "$INFRA/proactive-alerting"
cdk_deploy "$INFRA/proactive-alerting"

# ============================================
# 5. WEB PUSH (optional)
# ============================================
if [ "$SKIP_WEB_PUSH" = "true" ]; then
    step "Skipping Web Push (WebPushStack)"
else
    step "Deploying Web Push (WebPushStack)"
    setup_venv "$INFRA/web-push"
    # The push-sender and rotator Lambdas ship native arm64 dependencies. On
    # x86_64 Linux, Docker needs QEMU registered once per boot to run arm64
    # build containers. Docker Desktop on macOS and Windows includes this.
    if [ "$ARCH" = "x86_64" ] && [ "$PLATFORM" = "linux" ]; then
        info "x86_64 Linux host: arm64 bundling needs QEMU registered with Docker (a privileged container)."
        if confirm "Register QEMU now?"; then
            "$DOCKER" run --rm --privileged multiarch/qemu-user-static --reset -p yes >/dev/null
        fi
    fi
    "$INFRA/web-push/scripts/bundle.sh"
    cdk_deploy "$INFRA/web-push"
    if aws ssm get-parameter --region "$AWS_REGION" --name "$VAPID_PUBLIC_KEY_PARAM" >/dev/null 2>&1; then
        # Re-generating would invalidate every existing browser subscription.
        info "VAPID keys already provisioned; skipping (the rotator Lambda keeps them current)"
    else
        (cd "$INFRA/web-push" && .venv/bin/python provision/gen_vapid.py \
            --subject "$PUSH_SUBJECT" --put \
            --secret-id "$VAPID_SECRET_ID" \
            --public-key-param "$VAPID_PUBLIC_KEY_PARAM" \
            --region "$AWS_REGION" >/dev/null)
        info "VAPID keys stored in $VAPID_SECRET_ID and $VAPID_PUBLIC_KEY_PARAM"
    fi
fi

# ============================================
# 6. STORE SIMULATOR (two-phase edge-auth deploy)
# ============================================
step "7/7 Deploying the store simulator ($SIM_STACK)"
SIM_DIR="$INFRA/simulator-ec2-alb"
setup_venv "$SIM_DIR"
"$SIM_DIR/lambdas/edge-auth/bundle.sh"

save_context "$SIM_DIR" \
    "harnessArn=$HARNESS_ARN" \
    "iotEndpoint=$IOT_ENDPOINT" \
    "agentcoreMemoryId=$MEMORY_ID" \
    "storeId=$STORE_ID"
# Saved (not passed with -c) so later deploys and `cdk destroy` keep the
# exclusions; dropping them on a redeploy would make the VPC origin fail.
if [ "$USE_BPA_EXCLUSION" = "true" ]; then
    info "Adding VPC Block Public Access exclusions for the ALB's private subnets"
    save_context "$SIM_DIR" "vpcBpaExclusion=true"
fi

USER_POOL_ID="$(stack_output "$SIM_STACK" CognitoUserPoolId)"
APP_CLIENT_ID="$(stack_output "$SIM_STACK" CognitoAppClientId)"
if [ -z "$USER_POOL_ID" ] || [ -z "$APP_CLIENT_ID" ]; then
    # Phase 1: Lambda@Edge cannot read environment variables, so the Cognito
    # IDs are baked into the edge function at synth time. They don't exist
    # until Cognito is created, so the first deploy creates everything except
    # the edge authenticator. The distribution has no auth until phase 2,
    # which runs immediately below.
    info "Phase 1 of 2: creating Cognito, the instance, and CloudFront (edge auth is attached in phase 2)"
    cdk_deploy "$SIM_DIR" "$SIM_STACK" \
        -c edgeAuthRequired=false \
        -c edgeCognitoUserPoolId= \
        -c edgeCognitoAppClientId= \
        -c edgeCognitoUserPoolDomain=
    USER_POOL_ID="$(stack_output "$SIM_STACK" CognitoUserPoolId)"
    APP_CLIENT_ID="$(stack_output "$SIM_STACK" CognitoAppClientId)"
    [ -n "$USER_POOL_ID" ] && [ -n "$APP_CLIENT_ID" ] || die "Phase 1 finished without Cognito outputs."
else
    info "Cognito already exists; going straight to phase 2"
fi

# The CSRF nonce secret signs the edge authenticator's login state. The stack
# reuses it from the gitignored config.local.json sidecar, so seed the sidecar
# here rather than passing the secret on the command line.
SIDECAR="$SIM_DIR/lambdas/edge-auth/config.local.json"
if [ ! -s "$SIDECAR" ] || ! grep -q '"csrfNonceSecret": *"[^"]' "$SIDECAR"; then
    info "Generating a new CSRF nonce secret in ${SIDECAR#"$REPO_ROOT"/} (gitignored)"
    (umask 077 && printf '{"csrfNonceSecret": "%s"}\n' "$(openssl rand -hex 32)" > "$SIDECAR")
fi

info "Phase 2 of 2: attaching the Cognito authenticator at the edge"
save_context "$SIM_DIR" \
    "edgeCognitoUserPoolId=$USER_POOL_ID" \
    "edgeCognitoAppClientId=$APP_CLIENT_ID" \
    "edgeCognitoUserPoolDomain=restaurant-sim-$ACCOUNT_ID.auth.$AWS_REGION.amazoncognito.com"
cdk_deploy "$SIM_DIR" "$SIM_STACK"

# ============================================
# VALIDATION
# ============================================
step "Validating the deployment"
FAILED=0
STACKS=(IotCoreBrokerStack CloudMcpGatewayStack HarnessAgentStack ProactiveAlertingStack "$SIM_STACK")
[ "$SKIP_WEB_PUSH" = "true" ] || STACKS+=(WebPushStack)
for stack in "${STACKS[@]}"; do
    status="$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$stack" \
        --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo MISSING)"
    case "$status" in
        CREATE_COMPLETE|UPDATE_COMPLETE) info "OK    $stack ($status)" ;;
        *) info "FAIL  $stack ($status)"; FAILED=1 ;;
    esac
done

CF_DOMAIN="$(stack_output "$SIM_STACK" CloudFrontDomainName)"
if [ -n "$CF_DOMAIN" ]; then
    # Give CloudFront a moment; an unauthenticated request should be
    # redirected (302) to the Cognito Hosted UI.
    code=""
    for _ in $(seq 1 12); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "https://$CF_DOMAIN/" || true)"
        [ "$code" = "302" ] && break
        sleep 10
    done
    if [ "$code" = "302" ]; then
        info "OK    CloudFront redirects unauthenticated visitors to Cognito (HTTP 302)"
    else
        info "WARN  CloudFront returned HTTP ${code:-none}, expected 302. The edge function can take a few more minutes to propagate."
    fi
else
    info "FAIL  $SIM_STACK has no CloudFrontDomainName output"; FAILED=1
fi

[ "$FAILED" -eq 0 ] || die "Validation failed. Check the stacks above in the CloudFormation console ($AWS_REGION)."

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
cat <<EOF

==> Deployment complete (${ELAPSED} min)

    Dashboard: https://$CF_DOMAIN/
      Sign up for an account on the Cognito login page, then sign in.
      The simulator can take about 5 more minutes to finish booting.

    Instance:  $(stack_output "$SIM_STACK" InstanceId)
    Harness:   $HARNESS_ARN
    Memory:    $MEMORY_ARN

    Next: follow "Deployment Validation" in README.md for the end-to-end
    tool-call check.

    Account-specific values were saved to the gitignored cdk.context.json in
    infra/harness-agent and infra/simulator-ec2-alb, so later deploys and
    'cdk destroy' need no -c flags.

    To remove everything, follow "Cleanup" in README.md (memory id: $MEMORY_ID).

EOF
