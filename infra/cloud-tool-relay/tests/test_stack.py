# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK synth-time unit tests for the CloudMcpGatewayStack (spec 006, task 12).

Covers sub-tasks 12.1 through 12.10 — Lambda functions, DynamoDB tables,
ECR repositories, stack outputs, IAM role scoping, tag propagation.

All tests share a single module-scoped fixture that synthesizes the stack
once and returns the CDK ``Template`` wrapper plus the raw CloudFormation
JSON dict.
"""
from __future__ import annotations

import json
import re
from typing import Final

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infrastructure.cloud_mcp_gateway_stack import CloudMcpGatewayStack

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


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

#: The four gateway domain names (alphabetical).
_GATEWAY_DOMAINS: Final[list[str]] = [
    "equipment",
    "inventory",
    "pos",
    "schedule",
]

#: Expected gateway Lambda function names.
_GATEWAY_FUNCTION_NAMES: Final[list[str]] = [
    f"{d}-tool-relay" for d in _GATEWAY_DOMAINS
]

#: Python runtimes R4.2 will no longer accept, because AWS has retired them or
#: announced end of support. This is the deliberately-moving part of the runtime
#: assertion: R4.2 requires "a currently-supported runtime" rather than a literal
#: version, so the test cannot name the *right* answer -- it can only reject known
#: wrong ones. Add a version here when AWS announces its end of support, and
#: ``test_runtime_is_a_supported_python`` will start demanding a bump.
_RETIRED_PYTHON_RUNTIMES: Final[frozenset[str]] = frozenset(
    {
        "python3.6",
        "python3.7",
        "python3.8",
        "python3.9",
        "python3.10",
        # Retired here by the 2026-08-07 R4.2 amendment, which is what moved these
        # relays off it.
        "python3.11",
    }
)

#: Required stack output logical names (R13).
_REQUIRED_OUTPUTS: Final[list[str]] = [
    "PosGatewayLambdaArn",
    "EquipmentGatewayLambdaArn",
    "InventoryGatewayLambdaArn",
    "ScheduleGatewayLambdaArn",
    "CommandResultsTableName",
    "StoreStateCacheTableName",
    "Region",
]

#: Required tags (R1.4).
_REQUIRED_TAGS: Final[dict[str, str]] = {
    "Project": "restaurant-simulator",
    "Environment": "dev",
    "ManagedBy": "cdk",
    "Spec": "006-cloud-mcp-gateway-cdk",
}


def _collect_inline_policy_statements(raw: dict, role_logical_id: str) -> list[dict]:
    """Collect all policy statements from ``AWS::IAM::Policy`` resources
    that reference the given role logical ID."""
    stmts: list[dict] = []
    for _lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::IAM::Policy":
            continue
        props = res.get("Properties", {})
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


def _find_gateway_lambda_resources(raw: dict) -> dict[str, dict]:
    """Return ``{function_name: resource_dict}`` for the four gateway Lambdas."""
    result: dict[str, dict] = {}
    for _lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::Lambda::Function":
            continue
        fn_name = res.get("Properties", {}).get("FunctionName", "")
        if fn_name.endswith("-tool-relay"):
            result[fn_name] = res
    return result


def _find_table_logical_id(raw: dict, table_name: str) -> str:
    """Return the logical ID of the DynamoDB table with the given physical name."""
    for lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::DynamoDB::Table":
            continue
        if res.get("Properties", {}).get("TableName") == table_name:
            return lid
    raise AssertionError(f"DynamoDB table {table_name!r} not found in template")


def _find_role_logical_id_for_lambda(raw: dict, function_name: str) -> str | None:
    """Return the logical ID of the IAM role referenced by the named Lambda."""
    for _lid, res in raw.get("Resources", {}).items():
        if res.get("Type") != "AWS::Lambda::Function":
            continue
        if res.get("Properties", {}).get("FunctionName") != function_name:
            continue
        role_prop = res.get("Properties", {}).get("Role", {})
        if isinstance(role_prop, dict):
            get_att = role_prop.get("Fn::GetAtt", [])
            if len(get_att) >= 1:
                return get_att[0]
    return None


def _flatten_resource_refs(resource: object) -> list[str]:
    """Flatten a CloudFormation resource field into string representations."""
    results: list[str] = []
    if isinstance(resource, str):
        results.append(resource)
    elif isinstance(resource, dict):
        results.append(json.dumps(resource, sort_keys=True))
    elif isinstance(resource, list):
        for item in resource:
            results.extend(_flatten_resource_refs(item))
    return results


# ===================================================================
# 12.1 — Lambda functions: names, runtime, architecture, memory, timeout
#         (R4.1, R4.2, R4.7)
# ===================================================================


class TestLambdaFunctions:
    """12.1: Exactly four Lambda functions with expected names, a
    currently-supported python3.x runtime (R4.2, amended 2026-08-07 -- was a
    literal python3.11), architecture arm64, memory 512 MB, timeout 10 s."""

    def test_exactly_four_gateway_lambdas(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.1"""
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        assert len(gateways) == 4, (
            f"Expected exactly 4 gateway Lambdas, found {len(gateways)}: "
            f"{list(gateways.keys())}"
        )

    def test_expected_function_names(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.1"""
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        assert set(gateways.keys()) == set(_GATEWAY_FUNCTION_NAMES)

    def test_runtime_is_a_supported_python(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.2

        R4.2 was amended 2026-08-07 from "shall use the `python3.11` runtime" to
        "a Python runtime that AWS Lambda currently supports", with the version
        pinned in code rather than in prose. So this asserts the *property*, not a
        literal: re-pinning a number here would rebuild exactly the trap the
        amendment removed -- the old assertion is what made a routine runtime bump
        look like a requirements violation.

        ``_RETIRED_PYTHON_RUNTIMES`` is the moving part. Add a version to it as AWS
        announces end of support, and this test starts demanding a bump.
        """
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        runtimes = {fn: res["Properties"]["Runtime"] for fn, res in gateways.items()}

        for fn_name, runtime in runtimes.items():
            assert runtime.startswith("python3."), (
                f"{fn_name} runtime is {runtime!r}, expected a python3.x runtime"
            )
            assert runtime not in _RETIRED_PYTHON_RUNTIMES, (
                f"{fn_name} runs {runtime!r}, which is retired or nearing Lambda "
                f"end of support; R4.2 requires a currently-supported runtime"
            )

        # R4.2 says "all four functions", so they must not drift apart.
        assert len(set(runtimes.values())) == 1, (
            f"the four relays must share one runtime, found {runtimes}"
        )

    def test_architecture_is_arm64(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.2"""
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        for fn_name, res in gateways.items():
            archs = res["Properties"].get("Architectures", [])
            assert "arm64" in archs, (
                f"{fn_name} architectures={archs!r}, expected ['arm64']"
            )

    def test_memory_is_512mb(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.7"""
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        for fn_name, res in gateways.items():
            mem = res["Properties"]["MemorySize"]
            assert mem == 512, (
                f"{fn_name} memory is {mem}, expected 512"
            )

    def test_timeout_is_10s(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R4.7"""
        _, _, raw = synthesized
        gateways = _find_gateway_lambda_resources(raw)
        for fn_name, res in gateways.items():
            timeout = res["Properties"]["Timeout"]
            assert timeout == 10, (
                f"{fn_name} timeout is {timeout}, expected 10"
            )


# ===================================================================
# 12.2 — DynamoDB tables: partition keys, TTL, on-demand billing
#         (R3.1, R3.2)
# ===================================================================


class TestDynamoDBTables:
    """12.2: Two DynamoDB tables with correct partition keys, TTL on
    CommandResults, on-demand billing on both."""

    def test_exactly_two_dynamodb_tables(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.1, R3.2"""
        _, template, _ = synthesized
        template.resource_count_is("AWS::DynamoDB::Table", 2)

    def test_command_results_partition_key(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.1"""
        _, template, _ = synthesized
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "CommandResults",
                "KeySchema": [
                    {"AttributeName": "correlation_id", "KeyType": "HASH"},
                ],
            },
        )

    def test_store_state_cache_partition_key(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.2"""
        _, template, _ = synthesized
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "StoreStateCache",
                "KeySchema": [
                    {"AttributeName": "store_domain_key", "KeyType": "HASH"},
                ],
            },
        )

    def test_command_results_has_ttl(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.1"""
        _, template, _ = synthesized
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "CommandResults",
                "TimeToLiveSpecification": {
                    "AttributeName": "expires_at",
                    "Enabled": True,
                },
            },
        )

    def test_command_results_on_demand_billing(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.1"""
        _, template, _ = synthesized
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "CommandResults",
                "BillingMode": "PAY_PER_REQUEST",
            },
        )

    def test_store_state_cache_on_demand_billing(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R3.2"""
        _, template, _ = synthesized
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "StoreStateCache",
                "BillingMode": "PAY_PER_REQUEST",
            },
        )


# ===================================================================
# 12.4 — Stack outputs: all R13 outputs present with exact logical names
#         (R13.*)
# ===================================================================


class TestStackOutputs:
    """12.4: Every stack output from R13 present with the exact logical name."""

    def test_all_required_outputs_present(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R13.*"""
        _, _, raw = synthesized
        outputs = raw.get("Outputs", {})
        missing = [name for name in _REQUIRED_OUTPUTS if name not in outputs]
        assert not missing, (
            f"Missing stack outputs: {missing}. "
            f"Present outputs: {sorted(outputs.keys())}"
        )


# ===================================================================
# 12.5 — Gateway Lambda roles: dynamodb:UpdateItem on CommandResults
#         and NOT on StoreStateCache (R9.1)
# ===================================================================


class TestGatewayLambdaRoleDynamoDB:
    """12.5: Gateway Lambda roles have dynamodb:UpdateItem on
    CommandResults and NOT on StoreStateCache."""

    def test_gateway_roles_have_update_item_on_command_results(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R9.1"""
        _, _, raw = synthesized
        cr_lid = _find_table_logical_id(raw, "CommandResults")

        for fn_name in _GATEWAY_FUNCTION_NAMES:
            role_lid = _find_role_logical_id_for_lambda(raw, fn_name)
            assert role_lid is not None, f"No role found for {fn_name}"

            stmts = _collect_inline_policy_statements(raw, role_lid)
            # Find statements with dynamodb:UpdateItem
            has_update_on_cr = False
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "dynamodb:UpdateItem" not in actions:
                    continue
                # Check if resource references CommandResults
                resources_str = json.dumps(s.get("Resource", []))
                if cr_lid in resources_str:
                    has_update_on_cr = True
                    break

            assert has_update_on_cr, (
                f"Role for {fn_name} does not have dynamodb:UpdateItem "
                f"on CommandResults ({cr_lid})"
            )

    def test_gateway_roles_do_not_have_dynamodb_on_store_state_cache(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R9.1"""
        _, _, raw = synthesized
        ssc_lid = _find_table_logical_id(raw, "StoreStateCache")

        for fn_name in _GATEWAY_FUNCTION_NAMES:
            role_lid = _find_role_logical_id_for_lambda(raw, fn_name)
            assert role_lid is not None, f"No role found for {fn_name}"

            stmts = _collect_inline_policy_statements(raw, role_lid)
            for s in stmts:
                actions = s.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                ddb_actions = [a for a in actions if a.startswith("dynamodb:")]
                if not ddb_actions:
                    continue
                resources_str = json.dumps(s.get("Resource", []))
                assert ssc_lid not in resources_str, (
                    f"Role for {fn_name} has DynamoDB access on "
                    f"StoreStateCache ({ssc_lid}) — gateway Lambdas "
                    "must only access CommandResults"
                )


# ===================================================================
# 12.8 — No role has * resource on dynamodb:*, lambda:*, or bedrock:*
#         (R9.4)
# ===================================================================


class TestNoWildcardResources:
    """12.8: No role in the template has * resource on dynamodb:*,
    lambda:*, or bedrock:*."""

    def test_no_star_resource_on_sensitive_actions(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R9.4"""
        _, _, raw = synthesized
        sensitive_prefixes = ("dynamodb:", "lambda:", "bedrock:")
        violations: list[str] = []

        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::IAM::Role":
                continue
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
                flat = _flatten_resource_refs(resources)
                for r in flat:
                    if r == "*":
                        violations.append(
                            f"{lid}: actions={actions} resource=*"
                        )

        assert not violations, (
            "Roles with wildcard resources on sensitive actions "
            "(violates R9.4):\n  " + "\n  ".join(violations)
        )


# ===================================================================
# 12.9 — No role has AdministratorAccess or *FullAccess managed policy
#         (R9.5)
# ===================================================================


class TestNoFullAccessPolicies:
    """12.9: No role has a managed policy named AdministratorAccess or
    ending in FullAccess."""

    def test_no_admin_or_full_access_managed_policies(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R9.5"""
        _, _, raw = synthesized
        violations: list[str] = []

        for lid, res in raw.get("Resources", {}).items():
            if res.get("Type") != "AWS::IAM::Role":
                continue
            props = res.get("Properties", {})
            managed = props.get("ManagedPolicyArns", [])
            for policy_arn in managed:
                arn_str = json.dumps(policy_arn) if not isinstance(policy_arn, str) else policy_arn
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


# ===================================================================
# 12.10 — Every resource carries the four required tags
#          (R1.4)
# ===================================================================


class TestResourceTags:
    """12.10: Every resource carries the four required tags: Project,
    Environment, ManagedBy, Spec."""

    def test_all_taggable_resources_have_required_tags(
        self, synthesized: tuple[CloudMcpGatewayStack, Template, dict]
    ) -> None:
        """Validates: Requirements R1.4

        Checks all resources that support a ``Tags`` property. CDK's
        stack-level ``Tags.of(self)`` propagates tags to every taggable
        construct, so this test verifies that propagation is working.

        Tags appear in two formats in CloudFormation:
        - Standard: ``[{"Key": "k", "Value": "v"}, ...]`` (most resources)
        - Dict: ``{"k": "v", ...}`` (some L1 constructs like BedrockAgentCore)
        Both formats are accepted.
        """
        _, _, raw = synthesized

        # Resource types that support Tags in CloudFormation and are
        # created by this stack. We check all resources that have a
        # Tags property rather than maintaining a hardcoded list.
        violations: list[str] = []

        for lid, res in raw.get("Resources", {}).items():
            props = res.get("Properties", {})
            tags = props.get("Tags")

            # Skip resources that don't have a Tags property at all
            # (e.g., AWS::IAM::Policy, AWS::CDK::Metadata).
            if tags is None:
                continue

            # Build a dict of tag key -> value from the resource's tags.
            # Handle both the standard list-of-objects format and the
            # dict format used by some L1 constructs (e.g., CfnRuntime).
            tag_dict: dict[str, str] = {}
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, dict) and "Key" in tag and "Value" in tag:
                        tag_dict[tag["Key"]] = tag["Value"]
            elif isinstance(tags, dict):
                tag_dict = {k: v for k, v in tags.items() if isinstance(v, str)}

            for req_key, req_value in _REQUIRED_TAGS.items():
                if tag_dict.get(req_key) != req_value:
                    violations.append(
                        f"{lid} ({res.get('Type', '?')}): missing or wrong "
                        f"tag {req_key}={req_value!r} "
                        f"(has {tag_dict.get(req_key)!r})"
                    )

        assert not violations, (
            f"Resources missing required tags (R1.4):\n  "
            + "\n  ".join(violations)
        )
