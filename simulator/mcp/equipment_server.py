# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equipment MCP server — kitchen equipment monitoring tools."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from simulator.mcp._errors import internal_error, invalid_input, invalid_state, not_found
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)


async def tool_get_equipment_status(store: StateStore) -> dict:
    """Return all equipment with current temp, target, status, and active alert."""
    try:
        return {
            "equipment": [e.model_dump(mode="json") for e in store.equipment.equipment]
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_equipment_status failed")
        return internal_error(str(e))


async def tool_set_timer(store: StateStore, equipment_id: str, minutes: float) -> dict:
    """Set a countdown timer on a piece of equipment."""
    try:
        if minutes <= 0:
            return invalid_input("minutes must be greater than 0")
        target = None
        for e in store.equipment.equipment:
            if e.id == equipment_id:
                target = e
                break
        if target is None:
            return not_found(f"Equipment '{equipment_id}' not found")
        target.timer_remaining_seconds = float(minutes) * 60.0
        sim_time = store.simulation.clock.current_time
        await store.update(
            "equipment",
            "timer_set",
            {"equipment": store.equipment.equipment},
            sim_time=sim_time,
        )
        return {"equipment": target.model_dump(mode="json")}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_set_timer failed")
        return internal_error(str(e))


async def tool_get_temperature_log(
    store: StateStore, equipment_id: str, limit: int = 100
) -> dict:
    """Return temperature history for a piece of equipment."""
    try:
        history = store.equipment.temp_history.get(equipment_id)
        if history is None:
            return not_found(f"Equipment '{equipment_id}' not found")
        limit = max(1, min(int(limit), 10000))
        readings = history[-limit:]
        return {
            "equipment_id": equipment_id,
            "readings": [r.model_dump(mode="json") for r in readings],
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_temperature_log failed")
        return internal_error(str(e))


async def tool_acknowledge_alert(store: StateStore, equipment_id: str) -> dict:
    """Mark an active alert on a piece of equipment as acknowledged."""
    try:
        target = None
        for e in store.equipment.equipment:
            if e.id == equipment_id:
                target = e
                break
        if target is None:
            return not_found(f"Equipment '{equipment_id}' not found")
        if not target.active_alert:
            return invalid_state(f"Equipment '{equipment_id}' has no active alert")
        target.active_alert.acknowledged = True
        sim_time = store.simulation.clock.current_time
        await store.update(
            "equipment",
            "alert_acknowledged",
            {"equipment": store.equipment.equipment},
            sim_time=sim_time,
        )
        return {"equipment": target.model_dump(mode="json")}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_acknowledge_alert failed")
        return internal_error(str(e))


async def tool_acknowledge_proactive_alert(store: StateStore, alert_id: str) -> dict:
    """Acknowledge a proactive (cloud-raised) alert by its ``alert_id``, resuming
    the cloud Step Functions workflow (spec 023 Task 11).

    Distinct from ``acknowledge_alert`` (which clears an equipment's local
    ``active_alert`` by ``equipment_id`` and does not touch the cloud workflow).
    """
    try:
        sim_time = store.simulation.clock.current_time
        return await store.acknowledge_proactive_alert(alert_id, sim_time=sim_time)
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_acknowledge_proactive_alert failed")
        return internal_error(str(e))


async def tool_get_active_alerts(store: StateStore) -> dict:
    """List unacknowledged PROACTIVE alerts from the inbox (cloud-raised alerts the
    manager was notified about, spec 023). Distinct from ``get_equipment_status``,
    which reports each equipment's live status/active_alert; this reads the alert
    inbox (``alerts.entries``). Use it to answer 'are there any alerts?'."""
    try:
        entries = await store.list_active_alerts()
        alerts = [
            {
                "alert_id": e.get("alert_id"),
                "severity": e.get("severity"),
                "source_id": e.get("source_id"),
                "condition": e.get("condition"),
                "message": e.get("message"),
                "timestamp": e.get("timestamp"),
            }
            for e in entries
        ]
        return {"active_alerts": alerts, "count": len(alerts)}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_active_alerts failed")
        return internal_error(str(e))


def build_equipment_server(store: StateStore) -> FastMCP:
    """Create a FastMCP server that registers the four equipment tools."""
    mcp = FastMCP("equipment-mcp")

    @mcp.tool()
    async def get_equipment_status() -> dict:
        """Get all kitchen equipment with current temperature, target, status (normal/warning/alert), and active alert."""
        return await tool_get_equipment_status(store)

    @mcp.tool()
    async def set_timer(equipment_id: str, minutes: float) -> dict:
        """Set a countdown timer on a piece of equipment. Minutes must be greater than 0."""
        return await tool_set_timer(store, equipment_id=equipment_id, minutes=minutes)

    @mcp.tool()
    async def get_temperature_log(equipment_id: str, limit: int = 100) -> dict:
        """Get temperature history for a piece of equipment. Defaults to the last 100 readings."""
        return await tool_get_temperature_log(store, equipment_id=equipment_id, limit=limit)

    @mcp.tool()
    async def acknowledge_alert(equipment_id: str) -> dict:
        """Mark any active alert on this equipment as acknowledged."""
        return await tool_acknowledge_alert(store, equipment_id=equipment_id)

    @mcp.tool()
    async def acknowledge_proactive_alert(alert_id: str) -> dict:
        """Acknowledge a proactive alert (one pushed to the alert inbox) by its alert_id, resuming the cloud workflow."""
        return await tool_acknowledge_proactive_alert(store, alert_id=alert_id)

    @mcp.tool()
    async def get_active_alerts() -> dict:
        """List unacknowledged proactive alerts in the alert inbox (cloud-raised alerts the manager was notified about)."""
        return await tool_get_active_alerts(store)

    return mcp
