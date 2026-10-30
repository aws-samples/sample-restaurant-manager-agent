# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equipment data models for the restaurant simulator."""

from datetime import datetime

from pydantic import BaseModel


class Alert(BaseModel):
    """An alert on a piece of kitchen equipment."""

    type: str  # oil_change_needed, compressor_fault, door_ajar, thermostat_malfunction
    severity: str  # warning, critical
    timestamp: datetime
    acknowledged: bool = False


class Equipment(BaseModel):
    """A piece of kitchen equipment with thermal monitoring."""

    id: str
    name: str
    type: str  # fryer, oven, grill, cooler, freezer
    target_temp: float
    current_temp: float
    unit: str  # F or C
    warning_threshold: float  # Degrees from target to trigger warning
    alert_threshold: float  # Degrees from target to trigger alert
    status: str = "normal"  # normal, warning, alert
    failure_prob_per_hour: float
    active_alert: Alert | None = None
    timer_remaining_seconds: float | None = None


class TempReading(BaseModel):
    """A timestamped temperature reading for an equipment."""

    timestamp: datetime
    temperature: float
    equipment_id: str


class EquipmentState(BaseModel):
    """Complete equipment state for the state store."""

    equipment: list[Equipment] = []
    temp_history: dict[str, list[TempReading]] = {}  # equipment_id -> readings
