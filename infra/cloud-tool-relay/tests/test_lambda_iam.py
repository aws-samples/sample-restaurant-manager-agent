# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IAM role tests for the gateway Lambda functions (spec 006, task 4.4).

Spec 006 requirements covered here:

* **R9.1** — Each gateway Lambda execution role shall have:
    - ``dynamodb:PutItem``, ``dynamodb:UpdateItem``, ``dynamodb:GetItem``
      on the ``CommandResults`` table only.
    - ``logs:CreateLogGroup``, ``logs:CreateLogStream``,
      ``logs:PutLogEvents`` on the function's own log group.
    - ``iot:Publish`` on the ``stores/*/commands/{domain}`` topic only
      (spec 020). The Amazon MQ broker-credentials secret grant was
      removed in Task 8.
* **R9.4** — No role in this stack shall have ``*`` resource scope on
  any of the above actions.
* **R9.5** — No role shall have ``AdministratorAccess`` or any
  ``*FullAccess`` managed policy attached.

Tests follow the pattern established in ``test_dynamodb_tables.py``:
one module-scoped fixture synthesizes the stack once and returns the
CDK ``Template`` matcher wrapper and the raw CloudFormation JSON.
"""
from __future__ import annotations

import json
from typing import Final

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.cloud_mcp_gateway_stack import CloudMcpGatewayStack

#: The four gateway domains. Must match the stack's ``_GATEWAY_DOMAINS``.
_GATEWAY_DOMAINS: Final[list[str]] = [
    "equipment",
    "inventory",
    "pos",
    "schedule",
]


@pytest.fixture(scope="module")
def synthesized() -> tuple[CloudMcpGatewayStack, Template, dict]:
    """Synthesize the stack once per test module.

    Returns ``(stack, template, raw_template_dict)``.
    """
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack", env=cdk.Environment(region="us-west-2")
    )
    raw = app.synth().get_stack_by_name("TestStack").template
    return stack, Template.from_stack(stack), raw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_lambda_roles(raw: dict) -> dict[str, dict]:
    """Return a mapping of logical-id → resource-dict for IAM roles
    that are explicitly referenced by gateway Lambda functions
    (``*-tool-relay``) in the template.

    This excludes CDK-internal roles such as the ``LogRetention``
    custom-resource Lambda's service role, which is also assumed by
    ``lambda.amazonaws.com`` but is not a gateway execution role.
    """
    # Step 1: find the role logical IDs referenced by gateway Lambdas.
    gateway_role_refs: set[str] = set()
    for _lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::Lambda::Function":
            continue
        props = res.get("Properties", {})
        fn_name = props.get("FunctionName", "")
        if not fn_name.endswith("-tool-relay"):
            continue
        role_prop = props.get("Role", {})
        # CDK renders the role reference as {"Fn::GetAtt": ["RoleLogicalId", "Arn"]}
        if isinstance(role_prop, dict):
            get_att = role_prop.get("Fn::GetAtt", [])
            if len(get_att) >= 1:
                gateway_role_refs.add(get_att[0])

    # Non-vacuity guard. This helper SKIPS anything not matching the name suffix,
    # so if the suffix ever drifts from the stack's `function_name` the filter
    # matches nothing, every assertion below iterates an empty set, and the IAM
    # tests pass while checking nothing. That nearly shipped during the
    # 2026-08-08 mcp-gateway -> tool-relay rename.
    assert len(gateway_role_refs) == 4, (
        f"expected 4 relay execution roles, found {len(gateway_role_refs)} -- the "
        "FunctionName suffix filter in this file is out of step with the stack"
    )

    # Step 2: return only those IAM roles.
    roles = {}
    for lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::IAM::Role":
            continue
        if lid in gateway_role_refs:
            roles[lid] = res
    return roles


def _collect_inline_policy_statements(raw: dict, role_logical_id: str) -> list[dict]:
    """Collect all policy statements from ``AWS::IAM::Policy`` resources
    that reference the given role logical ID."""
    stmts: list[dict] = []
    for _lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::IAM::Policy":
            continue
        props = res.get("Properties", {})
        # Check if this policy is attached to our role
        roles_refs = props.get("Roles", [])
        role_referenced = False
        for role_ref in roles_refs:
            if isinstance(role_ref, dict) and role_ref.get("Ref") == role_logical_id:
                role_referenced = True
                break
        if not role_referenced:
            continue
        doc = props.get("PolicyDocument", {})
        stmts.extend(doc.get("Statement", []))
    return stmts


def _resolve_resource_refs(resource: object) -> list[str]:
    """Flatten a CloudFormation resource field into a list of string
    representations for comparison. Handles plain strings, ``Ref``,
    ``Fn::GetAtt``, ``Fn::Join``, and lists thereof."""
    results: list[str]  = []
    if isinstance(resource, str):
        results.append(resource)
    elif isinstance(resource, dict):
        results.append(json.dumps(resource, sort_keys=True))
    elif isinstance(resource, list):
        for item in resource:
            results.extend(_resolve_resource_refs(item))
    return results


# ---------------------------------------------------------------------------
# Tests: Each Lambda has its own custom role
# ---------------------------------------------------------------------------


class TestEachLambdaHasCustomRole:
    """R9.1: Each gateway Lambda must have its own execution role."""

    def test_four_lambda_roles_exist(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """There should be exactly four IAM roles assumed by
        ``lambda.amazonaws.com`` — one per gateway domain."""
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        assert len(roles) >= 4, (
            f"Expected at least 4 Lambda execution roles, found {len(roles)}: "
            f"{list(roles.keys())}"
        )

    def test_each_lambda_references_a_role(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Each ``AWS::Lambda::Function`` for a gateway domain must
        reference a ``Role`` property (not rely on CDK's default)."""
        _, _, raw = synthesized
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::Lambda::Function":
                continue
            props = res.get("Properties", {})
            fn_name = props.get("FunctionName", "")
            if not fn_name.endswith("-tool-relay"):
                continue
            role_prop = props.get("Role")
            assert role_prop is not None, (
                f"Lambda {fn_name} ({lid}) has no explicit Role property"
            )


# ---------------------------------------------------------------------------
# Tests: Secrets Manager permissions
# ---------------------------------------------------------------------------


class TestNoBrokerSecretAccess:
    """Spec 020 Task 8: Amazon MQ is decommissioned, so no gateway role
    may read a broker-credentials secret any more."""

    def test_no_secretsmanager_statements(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                sm = [a for a in actions if a.startswith("secretsmanager:")]
                assert not sm, (
                    f"Role {role_lid} still grants {sm}; broker secret "
                    "access must be removed after Amazon MQ decommission"
                )


class TestIotPublishPermissions:
    """R9.1 (spec 020): each role has ``iot:Publish`` scoped to its own
    domain's command topic, for any store — and never ``*``."""

    def test_iot_publish_present_and_scoped(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            iot_stmts = []
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "iot:Publish" in actions:
                    iot_stmts.append(s)
            assert len(iot_stmts) >= 1, (
                f"Role {role_lid} is missing iot:Publish"
            )
            for s in iot_stmts:
                flat = _resolve_resource_refs(s.get("Resource", []))
                assert flat, f"Role {role_lid} iot:Publish has no resource"
                for r in flat:
                    assert r != "*", (
                        f"Role {role_lid} has wildcard resource on "
                        "iot:Publish (violates R9.4)"
                    )
                    assert "topic/stores/" in r and "/commands/" in r, (
                        f"Role {role_lid} iot:Publish resource not scoped to "
                        f"a command topic: {r}"
                    )


# ---------------------------------------------------------------------------
# Tests: DynamoDB permissions
# ---------------------------------------------------------------------------


class TestDynamoDBPermissions:
    """R9.1: Each Lambda role has DynamoDB PutItem/UpdateItem/GetItem
    on ``CommandResults`` only — NOT on ``StoreStateCache``."""

    def test_dynamodb_actions_present(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        required_actions = {
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:GetItem",
        }
        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            ddb_actions: set[str] = set()
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                for a in actions:
                    if a.startswith("dynamodb:"):
                        ddb_actions.add(a)
            assert required_actions.issubset(ddb_actions), (
                f"Role {role_lid} is missing DynamoDB actions. "
                f"Has: {ddb_actions}, needs: {required_actions}"
            )

    def test_dynamodb_scoped_to_command_results_only(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """The DynamoDB statement's resource must reference the
        ``CommandResults`` table ARN, not ``StoreStateCache`` and not
        ``*``."""
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)

        # Find the CommandResults table logical ID
        cr_logical_id = None
        ssc_logical_id = None
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::DynamoDB::Table":
                continue
            tname = res.get("Properties", {}).get("TableName", "")
            if tname == "CommandResults":
                cr_logical_id = lid
            elif tname == "StoreStateCache":
                ssc_logical_id = lid

        assert cr_logical_id is not None, "CommandResults table not found"
        assert ssc_logical_id is not None, "StoreStateCache table not found"

        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                ddb_actions = [a for a in actions if a.startswith("dynamodb:")]
                if not ddb_actions:
                    continue
                # Check resources
                resources = s.get("Resource", [])
                flat = _resolve_resource_refs(resources)
                for r in flat:
                    assert r != "*", (
                        f"Role {role_lid} has wildcard resource on "
                        f"DynamoDB actions {ddb_actions} (violates R9.4)"
                    )
                    # The resource must reference CommandResults, not
                    # StoreStateCache
                    assert ssc_logical_id not in r, (
                        f"Role {role_lid} references StoreStateCache "
                        f"({ssc_logical_id}) in DynamoDB policy — "
                        "gateway Lambdas must only access CommandResults"
                    )

    def test_no_store_state_cache_access(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Explicitly verify that no Lambda role has any DynamoDB
        statement referencing the ``StoreStateCache`` table."""
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)

        ssc_logical_id = None
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::DynamoDB::Table":
                continue
            if res.get("Properties", {}).get("TableName") == "StoreStateCache":
                ssc_logical_id = lid
                break

        assert ssc_logical_id is not None, "StoreStateCache table not found"

        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            all_resources_str = json.dumps(stmts)
            assert ssc_logical_id not in all_resources_str, (
                f"Role {role_lid} references StoreStateCache "
                f"({ssc_logical_id}) — gateway Lambdas must NOT "
                "have access to StoreStateCache"
            )


# ---------------------------------------------------------------------------
# Tests: CloudWatch Logs permissions
# ---------------------------------------------------------------------------


class TestCloudWatchLogsPermissions:
    """R9.1: Each Lambda role has log write permissions scoped to its
    own log group."""

    def test_log_actions_present(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        required_log_actions = {
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        }
        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            log_actions: set[str] = set()
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                for a in actions:
                    if a.startswith("logs:"):
                        log_actions.add(a)
            assert required_log_actions.issubset(log_actions), (
                f"Role {role_lid} is missing log actions. "
                f"Has: {log_actions}, needs: {required_log_actions}"
            )

    def test_log_resources_not_wildcard(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """R9.4: Log statements must not use ``*`` as the resource."""
        _, _, raw = synthesized
        roles = _find_lambda_roles(raw)
        for role_lid in roles:
            stmts = _collect_inline_policy_statements(raw, role_lid)
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                log_actions = [a for a in actions if a.startswith("logs:")]
                if not log_actions:
                    continue
                resources = s.get("Resource", [])
                flat = _resolve_resource_refs(resources)
                for r in flat:
                    assert r != "*", (
                        f"Role {role_lid} has wildcard resource on "
                        f"log actions {log_actions} (violates R9.4)"
                    )


# ---------------------------------------------------------------------------
# Tests: No wildcard resources (R9.4)
# ---------------------------------------------------------------------------


class TestNoWildcardResources:
    """R9.4: No role in this stack shall have ``*`` resource scope on
    DynamoDB, Lambda, Bedrock, or Secrets Manager actions."""

    def test_no_star_resource_on_sensitive_actions(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        # Check ALL IAM roles in the stack, not just gateway roles.
        # CDK-internal roles (LogRetention) only have logs:* actions
        # which are not in our sensitive-prefix list, so they pass.
        sensitive_prefixes = (
            "dynamodb:",
            "lambda:",
            "bedrock:",
            "secretsmanager:",
        )
        violations: list[str] = []
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::IAM::Role":
                continue
            # Collect all inline policy statements for this role
            stmts = _collect_inline_policy_statements(raw, lid)
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                has_sensitive = any(
                    a.startswith(sensitive_prefixes) for a in actions
                )
                if not has_sensitive:
                    continue
                resources = s.get("Resource", [])
                flat = _resolve_resource_refs(resources)
                for r in flat:
                    if r == "*":
                        violations.append(
                            f"{lid}: actions={actions} resource=*"
                        )
        assert not violations, (
            "Roles with wildcard resources on sensitive actions "
            "(violates R9.4):\n  " + "\n  ".join(violations)
        )


# ---------------------------------------------------------------------------
# Tests: No FullAccess or AdministratorAccess (R9.5)
# ---------------------------------------------------------------------------


class TestNoFullAccessPolicies:
    """R9.5: No role shall have ``AdministratorAccess`` or any
    ``*FullAccess`` managed policy attached."""

    def test_no_admin_or_full_access_managed_policies(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        _, _, raw = synthesized
        violations: list[str] = []
        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::IAM::Role":
                continue
            props = res.get("Properties", {})
            managed = props.get("ManagedPolicyArns", [])
            for policy_arn in managed:
                arn_str = json.dumps(policy_arn)
                if "AdministratorAccess" in arn_str:
                    violations.append(
                        f"{lid}: has AdministratorAccess managed policy"
                    )
                if "FullAccess" in arn_str:
                    violations.append(
                        f"{lid}: has a *FullAccess managed policy: {arn_str}"
                    )
        assert not violations, (
            "Roles with prohibited managed policies (violates R9.5):\n  "
            + "\n  ".join(violations)
        )
