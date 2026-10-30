# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guard: the gateway stack must NOT import Amazon MQ broker
exports (spec 020 Task 8 decommissioned Amazon MQ).

Originally (spec 006 R2.1 / R2.2) this stack imported four exports from
spec 005's ``MqttBrokerStack`` via ``Fn::ImportValue``
(``BrokerEndpoint``, ``BrokerPort``, ``MasterCredentialsSecretArn``,
``Region``). Spec 020 migrated the transport to AWS IoT Core
(connection-less ``iot-data:Publish``) and Task 8 removed those imports —
both because the values are unused and because a live ``Fn::ImportValue``
would block deletion of ``MqttBrokerStack`` (CloudFormation refuses to
delete a stack whose exported value is still imported elsewhere).

These tests ensure the coupling is not reintroduced.
"""
from __future__ import annotations

import json

import aws_cdk as cdk
import pytest

from infrastructure.cloud_mcp_gateway_stack import CloudMcpGatewayStack


@pytest.fixture(scope="module")
def raw_template() -> dict:
    """Synthesize the stack once and return the CloudFormation JSON."""
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack", env=cdk.Environment(region="us-west-2")
    )
    return app.synth().get_stack_by_name("TestStack").template


def _find_import_values(obj: object) -> list[object]:
    """Recursively collect every ``Fn::ImportValue`` payload in the JSON."""
    found: list[object] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "Fn::ImportValue":
                found.append(value)
            else:
                found.extend(_find_import_values(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_import_values(item))
    return found


def test_stack_does_not_import_any_value(raw_template: dict) -> None:
    """No ``Fn::ImportValue`` anywhere — the gateway is self-contained on
    the IoT Core transport and must not couple to another stack's exports
    (which would block ``MqttBrokerStack`` deletion)."""
    imports = _find_import_values(raw_template)
    assert imports == [], (
        f"gateway stack still uses Fn::ImportValue {imports}; broker "
        "imports must stay removed so MqttBrokerStack can be deleted"
    )


def test_stack_has_no_broker_attributes() -> None:
    """The old ``broker_*`` stack attributes must be gone."""
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack2", env=cdk.Environment(region="us-west-2")
    )
    for attr in (
        "broker_endpoint",
        "broker_port",
        "master_credentials_secret_arn",
        "broker_region",
    ):
        assert not hasattr(stack, attr), (
            f"stack still exposes removed broker attribute {attr!r}"
        )


def test_template_has_no_hardcoded_broker_arn_or_hostname(
    raw_template: dict,
) -> None:
    """The template must not embed an Amazon MQ hostname, broker ID, or
    Secrets Manager ARN — all broker coupling is gone."""
    blob = json.dumps(raw_template)
    assert ".mq.us-west-2.amazonaws.com" not in blob, (
        "template contains a hard-coded Amazon MQ hostname"
    )
    assert '"b-' not in blob, (
        "template contains what looks like a hard-coded Amazon MQ broker ID"
    )
    assert "arn:aws:secretsmanager:us-west-2:" not in blob, (
        "template contains a hard-coded Secrets Manager ARN"
    )
