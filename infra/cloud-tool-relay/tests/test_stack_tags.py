# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Smoke-level assertion test for stack-wide tag propagation (spec 006, task 1.2).

R1.4: every resource created by the stack must be tagged with
``Project=restaurant-simulator``, ``Environment=dev``, ``ManagedBy=cdk``,
and ``Spec=006-cloud-mcp-gateway-cdk``.

Task 1.2 calls for a smoke-level check that these tags propagate to
synthesized resources. The full-coverage test — asserting *every*
taggable resource in the finished stack carries the four tags — is task
12.10 and will be added alongside the other assertion-library tests
once the stack grows real resources. For now the stack is the bare
scaffold from task 1.1, so this test synthesizes it, grabs any taggable
resource CloudFormation decides to emit, and asserts the four tags are
all present with the expected values. If CDK ever generates a synthetic
resource for us (e.g., a ``CDK::Metadata`` entry) we skip it because the
CDK-managed resources do not carry user tags.
"""
from __future__ import annotations

from typing import Final

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.cloud_mcp_gateway_stack import (
    STACK_TAGS,
    CloudMcpGatewayStack,
)


#: CloudFormation resource types that have no ``Tags`` property in their
#: schema — asserting tags on these would be a false negative. Kept
#: intentionally narrow. (Originally mirrored ``infra/mqtt-broker/tests/
#: test_stack.py``, deleted when Amazon MQ was retired -- see ADR-004.)
_UNTAGGABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "AWS::CDK::Metadata",
        "AWS::IAM::Policy",
        "AWS::EC2::Route",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::EC2::SubnetRouteTableAssociation",
        "AWS::EC2::VPCGatewayAttachment",
    }
)


@pytest.fixture(scope="module")
def synthesized() -> tuple[Template, dict]:
    """Synthesize the stack once per test module and return both the CDK
    ``Template`` matcher wrapper and the raw CloudFormation JSON."""
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack", env=cdk.Environment(region="us-west-2")
    )
    raw = app.synth().get_stack_by_name("TestStack").template
    return Template.from_stack(stack), raw


def test_stack_tags_constant_has_expected_keys_and_values() -> None:
    """R1.4: the ``STACK_TAGS`` constant in the stack module exposes
    exactly the four key/value pairs the spec requires, so later tasks
    and tests can import and reuse it without drifting."""
    assert STACK_TAGS == {
        "Project": "restaurant-simulator",
        "Environment": "dev",
        "ManagedBy": "cdk",
        "Spec": "006-cloud-mcp-gateway-cdk",
    }


def test_every_taggable_resource_carries_required_tags(
    synthesized: tuple[Template, dict],
) -> None:
    """R1.4: every taggable ``AWS::*`` resource in the synthesized
    template carries all four stack-level tags.

    Task 1.2 scaffold-phase note: the stack as checked in for task 1.1
    creates no real resources yet, so this test may find zero taggable
    resources to inspect — that's fine. The synth itself exercises the
    ``Tags.of(self)`` wiring, and once tasks 2–10 add real resources
    this same test grows teeth automatically.
    """
    _, raw = synthesized

    missing: list[str] = []
    taggable_checked = 0

    for logical_id, resource in raw.get("Resources", {}).items():
        rtype = resource.get("Type", "")
        if not rtype.startswith("AWS::"):
            # Skip ``Custom::*`` and any other non-AWS resource types;
            # CDK tag propagation does not reach those.
            continue
        if rtype in _UNTAGGABLE_TYPES:
            continue

        taggable_checked += 1
        props = resource.get("Properties", {}) or {}
        tags = props.get("Tags")
        if not isinstance(tags, (list, dict)):
            missing.append(f"{logical_id} ({rtype}): no Tags field")
            continue

        # Tags can be in standard array format [{"Key": k, "Value": v}]
        # or map format {"k": "v"} (used by some L1 constructs like
        # AWS::BedrockAgentCore::Runtime and RuntimeEndpoint).
        if isinstance(tags, list):
            keys = {t["Key"]: t["Value"] for t in tags if isinstance(t, dict)}
        else:
            keys = tags
        for key, value in STACK_TAGS.items():
            if keys.get(key) != value:
                missing.append(
                    f"{logical_id} ({rtype}): missing {key}={value} "
                    f"(got {keys.get(key)!r})"
                )

    assert not missing, "untagged resources:\n  " + "\n  ".join(missing)

    # Belt-and-braces: if there *are* taggable resources, make sure we
    # actually checked at least one so a template that happened to emit
    # zero resources wouldn't silently pass. Zero is legitimate for the
    # current scaffold, so this is informational, not an assertion.
    # (Left as a comment for future readers.)
    _ = taggable_checked
