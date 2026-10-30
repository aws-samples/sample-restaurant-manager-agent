# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POS MCP server — sales and order tools.

This module exposes four tool functions (`tool_get_daily_sales`,
`tool_get_active_orders`, `tool_get_transaction_history`, `tool_void_order`)
as plain async callables and a `build_pos_server(store)` function that wraps
them in a :class:`FastMCP` instance for stdio transport.

The plain callables are the canonical implementation; FastMCP adds schema
generation and JSON-RPC plumbing on top. The MQTT bridge (spec 002) invokes
the plain callables directly via the tool registry.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

from simulator.mcp._errors import internal_error, invalid_state, not_found
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"placed", "preparing", "ready", "served"})
_TERMINAL_STATUSES = frozenset({"completed", "voided"})


# ----- Plain async callables (the canonical tool implementations) -----

async def tool_get_daily_sales(store: StateStore) -> dict:
    """Return the current day's sales summary."""
    try:
        ds = store.pos.daily_sales
        if ds is None:
            return {
                "date": None,
                "total_revenue": 0.0,
                "order_count": 0,
                "avg_order_value": 0.0,
                "void_count": 0,
                "hourly_breakdown": [],
            }
        return ds.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_daily_sales failed")
        return internal_error(str(e))


async def tool_get_active_orders(store: StateStore) -> dict:
    """Return orders that are not completed or voided."""
    try:
        orders = [o for o in store.pos.orders if o.status in _ACTIVE_STATUSES]
        return {"orders": [o.model_dump(mode="json") for o in orders]}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_active_orders failed")
        return internal_error(str(e))


async def tool_get_transaction_history(
    store: StateStore,
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Return recent orders with an optional status filter."""
    try:
        limit = max(1, min(int(limit), 500))
        orders = store.pos.orders
        if status_filter:
            orders = [o for o in orders if o.status == status_filter]
        # Most recent first (orders are appended in order; reverse for recency).
        selected = list(reversed(orders))[:limit]
        return {"orders": [o.model_dump(mode="json") for o in selected]}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_transaction_history failed")
        return internal_error(str(e))


async def tool_void_order(store: StateStore, order_id: int, reason: str) -> dict:
    """Void an active order by id."""
    try:
        target = None
        for o in store.pos.orders:
            if o.id == order_id:
                target = o
                break
        if target is None:
            return not_found(f"Order {order_id} not found")
        if target.status in _TERMINAL_STATUSES:
            return invalid_state(
                f"Order {order_id} is already {target.status} and cannot be voided"
            )
        sim_time = store.simulation.clock.current_time
        target.status = "voided"
        target.void_reason = reason
        target.updated_at = sim_time
        await store.update(
            "pos",
            "order_voided",
            {"orders": store.pos.orders},
            sim_time=sim_time,
        )
        return {"order": target.model_dump(mode="json")}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_void_order failed")
        return internal_error(str(e))


# ----- FastMCP wrapper -----

def build_pos_server(store: StateStore) -> FastMCP:
    """Create a FastMCP server that registers the four POS tools over stdio."""
    mcp = FastMCP("pos-mcp")

    @mcp.tool()
    async def get_daily_sales() -> dict:
        """Get today's sales summary: total revenue, order count, average order value, void count, hourly breakdown."""
        return await tool_get_daily_sales(store)

    @mcp.tool()
    async def get_active_orders() -> dict:
        """Get all orders currently in progress (not completed or voided)."""
        return await tool_get_active_orders(store)

    @mcp.tool()
    async def get_transaction_history(
        status_filter: Optional[Literal["placed", "preparing", "ready", "served", "completed", "voided"]] = None,
        limit: int = 50,
    ) -> dict:
        """Get recent orders with optional status filter. `limit` defaults to 50 and is capped at 500."""
        return await tool_get_transaction_history(store, status_filter=status_filter, limit=limit)

    @mcp.tool()
    async def void_order(order_id: int, reason: str) -> dict:
        """Void an active order by id. Returns an error if the order is not found, already completed, or already voided."""
        return await tool_void_order(store, order_id=order_id, reason=reason)

    return mcp
