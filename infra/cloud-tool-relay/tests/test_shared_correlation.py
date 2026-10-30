# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``lambdas.shared.correlation`` — UUID generation and
``CommandResults`` item shape helpers.

Requirements covered:
    R4.4  — UUID v4 correlation ID, pending record shape.
    R4.9  — ``status="failed"`` with ``error_message``.
    R5.2  — terminal-status update shapes (``complete`` / ``error``).
    R15.5 — unit tests for the helpers.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lambdas.shared.correlation import (
    DEFAULT_TTL_OFFSET_S,
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PENDING,
    build_complete_update,
    build_error_update,
    build_failed_update,
    build_mqtt_payload,
    build_pending_item,
    new_correlation_id,
)


# -----------------------------------------------------------------------
# UUID generation
# -----------------------------------------------------------------------


class TestNewCorrelationId:
    """Verify ``new_correlation_id`` produces valid UUID v4 strings."""

    def test_returns_string(self) -> None:
        cid = new_correlation_id()
        assert isinstance(cid, str)

    def test_is_valid_uuid4(self) -> None:
        cid = new_correlation_id()
        parsed = uuid.UUID(cid)
        assert parsed.version == 4

    def test_is_lowercase(self) -> None:
        cid = new_correlation_id()
        assert cid == cid.lower()

    def test_uniqueness(self) -> None:
        """Generate 1000 IDs and verify they are all distinct."""
        ids = {new_correlation_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_standard_uuid_format(self) -> None:
        """UUID string should be 36 characters: 8-4-4-4-12."""
        cid = new_correlation_id()
        assert len(cid) == 36
        parts = cid.split("-")
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]


# -----------------------------------------------------------------------
# Pending item builder
# -----------------------------------------------------------------------


class TestBuildPendingItem:
    """Verify the ``CommandResults`` pending item shape (R4.4)."""

    def test_required_fields_present(self) -> None:
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        assert item["correlation_id"] == "test-uuid"
        assert item["tool_name"] == "get_equipment_status"
        assert item["store_id"] == "sim-store-001"
        assert item["domain"] == "equipment"
        assert item["status"] == STATUS_PENDING
        assert "created_at" in item
        assert "expires_at" in item
        assert isinstance(item["parameters"], dict)

    def test_default_ttl_offset(self) -> None:
        before = int(time.time())
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        after = int(time.time())

        # expires_at should be created_at + DEFAULT_TTL_OFFSET_S (300 s).
        assert item["expires_at"] == item["created_at"] + DEFAULT_TTL_OFFSET_S
        # created_at should be within the test's time window.
        assert before <= item["created_at"] <= after

    def test_custom_ttl_offset(self) -> None:
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
            ttl_offset_s=600,
        )
        assert item["expires_at"] == item["created_at"] + 600

    def test_parameters_default_to_empty_dict(self) -> None:
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        assert item["parameters"] == {}

    def test_parameters_passed_through(self) -> None:
        params = {"store_id": "sim-store-001", "verbose": True}
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
            parameters=params,
        )
        assert item["parameters"] == params

    def test_status_is_pending(self) -> None:
        item = build_pending_item(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            store_id="sim-store-001",
            domain="equipment",
        )
        assert item["status"] == "pending"


# -----------------------------------------------------------------------
# MQTT payload builder
# -----------------------------------------------------------------------


class TestBuildMqttPayload:
    """Verify the MQTT command payload shape."""

    def test_required_fields(self) -> None:
        payload = build_mqtt_payload(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
        )
        assert payload["correlation_id"] == "test-uuid"
        assert payload["tool_name"] == "get_equipment_status"
        assert isinstance(payload["timestamp"], int)
        assert isinstance(payload["parameters"], dict)

    def test_parameters_default_to_empty_dict(self) -> None:
        payload = build_mqtt_payload(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
        )
        assert payload["parameters"] == {}

    def test_parameters_passed_through(self) -> None:
        params = {"verbose": True}
        payload = build_mqtt_payload(
            correlation_id="test-uuid",
            tool_name="get_equipment_status",
            parameters=params,
        )
        assert payload["parameters"] == params


# -----------------------------------------------------------------------
# Update builders
# -----------------------------------------------------------------------


class TestBuildCompleteUpdate:
    """Verify the ``complete`` update shape (R5.2)."""

    def test_status_is_complete(self) -> None:
        update = build_complete_update(result={"fryer": "350F"})
        assert update["status"] == STATUS_COMPLETE

    def test_result_included(self) -> None:
        result = {"fryer": "350F", "grill": "400F"}
        update = build_complete_update(result=result)
        assert update["result"] == result

    def test_completed_at_is_epoch(self) -> None:
        before = int(time.time())
        update = build_complete_update(result={})
        after = int(time.time())
        assert before <= update["completed_at"] <= after

    def test_duration_ms_optional(self) -> None:
        update = build_complete_update(result={})
        assert "duration_ms" not in update

    def test_duration_ms_included_when_provided(self) -> None:
        update = build_complete_update(result={}, duration_ms=4500)
        assert update["duration_ms"] == 4500


class TestBuildErrorUpdate:
    """Verify the ``error`` update shape (R5.2)."""

    def test_status_is_error(self) -> None:
        update = build_error_update(error_message="timeout")
        assert update["status"] == STATUS_ERROR

    def test_error_message_included(self) -> None:
        update = build_error_update(error_message="store unreachable")
        assert update["error_message"] == "store unreachable"

    def test_completed_at_is_epoch(self) -> None:
        before = int(time.time())
        update = build_error_update(error_message="err")
        after = int(time.time())
        assert before <= update["completed_at"] <= after

    def test_duration_ms_optional(self) -> None:
        update = build_error_update(error_message="err")
        assert "duration_ms" not in update

    def test_duration_ms_included_when_provided(self) -> None:
        update = build_error_update(error_message="err", duration_ms=1200)
        assert update["duration_ms"] == 1200


class TestBuildFailedUpdate:
    """Verify the ``failed`` update shape (R4.9)."""

    def test_status_is_failed(self) -> None:
        update = build_failed_update(error_message="MQTT PUBLISH failed")
        assert update["status"] == STATUS_FAILED

    def test_error_message_included(self) -> None:
        update = build_failed_update(error_message="MQTT PUBLISH failed")
        assert update["error_message"] == "MQTT PUBLISH failed"

    def test_completed_at_is_epoch(self) -> None:
        before = int(time.time())
        update = build_failed_update(error_message="err")
        after = int(time.time())
        assert before <= update["completed_at"] <= after

    def test_no_duration_ms(self) -> None:
        """Failed updates (gateway-side) don't carry duration_ms."""
        update = build_failed_update(error_message="err")
        assert "duration_ms" not in update
