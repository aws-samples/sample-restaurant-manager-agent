# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB table tests for the cloud-tool-relay stack (spec 006, tasks 2.1, 2.2, and 2.3).

Spec 006 requirements covered here:

* **R3.1** — the stack creates a ``CommandResults`` DynamoDB table with
  partition key ``correlation_id`` (string), no sort key, on-demand
  billing, ``expires_at`` TTL, point-in-time recovery disabled, and
  server-side encryption with the AWS-owned KMS key.
* **R3.2** — the stack creates a ``StoreStateCache`` DynamoDB table
  with partition key ``store_domain_key`` (string, format
  ``{store_id}#{domain}``), no sort key, on-demand billing, **no TTL**
  (intentional — the cache must survive long outages to be useful),
  point-in-time recovery disabled, and server-side encryption with the
  AWS-owned KMS key.
* **R3.3** — both tables (``CommandResults`` and ``StoreStateCache``)
  carry ``RemovalPolicy.DESTROY`` so ``cdk destroy`` leaves nothing
  behind in dev. A code comment in the stack notes that production would
  use ``RETAIN``.

Task 2.1 covers ``CommandResults``; task 2.2 adds ``StoreStateCache``
alongside and the cross-table assertion in the middle of this file
guards against a future refactor accidentally collapsing the two
tables into one. Task 2.3 exposes table names / ARNs as additional
stack attributes (``command_results_table_name``, ``_table_arn``, and
the ``store_state_cache_*`` mirror) so downstream consumers — gateway
Lambda env vars + IAM (task 4), Fargate task def env vars + task role
(task 5), and stack outputs (task 11) — can grab either handle without
reaching through the L2 construct.

Tests follow the pattern established in ``test_stack_tags.py``: one
module-scoped fixture synthesizes the stack and returns both the CDK
``Template`` matcher wrapper and the raw CloudFormation JSON, so every
assertion in this file pays the synth cost exactly once.
"""
from __future__ import annotations

from typing import Final

import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk.assertions import Template

from infrastructure.cloud_mcp_gateway_stack import CloudMcpGatewayStack


#: Logical (CloudFormation ``TableName``) name of the correlation-id keyed
#: table. Spec 006 R3.1 pins this string so downstream consumers (the
#: gateway Lambdas, the Fargate subscriber, and the ``CommandResultsTableName``
#: stack output from task 11) can reference it deterministically.
_COMMAND_RESULTS_TABLE_NAME: Final[str] = "CommandResults"

#: Logical (CloudFormation ``TableName``) name of the domain-scoped
#: last-known-good cache table. Spec 006 R3.2 pins this string for the
#: same reasons as ``_COMMAND_RESULTS_TABLE_NAME``: the Fargate response
#: handler (task 5) writes to it by name, domain agents (task 8) read
#: from it by name, and task 11 emits it as ``StoreStateCacheTableName``
#: via a stack output.
_STORE_STATE_CACHE_TABLE_NAME: Final[str] = "StoreStateCache"


@pytest.fixture(scope="module")
def synthesized() -> tuple[CloudMcpGatewayStack, Template, dict]:
    """Synthesize the stack once per test module.

    Returns a tuple of ``(stack, template, raw_template_dict)``:

    * ``stack`` — the live ``CloudMcpGatewayStack`` instance, used to
      assert stack-level attribute exposure (``self.command_results_table``).
    * ``template`` — the CDK ``Template`` matcher wrapper for
      schema-aware structural assertions (``has_resource_properties``,
      ``resource_count_is``, etc.).
    * ``raw_template_dict`` — the raw CloudFormation JSON, useful when
      we need to assert on top-level fields the matcher API does not
      surface ergonomically (``DeletionPolicy`` / ``UpdateReplacePolicy``
      live at the resource level, not inside ``Properties``).
    """
    app = cdk.App()
    stack = CloudMcpGatewayStack(
        app, "TestStack", env=cdk.Environment(region="us-west-2")
    )
    raw = app.synth().get_stack_by_name("TestStack").template
    return stack, Template.from_stack(stack), raw


def _find_command_results_resource(raw: dict) -> tuple[str, dict]:
    """Return the ``(logical_id, resource_dict)`` pair for the single
    ``AWS::DynamoDB::Table`` named ``CommandResults`` in the template.

    Raises ``AssertionError`` if the table is missing or if more than one
    DynamoDB table with that physical name exists. Task 2.2 will add a
    second table (``StoreStateCache``), but its ``TableName`` will be
    distinct, so this helper stays stable across tasks.
    """
    matches = [
        (lid, res)
        for lid, res in raw.get("Resources", {}).items()
        if res.get("Type") == "AWS::DynamoDB::Table"
        and res.get("Properties", {}).get("TableName")
        == _COMMAND_RESULTS_TABLE_NAME
    ]
    assert len(matches) == 1, (
        f"expected exactly one AWS::DynamoDB::Table named "
        f"{_COMMAND_RESULTS_TABLE_NAME!r}; found {len(matches)}"
    )
    return matches[0]


def _find_store_state_cache_resource(raw: dict) -> tuple[str, dict]:
    """Return the ``(logical_id, resource_dict)`` pair for the single
    ``AWS::DynamoDB::Table`` named ``StoreStateCache`` in the template.

    Mirrors ``_find_command_results_resource`` — a near-duplicate by
    design rather than a parameterized helper, because the two tables
    are independent assertions in the spec (R3.1 vs R3.2) and keeping
    one helper per table keeps failure messages pinpoint-clear about
    which table misbehaved.
    """
    matches = [
        (lid, res)
        for lid, res in raw.get("Resources", {}).items()
        if res.get("Type") == "AWS::DynamoDB::Table"
        and res.get("Properties", {}).get("TableName")
        == _STORE_STATE_CACHE_TABLE_NAME
    ]
    assert len(matches) == 1, (
        f"expected exactly one AWS::DynamoDB::Table named "
        f"{_STORE_STATE_CACHE_TABLE_NAME!r}; found {len(matches)}"
    )
    return matches[0]


def test_command_results_table_exists_with_correct_name(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1: the synthesized template contains exactly one
    ``AWS::DynamoDB::Table`` with physical name ``CommandResults``.

    A deterministic physical name is required because task 11 emits the
    table name as a CloudFormation output (``CommandResultsTableName``)
    and downstream specs / smoke tests reference it directly rather than
    via an ``Fn::Ref``.
    """
    _, template, raw = synthesized

    # Task 2.1 adds one table; task 2.2 will add ``StoreStateCache``.
    # Scope the count assertion to the ``CommandResults`` name so this
    # test keeps working after task 2.2 lands.
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TableName": _COMMAND_RESULTS_TABLE_NAME},
    )
    _find_command_results_resource(raw)  # also asserts uniqueness by name


def test_command_results_partition_key_is_correlation_id_string(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1: the partition key is ``correlation_id`` of type ``S``, and
    there is no sort key.

    The gateway Lambda generates a UUID v4 per tool invocation; that
    UUID is the only identifier the request and response share. A sort
    key would add no value (at most one row per correlation id) and
    would complicate the response handler's ``UpdateItem`` call.
    """
    _, _, raw = synthesized
    _, resource = _find_command_results_resource(raw)
    props = resource["Properties"]

    # Partition key declared as HASH in the KeySchema.
    key_schema = props["KeySchema"]
    hash_keys = [k for k in key_schema if k["KeyType"] == "HASH"]
    assert hash_keys == [{"AttributeName": "correlation_id", "KeyType": "HASH"}]

    # No RANGE (sort) key present.
    range_keys = [k for k in key_schema if k["KeyType"] == "RANGE"]
    assert range_keys == []

    # AttributeDefinitions declares ``correlation_id`` as a string (``S``).
    attr_defs = props["AttributeDefinitions"]
    assert {"AttributeName": "correlation_id", "AttributeType": "S"} in attr_defs


def test_command_results_billing_mode_is_on_demand(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1: billing mode is ``PAY_PER_REQUEST`` (on-demand).

    Dev traffic is bursty and low-volume; on-demand removes capacity
    planning and is cheaper than the minimum provisioned throughput at
    this scale.
    """
    _, template, _ = synthesized
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": _COMMAND_RESULTS_TABLE_NAME,
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_command_results_ttl_attribute_is_expires_at(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1: the TTL specification enables ``expires_at`` as the TTL
    attribute.

    Gateway Lambdas set ``expires_at = now + 300s`` on each pending
    record. Table-level TTL means rows from failed / dropped flows
    disappear on their own instead of accumulating indefinitely.
    """
    _, template, _ = synthesized
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": _COMMAND_RESULTS_TABLE_NAME,
            "TimeToLiveSpecification": {
                "AttributeName": "expires_at",
                "Enabled": True,
            },
        },
    )


def test_command_results_pitr_disabled(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1/R3.2: point-in-time recovery is disabled.

    Why, per R3.4 (added 2026-08-07): the data is RECONSTRUCTIBLE, not merely
    non-production. Both tables are rebuilt by ordinary traffic, so a restore
    recovers nothing a live round-trip does not -- which is why cdk-nag DDB3 is
    suppressed here rather than fixed. This does not flip in production.

    CloudFormation represents disabled PITR in one of two equivalent
    ways: either no ``PointInTimeRecoverySpecification`` block at all,
    or a block with ``PointInTimeRecoveryEnabled: false``. This stack
    omits the parameter (CDK's default), so the block is absent — but
    we accept either shape here to stay resilient to an incidental CDK
    upgrade that flips the default rendering.
    """
    _, _, raw = synthesized
    _, resource = _find_command_results_resource(raw)
    props = resource["Properties"]

    pitr = props.get("PointInTimeRecoverySpecification")
    if pitr is None:
        # Absent is fine — disabled is the CloudFormation default.
        return
    # If present, must explicitly declare disabled.
    assert pitr == {"PointInTimeRecoveryEnabled": False}, (
        f"CommandResults PITR is not disabled: {pitr!r}"
    )


def test_command_results_encryption_is_aws_owned(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.1: server-side encryption uses the AWS-owned KMS key.

    DynamoDB's default encryption is with an AWS-owned key, and that
    default renders as *no* ``SSESpecification`` block in the synthesized
    CloudFormation (or, if present, ``SSEEnabled: false`` — an
    AWS-managed or customer-managed KMS key would set ``SSEEnabled: true``
    plus a ``SSEType`` of ``KMS``). Either "absent" or "present with
    SSEEnabled=false" therefore proves the table uses the AWS-owned key.
    """
    _, _, raw = synthesized
    _, resource = _find_command_results_resource(raw)
    props = resource["Properties"]

    sse = props.get("SSESpecification")
    if sse is None:
        # Absent → AWS-owned key (the DynamoDB default). ✓
        return
    # If present, must declare SSE explicitly disabled — still AWS-owned.
    assert sse.get("SSEEnabled") is False, (
        f"CommandResults SSESpecification implies a non-AWS-owned key: "
        f"{sse!r}"
    )


def test_command_results_removal_policy_is_destroy(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.3: the table's ``DeletionPolicy`` and ``UpdateReplacePolicy``
    are both ``Delete`` so ``cdk destroy`` takes the table with it in
    dev.

    These policies live at the resource top level in CloudFormation, not
    inside ``Properties``, which is why this test walks the raw template
    rather than using ``Template.has_resource_properties``.
    """
    _, _, raw = synthesized
    _, resource = _find_command_results_resource(raw)

    assert resource.get("DeletionPolicy") == "Delete", (
        f"CommandResults DeletionPolicy must be 'Delete' for "
        f"RemovalPolicy.DESTROY; got {resource.get('DeletionPolicy')!r}"
    )
    assert resource.get("UpdateReplacePolicy") == "Delete", (
        f"CommandResults UpdateReplacePolicy must be 'Delete' for "
        f"RemovalPolicy.DESTROY; got {resource.get('UpdateReplacePolicy')!r}"
    )


def test_stack_exposes_command_results_table_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Handoff: the stack surfaces the table as
    ``self.command_results_table`` so later tasks (the gateway Lambda
    role in task 4.4, the Fargate task role in task 5.5, and the
    ``CommandResultsTableName`` output in task 11) can grant / reference
    permissions on it without a second lookup.

    Accepting any ``dynamodb.ITable`` keeps the test robust to a future
    refactor that swaps the concrete ``Table`` for, say, a ``TableV2``
    or a construct that wraps the L2 table with extra behaviour.
    """
    stack, _, _ = synthesized

    assert hasattr(stack, "command_results_table"), (
        "CloudMcpGatewayStack must expose ``self.command_results_table`` "
        "so later tasks can grant permissions on the CommandResults table"
    )
    # ``dynamodb.ITable`` is a non-runtime-checkable ``Protocol`` in the
    # pinned CDK version, so ``isinstance(..., ITable)`` raises ``TypeError``.
    # The concrete L2 ``Table`` construct implements ``ITable``, so asserting
    # on the concrete class proves the same interface contract without
    # fighting ``typing_extensions``. If a future task swaps to a wrapping
    # construct, relax this check to ``dynamodb.Table`` plus a duck-typed
    # attribute probe (``hasattr(table, "table_name")`` and
    # ``hasattr(table, "grant_read_write_data")``).
    assert isinstance(stack.command_results_table, dynamodb.Table), (
        "stack.command_results_table must be an aws_cdk.aws_dynamodb.Table; "
        f"got {type(stack.command_results_table).__name__}"
    )


# ----------------------------------------------------------------------
# Task 2.2 — ``StoreStateCache`` table (R3.2, R3.3)
# ----------------------------------------------------------------------
#
# Structural mirror of the ``CommandResults`` block above. The key
# differences the spec calls out (and these tests enforce):
#
# * Partition key is ``store_domain_key``, not ``correlation_id``.
# * **No TTL** — the cache must survive long outages. Testing this
#   requires a tolerant assertion: CDK may omit the block entirely or,
#   depending on version, emit ``TimeToLiveSpecification.Enabled=false``.
#   Either shape proves "no TTL is enabled" and therefore passes.
# * Same on-demand billing, same AWS-owned KMS encryption, same PITR
#   disabled, same ``RemovalPolicy.DESTROY``.


def test_store_state_cache_table_exists_with_correct_name(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: the synthesized template contains exactly one
    ``AWS::DynamoDB::Table`` with physical name ``StoreStateCache``.

    A deterministic physical name is required because task 11 emits the
    table name as a CloudFormation output (``StoreStateCacheTableName``)
    and the Fargate response handler (task 5) + domain agents (task 8)
    reference it directly rather than via an ``Fn::Ref``.
    """
    _, template, raw = synthesized

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TableName": _STORE_STATE_CACHE_TABLE_NAME},
    )
    _find_store_state_cache_resource(raw)  # asserts uniqueness by name


def test_store_state_cache_partition_key_is_store_domain_key_string(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: the partition key is ``store_domain_key`` of type ``S``,
    with no sort key.

    Values follow the ``{store_id}#{domain}`` format (design.md D4) so
    each ``(store, domain)`` pair maps to exactly one item. Adding a
    sort key later (e.g., ``tool_name`` for per-tool granularity) is a
    backward-compatible evolution, but right now the schema is
    deliberately flat.
    """
    _, _, raw = synthesized
    _, resource = _find_store_state_cache_resource(raw)
    props = resource["Properties"]

    key_schema = props["KeySchema"]
    hash_keys = [k for k in key_schema if k["KeyType"] == "HASH"]
    assert hash_keys == [
        {"AttributeName": "store_domain_key", "KeyType": "HASH"}
    ]

    range_keys = [k for k in key_schema if k["KeyType"] == "RANGE"]
    assert range_keys == []

    attr_defs = props["AttributeDefinitions"]
    assert {
        "AttributeName": "store_domain_key",
        "AttributeType": "S",
    } in attr_defs


def test_store_state_cache_billing_mode_is_on_demand(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: billing mode is ``PAY_PER_REQUEST`` (on-demand).

    Same rationale as ``CommandResults``: bursty dev traffic, no
    capacity planning, cheaper than the minimum provisioned throughput
    at this scale.
    """
    _, template, _ = synthesized
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": _STORE_STATE_CACHE_TABLE_NAME,
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_store_state_cache_has_no_ttl(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: ``StoreStateCache`` has **no** enabled TTL.

    This is a deliberate departure from ``CommandResults``. The
    fallback cache must survive a store going offline for hours or
    days, so any expiration would silently evaporate the cache exactly
    when the fallback is most needed. Write-through on each successful
    read keeps entries fresh under normal operation.

    CloudFormation represents "no TTL" in two equivalent ways:
    either no ``TimeToLiveSpecification`` block at all (what CDK emits
    when ``time_to_live_attribute`` is not passed — the stack's current
    shape), or a block with ``Enabled: false``. Accept either so a
    future CDK upgrade that starts emitting an explicit-disabled block
    won't spuriously fail this test; reject any block that declares
    ``Enabled: true``.
    """
    _, _, raw = synthesized
    _, resource = _find_store_state_cache_resource(raw)
    props = resource["Properties"]

    ttl = props.get("TimeToLiveSpecification")
    if ttl is None:
        # Absent — no TTL configured at all. ✓
        return
    # If present, must be explicitly disabled.
    assert ttl.get("Enabled") is False, (
        f"StoreStateCache must have no enabled TTL; got "
        f"TimeToLiveSpecification={ttl!r}"
    )


def test_store_state_cache_pitr_disabled(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: point-in-time recovery is disabled (dev default).

    Tolerant shape, matching ``test_command_results_pitr_disabled``:
    the ``PointInTimeRecoverySpecification`` block may be absent (CDK
    default) or present-with-disabled. Either proves PITR is off.
    """
    _, _, raw = synthesized
    _, resource = _find_store_state_cache_resource(raw)
    props = resource["Properties"]

    pitr = props.get("PointInTimeRecoverySpecification")
    if pitr is None:
        return
    assert pitr == {"PointInTimeRecoveryEnabled": False}, (
        f"StoreStateCache PITR is not disabled: {pitr!r}"
    )


def test_store_state_cache_encryption_is_aws_owned(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.2: server-side encryption uses the AWS-owned KMS key.

    DynamoDB's default encryption is with an AWS-owned key, which
    renders as *no* ``SSESpecification`` block (or, equivalently, a
    block with ``SSEEnabled: false``). Either shape proves the table
    is not using an AWS-managed or customer-managed KMS key.
    """
    _, _, raw = synthesized
    _, resource = _find_store_state_cache_resource(raw)
    props = resource["Properties"]

    sse = props.get("SSESpecification")
    if sse is None:
        return
    assert sse.get("SSEEnabled") is False, (
        f"StoreStateCache SSESpecification implies a non-AWS-owned key: "
        f"{sse!r}"
    )


def test_store_state_cache_removal_policy_is_destroy(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """R3.3: the table's ``DeletionPolicy`` and ``UpdateReplacePolicy``
    are both ``Delete`` so ``cdk destroy`` takes the table with it in
    dev.

    These policies live at the resource top level, outside ``Properties``
    — same walk as the ``CommandResults`` equivalent.
    """
    _, _, raw = synthesized
    _, resource = _find_store_state_cache_resource(raw)

    assert resource.get("DeletionPolicy") == "Delete", (
        f"StoreStateCache DeletionPolicy must be 'Delete' for "
        f"RemovalPolicy.DESTROY; got {resource.get('DeletionPolicy')!r}"
    )
    assert resource.get("UpdateReplacePolicy") == "Delete", (
        f"StoreStateCache UpdateReplacePolicy must be 'Delete' for "
        f"RemovalPolicy.DESTROY; got {resource.get('UpdateReplacePolicy')!r}"
    )


def test_stack_exposes_store_state_cache_table_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Handoff: the stack surfaces the table as
    ``self.store_state_cache_table`` so later tasks can grant / reference
    it without a second lookup.

    The Fargate response handler (task 5) gets PutItem on this table;
    each domain agent (task 8) gets GetItem for the timeout-fallback
    branch of its async shim. Neither the gateway Lambdas (task 4) nor
    the supervisor agent (task 9) has any access — R9.1 and R9.3 make
    that exclusion explicit.
    """
    stack, _, _ = synthesized

    assert hasattr(stack, "store_state_cache_table"), (
        "CloudMcpGatewayStack must expose ``self.store_state_cache_table`` "
        "so later tasks can grant permissions on the StoreStateCache table"
    )
    # See the note in ``test_stack_exposes_command_results_table_attribute``:
    # ``dynamodb.ITable`` is a non-runtime-checkable ``Protocol``, so we
    # assert on the concrete L2 ``Table`` class instead of the interface.
    assert isinstance(stack.store_state_cache_table, dynamodb.Table), (
        "stack.store_state_cache_table must be an aws_cdk.aws_dynamodb.Table; "
        f"got {type(stack.store_state_cache_table).__name__}"
    )


def test_both_tables_are_distinct_resources(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Cross-table guard: the template contains exactly two
    ``AWS::DynamoDB::Table`` resources, and their ``TableName`` values
    are ``{"CommandResults", "StoreStateCache"}``.

    This catches a class of regressions that neither per-table test
    would: if a future refactor accidentally merged the two tables, or
    added a third DynamoDB table without updating this spec, one of
    those conditions would trip this assertion loudly. The per-table
    tests above would each still pass because each named table still
    exists — but the shape of the stack would have drifted from the
    design.
    """
    _, template, raw = synthesized

    # CDK Template matcher for the count — strict and schema-aware.
    template.resource_count_is("AWS::DynamoDB::Table", 2)

    # Belt-and-braces: also verify the names via the raw template so the
    # failure message points at which names are wrong if the count
    # happens to match coincidentally (e.g., both ``CommandResults`` and
    # a stray second table accidentally sharing ``CommandResults``).
    table_names = {
        res["Properties"]["TableName"]
        for res in raw.get("Resources", {}).values()
        if res.get("Type") == "AWS::DynamoDB::Table"
    }
    assert table_names == {
        _COMMAND_RESULTS_TABLE_NAME,
        _STORE_STATE_CACHE_TABLE_NAME,
    }, (
        f"expected exactly these two DynamoDB table names: "
        f"{{{_COMMAND_RESULTS_TABLE_NAME!r}, {_STORE_STATE_CACHE_TABLE_NAME!r}}}; "
        f"got {table_names!r}"
    )


# ----------------------------------------------------------------------
# Task 2.3 — Table name / ARN handoff attributes
# ----------------------------------------------------------------------
#
# Task 2.3 exposes four thin accessors on the stack — two names and two
# ARNs, one pair per table — so downstream consumers don't have to reach
# through the L2 ``_table`` construct to pull a handle out. The tests
# below assert each attribute exists, is ``str``-typed (CDK tokens are
# ``str`` instances), and resolves at synth time to the expected
# CloudFormation intrinsic:
#
# * ``*_table_name`` → ``{"Ref": "<LogicalId>"}`` (DynamoDB's ``Ref``
#   returns the table's physical name).
# * ``*_table_arn``  → ``{"Fn::GetAtt": ["<LogicalId>", "Arn"]}``.
#
# The logical ids are looked up dynamically from the synthesized template
# (by scanning for ``AWS::DynamoDB::Table`` resources with the matching
# ``TableName`` property) rather than hard-coded, so these tests stay
# stable across CDK-generated hash-suffix changes.


def _logical_id_for_table(raw: dict, table_name: str) -> str:
    """Return the CloudFormation logical id of the single
    ``AWS::DynamoDB::Table`` resource whose ``TableName`` property equals
    ``table_name``.

    The per-table ``_find_*_resource`` helpers above already assert
    uniqueness-by-name for each of the two tables, so this wrapper is a
    thin convenience that keeps the task 2.3 tests symmetrical for both
    tables without duplicating the lookup logic.
    """
    matches = [
        lid
        for lid, res in raw.get("Resources", {}).items()
        if res.get("Type") == "AWS::DynamoDB::Table"
        and res.get("Properties", {}).get("TableName") == table_name
    ]
    assert len(matches) == 1, (
        f"expected exactly one AWS::DynamoDB::Table named {table_name!r}; "
        f"found {len(matches)}"
    )
    return matches[0]


def test_stack_exposes_command_results_table_name_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Task 2.3: ``self.command_results_table_name`` exists, is
    ``str``-typed, and resolves to ``{"Ref": <CommandResults logical id>}``
    at synth.

    DynamoDB's CloudFormation ``Ref`` returns the table's physical name,
    so this attribute renders into env vars / outputs as the string
    ``"CommandResults"`` once CloudFormation processes the template.
    """
    stack, _, raw = synthesized

    assert hasattr(stack, "command_results_table_name"), (
        "CloudMcpGatewayStack must expose ``self.command_results_table_name`` "
        "so task 4.3 / 5.4 / 11.1 can plumb the table name into Lambda / "
        "ECS env vars and CloudFormation outputs"
    )
    # CDK tokens are ``str`` instances (``Token.as_string`` wraps them).
    assert isinstance(stack.command_results_table_name, str)

    logical_id = _logical_id_for_table(raw, _COMMAND_RESULTS_TABLE_NAME)
    resolved = stack.resolve(stack.command_results_table_name)
    assert resolved == {"Ref": logical_id}, (
        f"command_results_table_name did not resolve to Ref({logical_id!r}); "
        f"got {resolved!r}"
    )


def test_stack_exposes_command_results_table_arn_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Task 2.3: ``self.command_results_table_arn`` exists, is ``str``-typed,
    and resolves to ``{"Fn::GetAtt": [<logical id>, "Arn"]}``.

    The ARN is what ``iam.PolicyStatement.resources`` needs for
    least-privilege grants (task 4.4's gateway Lambda role, task 5.5's
    Fargate task role, task 10.2's agent role DDB read scope).
    """
    stack, _, raw = synthesized

    assert hasattr(stack, "command_results_table_arn"), (
        "CloudMcpGatewayStack must expose ``self.command_results_table_arn`` "
        "so IAM policy statements in tasks 4.4 / 5.5 / 10.2 can scope "
        "grants to exactly this table"
    )
    assert isinstance(stack.command_results_table_arn, str)

    logical_id = _logical_id_for_table(raw, _COMMAND_RESULTS_TABLE_NAME)
    resolved = stack.resolve(stack.command_results_table_arn)
    assert resolved == {"Fn::GetAtt": [logical_id, "Arn"]}, (
        f"command_results_table_arn did not resolve to "
        f"Fn::GetAtt({logical_id!r}, 'Arn'); got {resolved!r}"
    )


def test_stack_exposes_store_state_cache_table_name_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Task 2.3: mirror of the ``CommandResults`` name assertion for the
    ``StoreStateCache`` table.

    Used by: task 5.4 Fargate env var, task 8 domain agent env var (for
    the timeout-fallback ``GetItem``), task 11.1 ``StoreStateCacheTableName``
    stack output.
    """
    stack, _, raw = synthesized

    assert hasattr(stack, "store_state_cache_table_name"), (
        "CloudMcpGatewayStack must expose ``self.store_state_cache_table_name``"
    )
    assert isinstance(stack.store_state_cache_table_name, str)

    logical_id = _logical_id_for_table(raw, _STORE_STATE_CACHE_TABLE_NAME)
    resolved = stack.resolve(stack.store_state_cache_table_name)
    assert resolved == {"Ref": logical_id}, (
        f"store_state_cache_table_name did not resolve to Ref({logical_id!r}); "
        f"got {resolved!r}"
    )


def test_stack_exposes_store_state_cache_table_arn_attribute(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Task 2.3: mirror of the ``CommandResults`` ARN assertion for the
    ``StoreStateCache`` table.

    Used by: task 5.5 Fargate task role (PutItem/UpdateItem/GetItem),
    task 10.2 domain agent role (GetItem only).
    """
    stack, _, raw = synthesized

    assert hasattr(stack, "store_state_cache_table_arn"), (
        "CloudMcpGatewayStack must expose ``self.store_state_cache_table_arn``"
    )
    assert isinstance(stack.store_state_cache_table_arn, str)

    logical_id = _logical_id_for_table(raw, _STORE_STATE_CACHE_TABLE_NAME)
    resolved = stack.resolve(stack.store_state_cache_table_arn)
    assert resolved == {"Fn::GetAtt": [logical_id, "Arn"]}, (
        f"store_state_cache_table_arn did not resolve to "
        f"Fn::GetAtt({logical_id!r}, 'Arn'); got {resolved!r}"
    )


def test_handoff_attributes_point_to_correct_tables(
    synthesized: tuple[CloudMcpGatewayStack, Template, dict],
) -> None:
    """Task 2.3 sanity check: each handoff attribute resolves against the
    *correct* table's logical id — the ``command_results_*`` pair against
    the ``CommandResults`` table and the ``store_state_cache_*`` pair
    against the ``StoreStateCache`` table, not crossed.

    A swap regression (e.g. a copy-paste that assigned
    ``self.command_results_table_arn = self.store_state_cache_table.table_arn``)
    would leak permissions and env-var values across tables without
    tripping any of the per-attribute shape tests above — each of those
    only asserts the intrinsic shape, not which table it points at. This
    test closes that gap.
    """
    stack, _, raw = synthesized

    command_results_lid = _logical_id_for_table(
        raw, _COMMAND_RESULTS_TABLE_NAME
    )
    store_state_cache_lid = _logical_id_for_table(
        raw, _STORE_STATE_CACHE_TABLE_NAME
    )

    # The two logical ids must be distinct — if they're not, the whole
    # premise of this test collapses. ``_find_*_resource`` already asserts
    # each name maps to exactly one resource, so this is belt-and-braces.
    assert command_results_lid != store_state_cache_lid

    # ``command_results_*`` points at the CommandResults table.
    assert stack.resolve(stack.command_results_table_name) == {
        "Ref": command_results_lid
    }
    assert stack.resolve(stack.command_results_table_arn) == {
        "Fn::GetAtt": [command_results_lid, "Arn"]
    }

    # ``store_state_cache_*`` points at the StoreStateCache table.
    assert stack.resolve(stack.store_state_cache_table_name) == {
        "Ref": store_state_cache_lid
    }
    assert stack.resolve(stack.store_state_cache_table_arn) == {
        "Fn::GetAtt": [store_state_cache_lid, "Arn"]
    }
