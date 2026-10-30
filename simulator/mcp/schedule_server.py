# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule MCP server — staffing and labor cost tools."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from simulator.mcp._errors import internal_error, invalid_state, not_found
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)


async def tool_get_current_shifts(store: StateStore) -> dict:
    """Return all employees with clock-in status and today's shift info."""
    try:
        return {
            "employees": [e.model_dump(mode="json") for e in store.schedule.employees]
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_current_shifts failed")
        return internal_error(str(e))


async def tool_clock_in(store: StateStore, employee_id: str) -> dict:
    """Clock an employee in."""
    try:
        target = None
        for e in store.schedule.employees:
            if e.id == employee_id:
                target = e
                break
        if target is None:
            return not_found(f"Employee '{employee_id}' not found")
        # semgrep false positive: `is_clocked_in` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        if target.is_clocked_in:
            return invalid_state(f"Employee '{employee_id}' is already clocked in")
        sim_time = store.simulation.clock.current_time
        # nosemgrep: is-function-without-parentheses
        target.is_clocked_in = True
        target.clock_in_time = sim_time
        await store.update(
            "schedule",
            "clock_in",
            {"employees": store.schedule.employees},
            sim_time=sim_time,
        )
        return {"employee": target.model_dump(mode="json")}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_clock_in failed")
        return internal_error(str(e))


async def tool_clock_out(store: StateStore, employee_id: str) -> dict:
    """Clock an employee out."""
    try:
        target = None
        for e in store.schedule.employees:
            if e.id == employee_id:
                target = e
                break
        if target is None:
            return not_found(f"Employee '{employee_id}' not found")
        # nosemgrep: is-function-without-parentheses
        if not target.is_clocked_in:
            return invalid_state(f"Employee '{employee_id}' is not currently clocked in")
        sim_time = store.simulation.clock.current_time
        # Accumulate hours worked since clock-in.
        if target.clock_in_time is not None:
            delta_hours = (sim_time - target.clock_in_time).total_seconds() / 3600.0
            target.hours_today = round(target.hours_today + delta_hours, 4)
            target.labor_cost_today = round(
                target.hours_today * target.hourly_rate, 2
            )
        # nosemgrep: is-function-without-parentheses
        target.is_clocked_in = False
        target.clock_in_time = None
        await store.update(
            "schedule",
            "clock_out",
            {"employees": store.schedule.employees},
            sim_time=sim_time,
        )
        return {"employee": target.model_dump(mode="json")}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_clock_out failed")
        return internal_error(str(e))


async def tool_get_labor_cost(store: StateStore) -> dict:
    """Return total labor cost, percentage of daily revenue, and employees on clock."""
    try:
        return store.schedule.labor_cost.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        logger.exception("tool_get_labor_cost failed")
        return internal_error(str(e))


def build_schedule_server(store: StateStore) -> FastMCP:
    """Create a FastMCP server that registers the four schedule tools."""
    mcp = FastMCP("schedule-mcp")

    @mcp.tool()
    async def get_current_shifts() -> dict:
        """Get all employees with clock-in status, clock-in time, hours today, and scheduled shifts."""
        return await tool_get_current_shifts(store)

    @mcp.tool()
    async def clock_in(employee_id: str) -> dict:
        """Clock an employee in. Errors if the employee is not found or already clocked in."""
        return await tool_clock_in(store, employee_id=employee_id)

    @mcp.tool()
    async def clock_out(employee_id: str) -> dict:
        """Clock an employee out. Errors if the employee is not found or not currently clocked in."""
        return await tool_clock_out(store, employee_id=employee_id)

    @mcp.tool()
    async def get_labor_cost() -> dict:
        """Get total labor cost for the day, labor cost as percentage of daily revenue, and count of employees on clock."""
        return await tool_get_labor_cost(store)

    return mcp
