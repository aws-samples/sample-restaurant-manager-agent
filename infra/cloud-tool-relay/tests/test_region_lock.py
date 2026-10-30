# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Region-lock tests for the cloud-tool-relay stack (spec 006, task 1.3).

Spec 006 requirements covered here:

* **R1.1** — the stack shall deploy to AWS region ``us-west-2``.
* **R1.3** — the stack shall refuse to deploy in any region other than
  ``us-west-2``.

The stack enforces this at synth time by checking ``self.region`` in its
constructor and raising :class:`ValueError` if the region is anything
other than ``us-west-2``. These tests exercise that invariant directly
without synthesizing a full CloudFormation template, so they run in a
fraction of a second and give a clear failure mode if someone ever
loosens the guard.
"""
from __future__ import annotations

import aws_cdk as cdk
import pytest

from infrastructure.cloud_mcp_gateway_stack import (
    REQUIRED_REGION,
    CloudMcpGatewayStack,
)


def test_required_region_constant_is_us_west_2() -> None:
    """R1.1: the stack module's ``REQUIRED_REGION`` constant must stay
    pinned to ``us-west-2``.

    Guarding the constant value independently of the constructor check
    means an accidental edit to the string (typo, copy-paste from a
    different spec, find-and-replace gone wrong) gets caught here rather
    than surfacing as a confusing stack-synth failure downstream.
    """
    assert REQUIRED_REGION == "us-west-2"


def test_stack_synthesizes_in_us_west_2() -> None:
    """R1.1: instantiating the stack with ``env=Environment(region="us-west-2")``
    must succeed. This is the positive path — without it the negative
    tests below would be vacuous."""
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack", env=cdk.Environment(region="us-west-2")
    )
    assert stack.region == "us-west-2"


@pytest.mark.parametrize(
    "bad_region",
    [
        "us-east-1",
        "eu-west-1",
        "ap-southeast-2",
    ],
)
def test_stack_refuses_non_us_west_2_region(bad_region: str) -> None:
    """R1.3: instantiating the stack with any region other than
    ``us-west-2`` must raise :class:`ValueError`, and the message must
    mention both the required region and the offending region so the
    failure is self-explanatory in CI logs."""
    app = cdk.App()
    with pytest.raises(ValueError) as excinfo:
        CloudMcpGatewayStack(
            app, "TestStack", env=cdk.Environment(region=bad_region)
        )

    message = str(excinfo.value)
    assert REQUIRED_REGION in message
    assert bad_region in message
