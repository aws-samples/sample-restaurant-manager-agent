# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``lambdas.gateway.main`` — the shared MCP server entrypoint.

The gateway publishes commands to AWS IoT Core via the connection-less
``iot-data:Publish`` data plane (spec 020 migrated off Amazon MQ; Task 8
removed the broker path entirely).

Requirements covered:
    R4.3  — domain-specific tool exposure via ``GATEWAY_DOMAIN``.
    R4.4  — UUID, PutItem pending, IoT publish, return correlation_id.
    R4.5  — non-blocking; returns immediately after publish.
    R4.6  — clients cached in module-global state.
    R4.9  — explicit failure handling (publish errors).
"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# conftest.py adds both the project root and lambdas/ to sys.path,
# so ``shared`` and ``lambdas.gateway.main`` are both importable.


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _fresh_import():
    """Force a fresh import of the gateway main module, resetting globals."""
    for mod_name in list(sys.modules):
        if "gateway.main" in mod_name:
            del sys.modules[mod_name]
    import gateway.main as mod
    return mod


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch: pytest.MonkeyPatch):
    """Set env vars and reset module-global cached state before each test."""
    monkeypatch.setenv("GATEWAY_DOMAIN", "equipment")
    monkeypatch.setenv("COMMAND_RESULTS_TABLE_NAME", "TestCommandResults")

    # Force fresh import so env vars and globals are reset.
    mod = _fresh_import()
    yield mod

    # Cleanup: remove cached module so next test gets a fresh one.
    for mod_name in list(sys.modules):
        if "gateway.main" in mod_name:
            del sys.modules[mod_name]


# -----------------------------------------------------------------------
# tools/list tests (R4.3)
# -----------------------------------------------------------------------


class TestToolsList:
    """Verify tools/list returns the correct tool definitions."""

    def test_returns_equipment_tools(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({"method": "tools/list"}, None)

        assert "tools" in result
        tool_names = {t["name"] for t in result["tools"]}
        assert tool_names == {
            "get_equipment_status",
            "set_timer",
            "get_temperature_log",
            "acknowledge_alert",
        }

    def test_each_tool_has_required_fields(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({"method": "tools/list"}, None)

        for tool in result["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["description"], str)
            assert len(tool["description"]) > 0

    def test_tools_are_sorted_by_name(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({"method": "tools/list"}, None)

        names = [t["name"] for t in result["tools"]]
        assert names == sorted(names)

    def test_pos_domain_returns_pos_tools(self, _reset_module, monkeypatch) -> None:
        monkeypatch.setenv("GATEWAY_DOMAIN", "pos")
        mod = _fresh_import()

        result = mod.handler({"method": "tools/list"}, None)

        tool_names = {t["name"] for t in result["tools"]}
        assert tool_names == {
            "get_daily_sales",
            "get_active_orders",
            "get_transaction_history",
            "void_order",
        }

    def test_exactly_four_tools_per_domain(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({"method": "tools/list"}, None)
        assert len(result["tools"]) == 4

    def test_acknowledge_alert_schema_requires_equipment_id(self, _reset_module) -> None:
        """Regression (2026-06-28): the ack schema must use equipment_id, not alert_id."""
        mod = _reset_module
        result = mod.handler({"method": "tools/list"}, None)
        ack = next(t for t in result["tools"] if t["name"] == "acknowledge_alert")
        props = ack["inputSchema"]["properties"]
        assert "equipment_id" in props and "alert_id" not in props
        assert ack["inputSchema"]["required"] == ["equipment_id"]


# -----------------------------------------------------------------------
# tools/call tests — happy path (R4.4, R4.5)
# -----------------------------------------------------------------------


class TestToolsCallHappyPath:
    """Verify the publish-and-return flow."""

    def test_returns_correlation_id(self, _reset_module) -> None:
        """tools/call returns a correlation_id in the response."""
        mod = _reset_module

        with patch.object(mod, "_publish_iot"), \
             patch("shared.ddb.put_pending_command"), \
             patch("shared.correlation.new_correlation_id", return_value="test-uuid-1234"):

            result = mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {},
                    },
                },
                None,
            )

        assert "content" in result
        assert "isError" not in result
        text = result["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["correlation_id"] == "test-uuid-1234"

    def test_puts_pending_item_to_dynamodb(self, _reset_module) -> None:
        """tools/call writes a pending record to CommandResults."""
        mod = _reset_module

        with patch.object(mod, "_publish_iot"), \
             patch("shared.ddb.put_pending_command") as mock_put, \
             patch("shared.correlation.new_correlation_id", return_value="uuid-abc"):

            mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {},
                    },
                },
                None,
            )

        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        assert call_kwargs[1]["table_name"] == "TestCommandResults"
        item = call_kwargs[1]["item"]
        assert item["correlation_id"] == "uuid-abc"
        assert item["status"] == "pending"
        assert item["tool_name"] == "get_equipment_status"
        assert item["domain"] == "equipment"
        assert item["store_id"] == "sim-store-001"

    def test_publishes_to_iot_topic(self, _reset_module) -> None:
        """tools/call publishes to the correct IoT command topic."""
        mod = _reset_module

        with patch.object(mod, "_publish_iot") as mock_publish, \
             patch("shared.ddb.put_pending_command"), \
             patch("shared.correlation.new_correlation_id", return_value="uuid-pub"):

            mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {},
                    },
                },
                None,
            )

        mock_publish.assert_called_once()
        topic, payload = mock_publish.call_args[0]
        assert topic == "stores/sim-store-001/commands/equipment"
        assert payload["correlation_id"] == "uuid-pub"
        assert payload["tool_name"] == "get_equipment_status"

    def test_uses_custom_store_id(self, _reset_module) -> None:
        """tools/call uses the store_id from arguments when provided."""
        mod = _reset_module

        with patch.object(mod, "_publish_iot") as mock_publish, \
             patch("shared.ddb.put_pending_command") as mock_put, \
             patch("shared.correlation.new_correlation_id", return_value="uuid-store"):

            mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {"store_id": "my-store-42"},
                    },
                },
                None,
            )

        # Check DynamoDB item uses custom store_id.
        put_item = mock_put.call_args[1]["item"]
        assert put_item["store_id"] == "my-store-42"

        # Check the IoT topic uses the custom store_id.
        topic, _payload = mock_publish.call_args[0]
        assert topic == "stores/my-store-42/commands/equipment"

    def test_defaults_to_sim_store_001(self, _reset_module) -> None:
        """tools/call defaults to sim-store-001 when no store_id provided."""
        mod = _reset_module

        with patch.object(mod, "_publish_iot"), \
             patch("shared.ddb.put_pending_command") as mock_put, \
             patch("shared.correlation.new_correlation_id", return_value="uuid-default"):

            mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {},
                    },
                },
                None,
            )

        put_item = mock_put.call_args[1]["item"]
        assert put_item["store_id"] == "sim-store-001"


# -----------------------------------------------------------------------
# tools/call tests — error handling (R4.9)
# -----------------------------------------------------------------------


class TestToolsCallErrors:
    """Verify failure handling in the publish-and-return flow."""

    def test_unknown_tool_returns_error(self, _reset_module) -> None:
        """Calling a tool not in this domain returns an MCP error."""
        mod = _reset_module

        # get_daily_sales belongs to pos, not equipment.
        result = mod.handler(
            {
                "method": "tools/call",
                "params": {
                    "name": "get_daily_sales",
                    "arguments": {},
                },
            },
            None,
        )

        assert result["isError"] is True
        assert "not available" in result["content"][0]["text"]

    def test_missing_tool_name_returns_error(self, _reset_module) -> None:
        """tools/call with no tool name returns an MCP error."""
        mod = _reset_module
        result = mod.handler(
            {
                "method": "tools/call",
                "params": {"arguments": {}},
            },
            None,
        )

        assert result["isError"] is True
        assert "Missing" in result["content"][0]["text"]

    def test_iot_publish_failure_marks_record_failed(self, _reset_module) -> None:
        """IoT publish failure after PutItem updates record to failed (R4.9)."""
        mod = _reset_module

        with patch.object(
            mod, "_publish_iot", side_effect=RuntimeError("IoT publish failed")
        ), \
             patch("shared.ddb.put_pending_command"), \
             patch("shared.ddb.update_command_result") as mock_update, \
             patch("shared.correlation.new_correlation_id", return_value="uuid-fail"):

            result = mod.handler(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "get_equipment_status",
                        "arguments": {},
                    },
                },
                None,
            )

        assert result["isError"] is True
        assert "Failed to publish" in result["content"][0]["text"]

        # Verify the record was updated to failed.
        mock_update.assert_called_once()
        update_kwargs = mock_update.call_args[1]
        assert update_kwargs["correlation_id"] == "uuid-fail"
        assert update_kwargs["updates"]["status"] == "failed"


# -----------------------------------------------------------------------
# Unknown method
# -----------------------------------------------------------------------


class TestUnknownMethod:
    """Verify unknown methods return an error."""

    def test_unknown_method_returns_error(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({"method": "resources/list"}, None)

        assert result["isError"] is True
        assert "Unknown method" in result["content"][0]["text"]

    def test_empty_method_returns_error(self, _reset_module) -> None:
        mod = _reset_module
        result = mod.handler({}, None)

        assert result["isError"] is True


# -----------------------------------------------------------------------
# Module-global caching (R4.6)
# -----------------------------------------------------------------------


class TestModuleGlobalCaching:
    """Verify that the iot-data client and domain tools are cached."""

    def test_domain_tools_cached_across_calls(self, _reset_module) -> None:
        """_get_domain_tools returns the same object on repeated calls."""
        mod = _reset_module
        tools1 = mod._get_domain_tools()
        tools2 = mod._get_domain_tools()
        assert tools1 is tools2

    def test_iot_client_cached_across_calls(self, _reset_module) -> None:
        """_get_iot_data_client returns the same object on repeated calls."""
        mod = _reset_module
        mod._iot_data_client = None  # Force cold start

        with patch("boto3.client", return_value=MagicMock()) as mock_factory:
            client1 = mod._get_iot_data_client()
            client2 = mod._get_iot_data_client()

        assert client1 is client2
        # Factory should only be called once (cold start).
        mock_factory.assert_called_once_with("iot-data")


# -----------------------------------------------------------------------
# Input-schema drift regression (2026-06-28) — schemas must match the store
# -----------------------------------------------------------------------


class TestToolSchemas:
    """The gateway input schemas must match the real store tool signatures."""

    def _schema(self, domain: str, tool: str, monkeypatch) -> dict:
        monkeypatch.setenv("GATEWAY_DOMAIN", domain)
        mod = _fresh_import()
        result = mod.handler({"method": "tools/list"}, None)
        spec = next(t for t in result["tools"] if t["name"] == tool)
        return spec["inputSchema"]

    def test_set_timer_uses_minutes(self, _reset_module, monkeypatch) -> None:
        s = self._schema("equipment", "set_timer", monkeypatch)
        assert "minutes" in s["properties"] and "duration_seconds" not in s["properties"]
        assert set(s["required"]) == {"equipment_id", "minutes"}

    def test_get_temperature_log_requires_equipment_id(self, _reset_module, monkeypatch) -> None:
        s = self._schema("equipment", "get_temperature_log", monkeypatch)
        assert "equipment_id" in s["properties"]
        assert s["required"] == ["equipment_id"]

    def test_void_order_int_id_and_required_reason(self, _reset_module, monkeypatch) -> None:
        s = self._schema("pos", "void_order", monkeypatch)
        assert s["properties"]["order_id"]["type"] == "integer"
        assert set(s["required"]) == {"order_id", "reason"}

    def test_log_waste_uses_item_id_and_required_reason(self, _reset_module, monkeypatch) -> None:
        s = self._schema("inventory", "log_waste", monkeypatch)
        assert "item_id" in s["properties"] and "item_name" not in s["properties"]
        assert set(s["required"]) == {"item_id", "quantity", "reason"}
