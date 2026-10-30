# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule data models for the restaurant simulator."""

from datetime import datetime

from pydantic import BaseModel


class ShiftTemplate(BaseModel):
    """A shift template defining a recurring work period."""

    day: str  # monday, tuesday, etc.
    start: str  # HH:MM
    end: str  # HH:MM


class Employee(BaseModel):
    """An employee with shift and labor tracking."""

    id: str
    name: str
    role: str  # manager, line_cook, prep_cook, server, host, dishwasher
    hourly_rate: float
    shifts: list[ShiftTemplate]
    is_clocked_in: bool = False
    clock_in_time: datetime | None = None
    hours_today: float = 0.0
    labor_cost_today: float = 0.0


class LaborCost(BaseModel):
    """Aggregated labor cost summary."""

    total_labor_cost: float
    labor_cost_percentage: float  # labor cost / daily revenue * 100
    employees_on_clock: int


class ScheduleState(BaseModel):
    """Complete schedule state for the state store."""

    employees: list[Employee] = []
    labor_cost: LaborCost = LaborCost(
        total_labor_cost=0, labor_cost_percentage=0, employees_on_clock=0
    )
