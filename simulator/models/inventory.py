# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inventory data models for the restaurant simulator."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class WasteReason(str, Enum):
    """Valid reasons for waste events."""

    EXPIRED = "expired"
    SPILLAGE = "spillage"
    CONTAMINATION = "contamination"
    OVERPRODUCTION = "overproduction"


class InventoryItem(BaseModel):
    """An inventory item with stock tracking."""

    id: str
    name: str
    current_quantity: float
    unit: str  # lbs, units, gallons, etc.
    reorder_threshold: float
    max_quantity: float
    needs_reorder: bool = False


class WasteEvent(BaseModel):
    """A recorded waste event."""

    item_id: str
    item_name: str
    quantity: float
    reason: WasteReason
    timestamp: datetime


class InventoryState(BaseModel):
    """Complete inventory state for the state store."""

    items: list[InventoryItem] = []
    waste_history: list[WasteEvent] = []
    consumption_log: list["ConsumptionEvent"] = []


class ConsumptionEvent(BaseModel):
    """A recorded stock deduction (from orders or waste)."""

    item_id: str
    item_name: str
    quantity: float
    reason: str  # "order" | "waste"
    order_id: int | None = None  # matches Order.id (int); None for waste-reason events
    timestamp: datetime
