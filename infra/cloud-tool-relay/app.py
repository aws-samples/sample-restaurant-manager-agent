#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app entrypoint for the cloud-side MCP gateway + AgentCore stack.

Per spec 006 this stack is pinned to ``us-west-2`` (requirements R1.1, R1.3)
and consumes four stack exports from spec 005 (the MQTT broker): see
``infrastructure/cloud_mcp_gateway_stack.py``. The account is resolved from
the caller's environment at deploy time so any developer with dev-account
credentials can deploy it without editing this file.
"""
from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infrastructure.cloud_mcp_gateway_stack import CloudMcpGatewayStack

app = cdk.App()

CloudMcpGatewayStack(
    app,
    "CloudMcpGatewayStack",
    env=cdk.Environment(region="us-west-2"),
)

# IaC security checks (P1): error-level findings fail synth; reports=True writes
# the NagReport.csv the ProtoShield cdknag plugin reads.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))

app.synth()
