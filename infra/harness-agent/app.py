#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app for the AgentCore Managed Harness agent layer (spec 022 Task 1).

This stack provisions ONLY the least-privilege harness *execution role*. The
harness resource itself has no stable CloudFormation construct at GA, so it is
created out-of-band by ``provision/provision_harness.py`` against the
``bedrock-agentcore-control`` data plane, referencing the role ARN this stack
exports.

Additive and reversible: deploying this stack creates an unused IAM role and
changes no production traffic.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infrastructure.harness_agent_stack import HarnessAgentStack

app = cdk.App()
HarnessAgentStack(
    app,
    "HarnessAgentStack",
    stack_name="HarnessAgentStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-west-2"
    ),
    description=(
        "Spec 022 Task 1: least-privilege execution role for the AgentCore "
        "Managed Harness agent layer. Additive; no traffic, no production "
        "change. Harness resource created via provision/provision_harness.py."
    ),
)

# IaC security checks (P1). Error-level findings fail `cdk synth`, so an
# unreviewed violation cannot be deployed; `reports=True` writes the
# AwsSolutions-*-NagReport.csv that the ProtoShield cdknag plugin reads.
# Accepted risks are NagSuppressions carrying written evidence, in the stack.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

app.synth()
