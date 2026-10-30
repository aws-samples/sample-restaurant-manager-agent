#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app for the proactive alerting foundation (spec 023, Phase 0)."""
from __future__ import annotations

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infrastructure.proactive_alerting_stack import ProactiveAlertingStack

app = cdk.App()
ProactiveAlertingStack(
    app,
    "ProactiveAlertingStack",
    stack_name="ProactiveAlertingStack",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-west-2"),
    description="Proactive alerting foundation (spec 023 Phase 0): Alerts table, EventBridge bus, "
    "Step Functions policy skeleton, ingest + ack Lambdas, events DLQ. Additive; engine-independent.",
)
# IaC security checks (P1): error-level findings fail synth; reports=True writes
# the NagReport.csv the ProtoShield cdknag plugin reads.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

app.synth()
