#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app for the IoT Core broker (spec 020 Phase A)."""
from __future__ import annotations

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infrastructure.iot_core_broker_stack import IotCoreBrokerStack

app = cdk.App()
IotCoreBrokerStack(
    app,
    "IotCoreBrokerStack",
    stack_name="IotCoreBrokerStack",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-west-2"),
    description="IoT Core broker (spec 020 Phase A): response handler + IoT rule. Additive; no cutover.",
)

# IaC security checks (P1). AwsSolutionsChecks annotates the tree at synth time;
# error-level findings fail `cdk synth`, so an unreviewed violation cannot be
# deployed. `reports=True` writes AwsSolutions-*-NagReport.csv into cdk.out, which
# is also the artifact the ProtoShield cdknag plugin reads -- before this, that
# plugin reported "No NagReport.csv files found" and IaC coverage was effectively
# zero. Accepted risks are recorded as NagSuppressions WITH a reason in the stack.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

app.synth()
