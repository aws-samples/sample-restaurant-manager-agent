# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inventory MCP server — stock level and waste management tools."""

from __future__ import annotations

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from simulator.mcp._errors import internal_error, invalid_input, not_found
from simulator.models.inventory import WasteEvent, WasteReason
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)

_VALID_WASTE_REASONS = {r.value for r in WasteReason}


async def tool_get_stock_levels(store: StateStore) -> dict:
    """Return all inventory items with current quantity, threshold, and reorder flag."""
    try:
        return {
            "items": [i.model_dump(mode="json") for i in store.inventory.items]
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_stock_levels failed")
        return internal_error(str(e))


async def tool_log_waste(
    store: StateStore, item_id: str, quantity: float, reason: str
) -> dict:
    """Record a waste event and deduct the quantity from stock."""
    try:
        if quantity <= 0:
            return invalid_input("quantity must be greater than 0")
        if reason not in _VALID_WASTE_REASONS:
            return invalid_input(
                f"reason must be one of {sorted(_VALID_WASTE_REASONS)}"
            )
        target = None
        for item in store.inventory.items:
            if item.id == item_id:
                target = item
                break
        if target is None:
            return not_found(f"Inventory item '{item_id}' not found")

        # Deduct stock (clamp to 0 per spec 000).
        deducted = min(quantity, target.current_quantity)
        target.current_quantity = round(target.current_quantity - deducted, 4)
        target.needs_reorder = target.current_quantity < target.reorder_threshold

        sim_time = store.simulation.clock.current_time
        event = WasteEvent(
            item_id=target.id,
            item_name=target.name,
            quantity=quantity,
            reason=WasteReason(reason),
            timestamp=sim_time,
        )
        store.inventory.waste_history.append(event)

        await store.update(
            "inventory",
            "waste_logged",
            {
                "items": store.inventory.items,
                "waste_history": store.inventory.waste_history,
            },
            sim_time=sim_time,
        )
        return {
            "item": target.model_dump(mode="json"),
            "waste_event": event.model_dump(mode="json"),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_log_waste failed")
        return internal_error(str(e))


async def tool_get_usage_report(store: StateStore, start_time: str = "", end_time: str = "") -> dict:
    """Return consumption log and usage summary, optionally filtered by time range."""
    try:
        from datetime import datetime as dt

        log = store.inventory.consumption_log

        # Time-range filtering
        if start_time or end_time:
            start = dt.fromisoformat(start_time) if start_time else dt.min
            end = dt.fromisoformat(end_time) if end_time else dt.max
            filtered = [e for e in log if start <= e.timestamp <= end]

            # Build summary per item
            summary: dict[str, dict[str, float]] = {}
            for e in filtered:
                if e.item_id not in summary:
                    summary[e.item_id] = {"item_name": e.item_name, "total": 0.0, "order": 0.0, "waste": 0.0}
                summary[e.item_id]["total"] = round(summary[e.item_id]["total"] + e.quantity, 4)
                summary[e.item_id][e.reason] = round(summary[e.item_id].get(e.reason, 0.0) + e.quantity, 4)

            return {
                "events": [e.model_dump(mode="json") for e in filtered[-100:]],  # cap at 100 events in response
                "summary": summary,
                "period": {
                    "start": start_time or "beginning",
                    "end": end_time or "now",
                    "event_count": len(filtered),
                },
            }

        # Legacy format (no time range) — backward compatible
        waste_events = [w.model_dump(mode="json") for w in store.inventory.waste_history]
        consumption: dict[str, float] = {}
        for w in store.inventory.waste_history:
            consumption[w.item_id] = consumption.get(w.item_id, 0.0) + w.quantity
        return {
            "waste_events": waste_events,
            "consumption": {k: round(v, 4) for k, v in consumption.items()},
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_usage_report failed")
        return internal_error(str(e))


async def tool_check_reorder_status(store: StateStore) -> dict:
    """Return items flagged for reorder."""
    try:
        items = [
            i.model_dump(mode="json")
            for i in store.inventory.items
            if i.needs_reorder
        ]
        return {"items": items}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_check_reorder_status failed")
        return internal_error(str(e))


def build_inventory_server(store: StateStore) -> FastMCP:
    """Create a FastMCP server that registers the four inventory tools."""
    mcp = FastMCP("inventory-mcp")

    @mcp.tool()
    async def get_stock_levels() -> dict:
        """Get all inventory items with current quantity, unit, reorder threshold, max quantity, and reorder flag."""
        return await tool_get_stock_levels(store)

    @mcp.tool()
    async def log_waste(
        item_id: str,
        quantity: float,
        reason: Literal["expired", "spillage", "contamination", "overproduction"],
    ) -> dict:
        """Record a waste event and deduct the quantity from stock. Reason must be one of expired, spillage, contamination, overproduction."""
        return await tool_log_waste(store, item_id=item_id, quantity=quantity, reason=reason)

    @mcp.tool()
    async def get_usage_report() -> dict:
        """Get the waste event history and a per-item consumption summary since the current day started."""
        return await tool_get_usage_report(store)

    @mcp.tool()
    async def check_reorder_status() -> dict:
        """Return only inventory items whose stock has fallen below the reorder threshold."""
        return await tool_check_reorder_status(store)

    return mcp
