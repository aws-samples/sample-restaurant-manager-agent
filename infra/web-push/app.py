#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app for Web Push re-engagement (spec 025 Task 1d)."""
from __future__ import annotations

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infrastructure.web_push_stack import WebPushStack

app = cdk.App()
WebPushStack(
    app,
    "WebPushStack",
    stack_name="WebPushStack",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-west-2"),
    description="Web Push re-engagement (spec 025 Task 1d): PushSubscriptions table, VAPID secret, "
    "registrar + push-sender Lambdas, IoT rules. Additive to spec-023; in-app inbox remains the floor.",
)
# IaC security checks (P1): error-level findings fail synth; reports=True writes
# the NagReport.csv the ProtoShield cdknag plugin reads.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

app.synth()
