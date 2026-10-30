# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``lambdas.shared.ddb`` — DynamoDB helpers for
``CommandResults`` and ``StoreStateCache``.

Uses ``moto`` to mock DynamoDB so tests run without AWS credentials.

Requirements covered:
    R4.4  — ``PutItem`` pending record on ``CommandResults``.
    R4.9  — ``UpdateItem`` to ``status="failed"`` on publish failure.
    R5.2  — ``UpdateItem`` to ``status="complete"`` / ``"error"``.
    R5.3  — ``PutItem`` write-through on ``StoreStateCache``.
    R5.5  — idempotent updates.
    R15.5 — unit tests for the helpers.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lambdas.shared.correlation import (
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PENDING,
    build_complete_update,
    build_error_update,
    build_failed_update,
    build_pending_item,
)
from lambdas.shared.ddb import (
    get_command_result,
    get_state_cache,
    put_pending_command,
    put_state_cache,
    update_command_result,
)

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

_REGION = "us-west-2"
_COMMAND_RESULTS_TABLE = "CommandResults"
_STORE_STATE_CACHE_TABLE = "StoreStateCache"


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------


@pytest.fixture()
def dynamodb_tables():
    """Create both DynamoDB tables in moto and yield their names.

    The tables mirror the CDK stack's configuration (R3.1, R3.2):
    - ``CommandResults``: PK ``correlation_id`` (S), TTL ``expires_at``.
    - ``StoreStateCache``: PK ``store_domain_key`` (S), no TTL.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)

        # CommandResults (R3.1)
        client.create_table(
            TableName=_COMMAND_RESULTS_TABLE,
            KeySchema=[
                {"AttributeName": "correlation_id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "correlation_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # StoreStateCache (R3.2)
        client.create_table(
            TableName=_STORE_STATE_CACHE_TABLE,
            KeySchema=[
                {"AttributeName": "store_domain_key", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "store_domain_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {
            "command_results": _COMMAND_RESULTS_TABLE,
            "store_state_cache": _STORE_STATE_CACHE_TABLE,
        }


# -----------------------------------------------------------------------
# CommandResults — put_pending_command
# -----------------------------------------------------------------------


class TestPutPendingCommand:
    """Verify ``put_pending_command`` writes the correct item shape."""

    def test_writes_pending_item(self, dynamodb_tables: dict) -> None:
        item = build_pending_item(
            correlation_id="cid-001",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
            parameters={"verbose": True},
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-001",
            region=_REGION,
        )
        assert result is not None
        assert result["correlation_id"] == "cid-001"
        assert result["tool_name"] == "get_equipment_status"
        assert result["store_id"] == "sim-store-001"
        assert result["domain"] == "equipment"
        assert result["status"] == STATUS_PENDING
        assert result["parameters"] == {"verbose": True}
        assert "created_at" in result
        assert "expires_at" in result

    def test_expires_at_is_in_the_future(self, dynamodb_tables: dict) -> None:
        now = int(time.time())
        item = build_pending_item(
            correlation_id="cid-002",
            tool_name="get_stock_levels",
            store_id="sim-store-001",
            domain="inventory",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-002",
            region=_REGION,
        )
        assert result is not None
        assert int(result["expires_at"]) > now


# -----------------------------------------------------------------------
# CommandResults — update_command_result
# -----------------------------------------------------------------------


class TestUpdateCommandResult:
    """Verify ``update_command_result`` transitions item status."""

    def test_update_to_complete(self, dynamodb_tables: dict) -> None:
        """R5.2: response handler marks a record ``complete``."""
        item = build_pending_item(
            correlation_id="cid-010",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        updates = build_complete_update(
            result={"fryer": "350F"},
            duration_ms=4500,
        )
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-010",
            updates=updates,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-010",
            region=_REGION,
        )
        assert result is not None
        assert result["status"] == STATUS_COMPLETE
        assert result["result"] == {"fryer": "350F"}
        assert result["duration_ms"] == 4500
        assert "completed_at" in result
        # Original fields preserved.
        assert result["tool_name"] == "get_equipment_status"
        assert result["store_id"] == "sim-store-001"

    def test_update_to_error(self, dynamodb_tables: dict) -> None:
        """R5.2: response handler marks a record ``error``."""
        item = build_pending_item(
            correlation_id="cid-011",
            tool_name="void_order",
            store_id="sim-store-001",
            domain="pos",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        updates = build_error_update(error_message="store timeout")
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-011",
            updates=updates,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-011",
            region=_REGION,
        )
        assert result is not None
        assert result["status"] == STATUS_ERROR
        assert result["error_message"] == "store timeout"

    def test_update_to_failed(self, dynamodb_tables: dict) -> None:
        """R4.9: gateway Lambda marks a record ``failed`` on MQTT error."""
        item = build_pending_item(
            correlation_id="cid-012",
            tool_name="get_daily_sales",
            store_id="sim-store-001",
            domain="pos",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        updates = build_failed_update(error_message="MQTT PUBLISH failed")
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-012",
            updates=updates,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-012",
            region=_REGION,
        )
        assert result is not None
        assert result["status"] == STATUS_FAILED
        assert result["error_message"] == "MQTT PUBLISH failed"

    def test_idempotent_complete_update(self, dynamodb_tables: dict) -> None:
        """R5.5: re-writing ``complete`` → ``complete`` is safe."""
        item = build_pending_item(
            correlation_id="cid-013",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        updates = build_complete_update(
            result={"fryer": "350F"},
            duration_ms=100,
        )

        # Apply the same update twice.
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-013",
            updates=updates,
            region=_REGION,
        )
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-013",
            updates=updates,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-013",
            region=_REGION,
        )
        assert result is not None
        assert result["status"] == STATUS_COMPLETE
        assert result["result"] == {"fryer": "350F"}

    def test_empty_updates_is_noop(self, dynamodb_tables: dict) -> None:
        """Passing an empty updates dict should not raise."""
        item = build_pending_item(
            correlation_id="cid-014",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        # Should be a no-op, not an error.
        update_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-014",
            updates={},
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-014",
            region=_REGION,
        )
        assert result is not None
        assert result["status"] == STATUS_PENDING


# -----------------------------------------------------------------------
# CommandResults — get_command_result
# -----------------------------------------------------------------------


class TestGetCommandResult:
    """Verify ``get_command_result`` retrieval."""

    def test_returns_none_for_missing_item(self, dynamodb_tables: dict) -> None:
        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="nonexistent",
            region=_REGION,
        )
        assert result is None

    def test_returns_item_when_present(self, dynamodb_tables: dict) -> None:
        item = build_pending_item(
            correlation_id="cid-020",
            tool_name="get_current_shifts",
            store_id="sim-store-001",
            domain="schedule",
        )
        put_pending_command(
            table_name=dynamodb_tables["command_results"],
            item=item,
            region=_REGION,
        )

        result = get_command_result(
            table_name=dynamodb_tables["command_results"],
            correlation_id="cid-020",
            region=_REGION,
        )
        assert result is not None
        assert result["correlation_id"] == "cid-020"


# -----------------------------------------------------------------------
# StoreStateCache — put_state_cache
# -----------------------------------------------------------------------


class TestPutStateCache:
    """Verify ``put_state_cache`` write-through (R5.3)."""

    def test_writes_cache_item(self, dynamodb_tables: dict) -> None:
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="equipment",
            result={"fryer": "350F"},
            correlation_id="cid-030",
            tool_name="get_equipment_status",
            region=_REGION,
        )

        cached = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="equipment",
            region=_REGION,
        )
        assert cached is not None
        assert cached["store_domain_key"] == "sim-store-001#equipment"
        assert cached["tool_name"] == "get_equipment_status"
        assert cached["last_known_state"] == {"fryer": "350F"}
        assert cached["last_correlation_id"] == "cid-030"
        assert "last_updated_at" in cached

    def test_overwrites_on_second_write(self, dynamodb_tables: dict) -> None:
        """R5.5: idempotent — second write replaces the first."""
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="pos",
            result={"total": 100},
            correlation_id="cid-031",
            tool_name="get_daily_sales",
            region=_REGION,
        )
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="pos",
            result={"total": 200},
            correlation_id="cid-032",
            tool_name="get_daily_sales",
            region=_REGION,
        )

        cached = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="pos",
            region=_REGION,
        )
        assert cached is not None
        assert cached["last_known_state"] == {"total": 200}
        assert cached["last_correlation_id"] == "cid-032"

    def test_different_domains_are_separate_items(self, dynamodb_tables: dict) -> None:
        """Each ``(store_id, domain)`` pair is a distinct cache entry."""
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="equipment",
            result={"fryer": "350F"},
            correlation_id="cid-040",
            tool_name="get_equipment_status",
            region=_REGION,
        )
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="inventory",
            result={"flour": 50},
            correlation_id="cid-041",
            tool_name="get_stock_levels",
            region=_REGION,
        )

        equip = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="equipment",
            region=_REGION,
        )
        inv = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="inventory",
            region=_REGION,
        )
        assert equip is not None
        assert inv is not None
        assert equip["last_known_state"] == {"fryer": "350F"}
        assert inv["last_known_state"] == {"flour": 50}


# -----------------------------------------------------------------------
# StoreStateCache — get_state_cache
# -----------------------------------------------------------------------


class TestGetStateCache:
    """Verify ``get_state_cache`` retrieval."""

    def test_returns_none_for_missing_item(self, dynamodb_tables: dict) -> None:
        cached = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="schedule",
            region=_REGION,
        )
        assert cached is None

    def test_returns_item_when_present(self, dynamodb_tables: dict) -> None:
        put_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="schedule",
            result={"shifts": []},
            correlation_id="cid-050",
            tool_name="get_current_shifts",
            region=_REGION,
        )

        cached = get_state_cache(
            table_name=dynamodb_tables["store_state_cache"],
            store_id="sim-store-001",
            domain="schedule",
            region=_REGION,
        )
        assert cached is not None
        assert cached["store_domain_key"] == "sim-store-001#schedule"
