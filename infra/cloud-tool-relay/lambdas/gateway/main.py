# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared MCP server entrypoint for the four gateway Lambdas.

Each gateway Lambda is deployed with the environment variable
``GATEWAY_DOMAIN`` set to one of ``pos``, ``equipment``, ``inventory``,
or ``schedule``.  At cold-start the handler loads only that domain's
tools from ``shared.tools`` and publishes commands to AWS IoT Core via
the connection-less ``iot-data`` data plane (the client is cached for the
lifetime of the execution environment).

The Lambda exposes a custom MCP-compatible handler that responds to two
JSON-RPC-style methods:

* ``tools/list`` — returns the tool definitions for this domain.
* ``tools/call`` — executes the publish-and-return flow:
    1. Generate a UUID v4 ``correlation_id``.
    2. ``PutItem`` a pending record to ``CommandResults``.
    3. ``PUBLISH`` QoS 1 to ``stores/{store_id}/commands/{domain}``.
    4. Return ``{correlation_id}``.

The Lambda does **not** block waiting for an MQTT response (R4.5).

Requirements covered:
    R4.3  — domain-specific tool exposure via ``GATEWAY_DOMAIN``.
    R4.4  — UUID, PutItem pending, PUBLISH QoS 1, return correlation_id.
    R4.5  — non-blocking; returns immediately after publish.
    R4.6  — credentials cached in module-global state, never logged.
    R4.9  — explicit failure handling (Secrets Manager / MQTT errors).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -----------------------------------------------------------------------
# Environment variables
# -----------------------------------------------------------------------

GATEWAY_DOMAIN: str = os.environ.get("GATEWAY_DOMAIN", "")
COMMAND_RESULTS_TABLE_NAME: str = os.environ.get("COMMAND_RESULTS_TABLE_NAME", "")

#: Cached iot-data client. Commands are published to AWS IoT Core via the
#: connection-less data plane. Spec 020 migrated the transport off Amazon MQ;
#: there is no MQTT client or broker secret on this path any more.
_iot_data_client: Any = None


def _get_iot_data_client() -> Any:
    global _iot_data_client
    if _iot_data_client is None:
        import boto3

        _iot_data_client = boto3.client("iot-data")
    return _iot_data_client


def _publish_iot(topic: str, payload: dict[str, Any]) -> None:
    """Publish a command to IoT Core via the data plane (no MQTT client)."""
    _get_iot_data_client().publish(
        topic=topic, qos=1, payload=json.dumps(payload).encode("utf-8")
    )


#: Default store ID when the caller does not provide one (R12.1).
DEFAULT_STORE_ID: str = "sim-store-001"

# -----------------------------------------------------------------------
# Module-global cached state (R4.6)
# -----------------------------------------------------------------------
# These are populated on the first invocation (cold start) and reused
# across warm invocations within the same Lambda execution environment.

_domain_tools: frozenset[str] | None = None


def _get_domain_tools() -> frozenset[str]:
    """Load and cache the tool set for the configured domain."""
    global _domain_tools
    if _domain_tools is None:
        from shared.tools import tools_for_domain

        _domain_tools = tools_for_domain(GATEWAY_DOMAIN)
    return _domain_tools


# -----------------------------------------------------------------------
# Tool definitions for tools/list
# -----------------------------------------------------------------------

#: Human-readable descriptions for each tool, keyed by tool name.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    # pos
    "get_daily_sales": "Retrieve the daily sales summary for the store.",
    "get_active_orders": "List currently active orders.",
    "get_transaction_history": "Retrieve recent transaction history.",
    "void_order": "Void an existing order by order ID.",
    # equipment
    "get_equipment_status": "Get the current status of all equipment.",
    "set_timer": "Set a timer on a piece of equipment.",
    "get_temperature_log": "Retrieve the temperature log for equipment.",
    "acknowledge_alert": "Acknowledge an equipment alert by equipment id.",
    # inventory
    "get_stock_levels": "Get current stock levels for all items.",
    "log_waste": "Log a waste event for an inventory item.",
    "get_usage_report": "Retrieve an inventory usage report.",
    "check_reorder_status": "Check the reorder status for inventory items.",
    # schedule
    "get_current_shifts": "Get the current shift schedule.",
    "clock_in": "Clock in a staff member.",
    "clock_out": "Clock out a staff member.",
    "get_labor_cost": "Retrieve the current labor cost summary.",
}

#: Input schema definitions for each tool.
_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    # pos
    "get_daily_sales": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    "get_active_orders": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    "get_transaction_history": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "status_filter": {"type": "string", "description": "Optional order status to filter by."},
            "limit": {"type": "integer", "description": "Maximum number of transactions to return."},
        },
    },
    "void_order": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "order_id": {"type": "integer", "description": "Numeric id of the order to void."},
            "reason": {"type": "string", "description": "Reason for voiding the order."},
        },
        "required": ["order_id", "reason"],
    },
    # equipment
    "get_equipment_status": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    "set_timer": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "equipment_id": {"type": "string", "description": "Equipment identifier."},
            "minutes": {"type": "number", "description": "Timer duration in minutes (> 0)."},
        },
        "required": ["equipment_id", "minutes"],
    },
    "get_temperature_log": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "equipment_id": {"type": "string", "description": "Equipment identifier."},
            "limit": {"type": "integer", "description": "Max history rows (default 100)."},
        },
        "required": ["equipment_id"],
    },
    "acknowledge_alert": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "equipment_id": {"type": "string", "description": "Equipment id whose active alert to acknowledge."},
        },
        "required": ["equipment_id"],
    },
    # inventory
    "get_stock_levels": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    "log_waste": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "item_id": {"type": "string", "description": "Inventory item id (from get_stock_levels)."},
            "quantity": {"type": "number", "description": "Quantity wasted (> 0)."},
            "reason": {
                "type": "string",
                "enum": ["contamination", "expired", "overproduction", "spillage"],
                "description": "Waste reason.",
            },
        },
        "required": ["item_id", "quantity", "reason"],
    },
    "get_usage_report": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "start_time": {"type": "string", "description": "ISO 8601 start (optional)."},
            "end_time": {"type": "string", "description": "ISO 8601 end (optional)."},
        },
    },
    "check_reorder_status": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    # schedule
    "get_current_shifts": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
    "clock_in": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "employee_id": {"type": "string", "description": "Employee identifier."},
        },
        "required": ["employee_id"],
    },
    "clock_out": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
            "employee_id": {"type": "string", "description": "Employee identifier."},
        },
        "required": ["employee_id"],
    },
    "get_labor_cost": {
        "type": "object",
        "properties": {
            "store_id": {"type": "string", "description": "Store identifier. Defaults to sim-store-001."},
        },
    },
}


def _build_tool_definitions() -> list[dict[str, Any]]:
    """Build MCP tool definitions for the current domain's tools."""
    tools = _get_domain_tools()
    definitions: list[dict[str, Any]] = []
    for tool_name in sorted(tools):
        definition: dict[str, Any] = {
            "name": tool_name,
            "description": _TOOL_DESCRIPTIONS.get(tool_name, ""),
            "inputSchema": _TOOL_INPUT_SCHEMAS.get(
                tool_name,
                {"type": "object", "properties": {}},
            ),
        }
        definitions.append(definition)
    return definitions


# -----------------------------------------------------------------------
# tools/call — the publish-and-return flow (R4.4)
# -----------------------------------------------------------------------


def _handle_tools_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute the publish-and-return flow for a single tool invocation.

    1. Validate the tool belongs to this domain.
    2. Generate a UUID v4 ``correlation_id``.
    3. ``PutItem`` a pending record to ``CommandResults``.
    4. ``PUBLISH`` QoS 1 to ``stores/{store_id}/commands/{domain}``.
    5. Return ``{correlation_id}``.

    On failure after PutItem, update the record to ``status="failed"``
    and raise an error (R4.9).
    """
    from shared.correlation import (
        build_mqtt_payload,
        build_pending_item,
        new_correlation_id,
    )
    from shared.ddb import put_pending_command, update_command_result
    from shared.correlation import build_failed_update

    # Validate tool belongs to this domain.
    domain_tools = _get_domain_tools()
    if tool_name not in domain_tools:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Tool {tool_name!r} is not available on the {GATEWAY_DOMAIN!r} gateway.",
                }
            ],
        }

    # Extract store_id from arguments, defaulting to sim-store-001 (R12.1).
    store_id = arguments.pop("store_id", None) or DEFAULT_STORE_ID

    # Step 1: Generate correlation ID.
    correlation_id = new_correlation_id()

    # Step 2: PutItem pending record to CommandResults (R4.4).
    pending_item = build_pending_item(
        correlation_id=correlation_id,
        tool_name=tool_name,
        store_id=store_id,
        domain=GATEWAY_DOMAIN,
        parameters=arguments,
    )
    put_pending_command(
        table_name=COMMAND_RESULTS_TABLE_NAME,
        item=pending_item,
    )

    # Step 3: PUBLISH QoS 1 to the store's command topic (R4.4).
    # Connection-less IoT Core publish (spec 020) — no MQTT client and no
    # broker secret. The store is subscribed over mTLS on
    # ``stores/{store_id}/commands/{domain}``.
    mqtt_payload = build_mqtt_payload(
        correlation_id=correlation_id,
        tool_name=tool_name,
        parameters=arguments,
    )
    try:
        topic = f"stores/{store_id}/commands/{GATEWAY_DOMAIN}"
        _publish_iot(topic, mqtt_payload)
    except Exception as exc:  # noqa: BLE001 - any publish failure -> failed record (R4.9)
        # Publish failure after PutItem — mark the record as failed (R4.9).
        logger.error(
            "iot publish failed for correlation_id=%s: %s",
            correlation_id,
            type(exc).__name__,
        )
        failed_update = build_failed_update(str(exc))
        update_command_result(
            table_name=COMMAND_RESULTS_TABLE_NAME,
            correlation_id=correlation_id,
            updates=failed_update,
        )
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Failed to publish command: {type(exc).__name__}",
                }
            ],
        }

    # Step 4: Return correlation_id (R4.4, R4.5 — non-blocking).
    logger.info(
        "Published %s for store=%s, correlation_id=%s",
        tool_name,
        store_id,
        correlation_id,
    )
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"correlation_id": correlation_id}),
            }
        ],
    }


# -----------------------------------------------------------------------
# Lambda handler
# -----------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint implementing a custom MCP-compatible handler.

    Supports two JSON-RPC-style methods:

    * ``tools/list`` — returns the tool definitions for this domain.
    * ``tools/call`` — executes the publish-and-return flow.

    Request shape (``tools/list``)::

        {
            "method": "tools/list"
        }

    Request shape (``tools/call``)::

        {
            "method": "tools/call",
            "params": {
                "name": "<tool_name>",
                "arguments": { ... }
            }
        }

    Response shape (``tools/list``)::

        {
            "tools": [ { "name": "...", "description": "...", "inputSchema": {...} }, ... ]
        }

    Response shape (``tools/call``)::

        {
            "content": [ { "type": "text", "text": "{\"correlation_id\": \"...\"}" } ]
        }
    """
    method = event.get("method", "")

    if method == "tools/list":
        return {"tools": _build_tool_definitions()}

    if method == "tools/call":
        params = event.get("params", {})
        tool_name = params.get("name", "")
        arguments = dict(params.get("arguments", {}))

        if not tool_name:
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "Missing 'name' in tools/call params.",
                    }
                ],
            }

        # Secrets Manager failure at cold start surfaces here (R4.9).
        # The DynamoDB PutItem now happens before the MQTT publish
        # inside _handle_tools_call, so a failure here is from an
        # unexpected error outside the normal publish flow.
        try:
            return _handle_tools_call(tool_name, arguments)
        except (RuntimeError, ImportError, ModuleNotFoundError) as exc:
            logger.error(
                "Gateway error for tool %s: %s",
                tool_name,
                type(exc).__name__,
            )
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": f"Gateway error: {type(exc).__name__}",
                    }
                ],
            }

    # Unknown method.
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": f"Unknown method: {method!r}. Supported: 'tools/list', 'tools/call'.",
            }
        ],
    }
