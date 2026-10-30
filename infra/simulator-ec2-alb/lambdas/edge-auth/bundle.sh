#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Vendor the edge-auth Lambda@Edge production dependencies (cognito-at-edge)
# into node_modules/ so the CDK `Code.from_asset(...)` bundle carries them.
#
# cognito-at-edge is pure JavaScript (no native addons), so no Docker/QEMU
# cross-compile is required - Lambda@Edge runs on x86_64. Run this before
# `cdk synth` / `cdk deploy` from anywhere:
#
#     infra/simulator-ec2-alb/lambdas/edge-auth/bundle.sh
#
#   node_modules/       gitignored, rebuilt here on every deploy
#   package-lock.json   committed, so this install is reproducible (npm ci)
#
set -euo pipefail
cd "$(dirname "$0")"

# npm ci is reproducible and requires the committed package-lock.json. If the
# lock is somehow missing, fall back to `npm install` to regenerate it.
if [ -f package-lock.json ]; then
  npm ci --omit=dev
else
  echo "package-lock.json missing - regenerating with npm install" >&2
  npm install --omit=dev
fi

echo "edge-auth deps vendored: cognito-at-edge@$(node -e "console.log(require('cognito-at-edge/package.json').version)")"
