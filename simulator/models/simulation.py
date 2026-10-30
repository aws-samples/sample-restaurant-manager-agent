# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simulation and shared data models for the restaurant simulator."""

from datetime import datetime

from pydantic import BaseModel


class ClockState(BaseModel):
    """Current state of the virtual clock."""

    current_time: datetime
    speed_multiplier: float
    is_paused: bool


class SimulationConfig(BaseModel):
    """Configuration parameters for the simulation engine."""

    tick_interval_seconds: float = 1.0
    peak_hours: list[list[int]]  # [[11,13],[17,20]]
    peak_orders_per_hour: int = 45
    off_peak_orders_per_hour: int = 12
    avg_order_value: float = 14.50
    void_rate: float = 0.03
    avg_prep_time_min: float = 12.0
    waste_probability: float = 0.01
    clock_in_variance_min: float = 5.0
    default_speed_multiplier: float = 10.0
    random_seed: int | None = None


class SimulationState(BaseModel):
    """Complete simulation state for the state store."""

    clock: ClockState
    config: SimulationConfig
    is_running: bool = False


class StateEvent(BaseModel):
    """An event emitted when state changes."""

    domain: str  # pos, equipment, inventory, schedule
    mutation_type: str  # order_placed, temperature_updated, stock_depleted, etc.
    data: dict  # The changed data slice
    sim_time: datetime


# REST API Request/Response Models


class SimControlRequest(BaseModel):
    """Request to control the simulation (pause, resume, reset)."""

    action: str  # pause, resume, reset
    speed: float | None = None


class InjectEventRequest(BaseModel):
    """Request to inject a manual event into the simulation."""

    type: str  # rush, equipment_failure, waste, no_show
    params: dict = {}


class SetTimerRequest(BaseModel):
    """Request to set a timer on equipment."""

    minutes: float


class LogWasteRequest(BaseModel):
    """Request to log a waste event."""

    item_id: str
    quantity: float
    reason: str


class ClockRequest(BaseModel):
    """Request to clock an employee in or out."""

    employee_id: str
    action: str  # in, out


class SuccessResponse(BaseModel):
    """Standard success response."""

    success: bool = True


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    code: int
