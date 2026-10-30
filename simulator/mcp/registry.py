# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Central tool registry for the store-side MCP layer.

The registry is the single source of truth for the ``tool_name → callable``
mapping. The MQTT bridge (spec 002) consumes it directly to dispatch inbound
commands without going through stdio/JSON-RPC. Tests and any future
in-process dispatchers use it the same way.

Adding a new MCP tool means: (a) implementing the plain async callable in the
appropriate domain server, (b) registering it on the FastMCP wrapper, and
(c) adding an entry to :func:`build_tool_registry` so the MQTT bridge can
route commands to it.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from simulator.mcp import (
    equipment_server,
    inventory_server,
    pos_server,
    schedule_server,
)
from simulator.state.store import StateStore

ToolFunc = Callable[..., Awaitable[dict]]

# Tool → expected command-topic domain segment. The MQTT dispatcher validates
# inbound commands against this map so a tool sent to the wrong domain topic is
# rejected with code ``topic_domain_mismatch``.
DOMAIN_OF_TOOL: dict[str, str] = {
    # POS
    "get_daily_sales": "pos",
    "get_active_orders": "pos",
    "get_transaction_history": "pos",
    "void_order": "pos",
    # Equipment
    "get_equipment_status": "equipment",
    "set_timer": "equipment",
    "get_temperature_log": "equipment",
    "acknowledge_alert": "equipment",
    "acknowledge_proactive_alert": "equipment",
    "get_active_alerts": "equipment",
    # Inventory
    "get_stock_levels": "inventory",
    "log_waste": "inventory",
    "get_usage_report": "inventory",
    "check_reorder_status": "inventory",
    # Schedule
    "get_current_shifts": "schedule",
    "clock_in": "schedule",
    "clock_out": "schedule",
    "get_labor_cost": "schedule",
}


def build_tool_registry(store: StateStore) -> dict[str, ToolFunc]:
    """Return a dict mapping tool name to bound async callable.

    Each value takes ``**kwargs`` and returns an awaitable dict. The MQTT
    bridge invokes registry entries with the ``parameters`` from inbound
    commands; the stdio runner exposes the same callables through FastMCP.
    """
    return {
        # POS
        "get_daily_sales": lambda **kw: pos_server.tool_get_daily_sales(store, **kw),
        "get_active_orders": lambda **kw: pos_server.tool_get_active_orders(store, **kw),
        "get_transaction_history": lambda **kw: pos_server.tool_get_transaction_history(store, **kw),
        "void_order": lambda **kw: pos_server.tool_void_order(store, **kw),
        # Equipment
        "get_equipment_status": lambda **kw: equipment_server.tool_get_equipment_status(store, **kw),
        "set_timer": lambda **kw: equipment_server.tool_set_timer(store, **kw),
        "get_temperature_log": lambda **kw: equipment_server.tool_get_temperature_log(store, **kw),
        "acknowledge_alert": lambda **kw: equipment_server.tool_acknowledge_alert(store, **kw),
        "acknowledge_proactive_alert": lambda **kw: equipment_server.tool_acknowledge_proactive_alert(store, **kw),
        "get_active_alerts": lambda **kw: equipment_server.tool_get_active_alerts(store, **kw),
        # Inventory
        "get_stock_levels": lambda **kw: inventory_server.tool_get_stock_levels(store, **kw),
        "log_waste": lambda **kw: inventory_server.tool_log_waste(store, **kw),
        "get_usage_report": lambda **kw: inventory_server.tool_get_usage_report(store, **kw),
        "check_reorder_status": lambda **kw: inventory_server.tool_check_reorder_status(store, **kw),
        # Schedule
        "get_current_shifts": lambda **kw: schedule_server.tool_get_current_shifts(store, **kw),
        "clock_in": lambda **kw: schedule_server.tool_clock_in(store, **kw),
        "clock_out": lambda **kw: schedule_server.tool_clock_out(store, **kw),
        "get_labor_cost": lambda **kw: schedule_server.tool_get_labor_cost(store, **kw),
    }
