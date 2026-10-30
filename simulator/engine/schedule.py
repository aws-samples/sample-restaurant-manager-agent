# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule engine simulation module."""

import random
from datetime import datetime

from simulator.models.schedule import Employee, LaborCost, ScheduleState, ShiftTemplate
from simulator.state.store import StateStore

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class ScheduleModule:
    """Manages employee shifts, clock in/out, and labor cost tracking."""

    def __init__(self, state_store: StateStore, staff_config: list[dict], sim_config, pos_module=None, random_seed: int | None = None) -> None:
        self.store = state_store
        self.config = sim_config
        self.pos = pos_module
        # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
        self._rng = random.Random(random_seed)  # nosec B311
        self._employees_map: dict[str, Employee] = {}
        employees = []
        for emp in staff_config:
            e = Employee(
                id=emp["id"], name=emp["name"], role=emp["role"],
                hourly_rate=emp["hourly_rate"],
                shifts=[ShiftTemplate(**s) for s in emp["shifts"]],
            )
            employees.append(e)
            self._employees_map[e.id] = e
        self.store.schedule = ScheduleState(employees=employees)

    def _today_shift(self, emp: Employee, sim_time: datetime) -> ShiftTemplate | None:
        day_name = _DAYS[sim_time.weekday()]
        for s in emp.shifts:
            if s.day == day_name:
                return s
        return None

    def _parse_time(self, hhmm: str) -> tuple[int, int]:
        h, m = hhmm.split(":")
        return int(h), int(m)

    def clock_in(self, employee_id: str, sim_time: datetime | None = None) -> None:
        emp = self._employees_map.get(employee_id)
        if not emp:
            raise ValueError(f"Employee not found: {employee_id}")
        # semgrep false positive: `is_clocked_in` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        if emp.is_clocked_in:
            raise ValueError(f"Employee {employee_id} already clocked in")
        # nosemgrep: is-function-without-parentheses
        emp.is_clocked_in = True
        emp.clock_in_time = sim_time or datetime.now()

    def clock_out(self, employee_id: str, sim_time: datetime | None = None) -> None:
        emp = self._employees_map.get(employee_id)
        if not emp:
            raise ValueError(f"Employee not found: {employee_id}")
        # nosemgrep: is-function-without-parentheses
        if not emp.is_clocked_in:
            raise ValueError(f"Employee {employee_id} not clocked in")
        now = sim_time or datetime.now()
        if emp.clock_in_time:
            hours = (now - emp.clock_in_time).total_seconds() / 3600.0
            emp.hours_today = round(emp.hours_today + hours, 4)
            emp.labor_cost_today = round(emp.hours_today * emp.hourly_rate, 2)
        # nosemgrep: is-function-without-parentheses
        emp.is_clocked_in = False
        emp.clock_in_time = None

    async def tick(self, sim_time: datetime, delta_seconds: float) -> None:
        variance = self.config.clock_in_variance_min
        sim_h, sim_m = sim_time.hour, sim_time.minute

        for emp in self.store.schedule.employees:
            shift = self._today_shift(emp, sim_time)
            if not shift:
                # No shift today — clock out if still in
                # nosemgrep: is-function-without-parentheses
                if emp.is_clocked_in:
                    self.clock_out(emp.id, sim_time)
                continue

            start_h, start_m = self._parse_time(shift.start)
            end_h, end_m = self._parse_time(shift.end)

            # Auto clock-in: within variance window of shift start
            # nosemgrep: is-function-without-parentheses
            if not emp.is_clocked_in:
                diff = (sim_h * 60 + sim_m) - (start_h * 60 + start_m)
                if abs(diff) <= variance:
                    self.clock_in(emp.id, sim_time)

            # Auto clock-out: within variance window of shift end
            # nosemgrep: is-function-without-parentheses
            if emp.is_clocked_in:
                diff = (sim_h * 60 + sim_m) - (end_h * 60 + end_m)
                if abs(diff) <= variance and (end_h * 60 + end_m) > (start_h * 60 + start_m):
                    self.clock_out(emp.id, sim_time)

            # Update hours for currently clocked-in employees
            # nosemgrep: is-function-without-parentheses
            if emp.is_clocked_in and emp.clock_in_time:
                hours = (sim_time - emp.clock_in_time).total_seconds() / 3600.0
                emp.hours_today = round(hours, 4)
                emp.labor_cost_today = round(emp.hours_today * emp.hourly_rate, 2)

        # Compute aggregate labor cost
        total_cost = sum(e.labor_cost_today for e in self.store.schedule.employees)
        # nosemgrep: is-function-without-parentheses
        on_clock = sum(1 for e in self.store.schedule.employees if e.is_clocked_in)
        revenue = 0.0
        if self.pos and self.store.pos.daily_sales:
            revenue = self.store.pos.daily_sales.total_revenue
        pct = round((total_cost / revenue) * 100, 2) if revenue > 0 else 0.0

        self.store.schedule.labor_cost = LaborCost(
            total_labor_cost=round(total_cost, 2),
            labor_cost_percentage=pct,
            employees_on_clock=on_clock,
        )

        await self.store.update("schedule", "schedule_updated", {
            "employees": self.store.schedule.employees,
            "labor_cost": self.store.schedule.labor_cost,
        }, sim_time=sim_time)
