#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app entrypoint for the simulator EC2 + ALB stack.

This stack is pinned to ``us-west-2`` per spec 004 (requirements R1.1, R1.3).
The account is intentionally resolved from the caller's environment at deploy
time so the same CDK app can be deployed by any developer with credentials for
the dev account without editing this file.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions

from infrastructure.simulator_ec2_alb_stack import SimulatorEc2AlbStack

app = cdk.App()

SimulatorEc2AlbStack(
    app,
    "SimulatorEc2AlbStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="us-west-2",
    ),
)

# IaC security checks (P1): error-level findings fail synth; reports=True writes
# the NagReport.csv the ProtoShield cdknag plugin reads.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

# The Lambda@Edge authenticator lives in a SEPARATE, CDK-generated us-east-1
# support stack (experimental.EdgeFunction creates it), so the main stack's
# add_stack_suppressions cannot reach it -- its two findings are raised on a
# different Stack instance. Suppress there, from the app where both exist.
for _child in app.node.children:
    if isinstance(_child, cdk.Stack) and _child.node.id.startswith("edge-lambda-stack"):
        NagSuppressions.add_stack_suppressions(
            _child,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "CDK-generated Lambda@Edge execution role in the support stack "
                        "that experimental.EdgeFunction synthesizes. Not authored here; "
                        "it carries AWSLambdaBasicExecutionRole for its own logging."
                    ),
                },
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "Runtime of the CDK-generated Lambda@Edge function. Lambda@Edge "
                        "supports a narrower runtime set than regional Lambda, and the "
                        "version is chosen by the construct plus the cognito-at-edge "
                        "bundle, not by this app."
                    ),
                },
            ],
        )

app.synth()
