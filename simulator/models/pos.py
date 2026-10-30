# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POS data models for the restaurant simulator."""

from datetime import datetime

from pydantic import BaseModel


class IngredientRequirement(BaseModel):
    """Ingredient requirement for a menu item."""

    id: str
    quantity: float


class MenuItem(BaseModel):
    """A menu item available for ordering."""

    id: str
    name: str
    price: float
    category: str  # appetizer, entree, side, drink, dessert
    prep_time_min: float
    ingredients: list[IngredientRequirement]


class OrderItem(BaseModel):
    """A single item within an order."""

    menu_item_id: str
    menu_item_name: str
    price: float
    quantity: int = 1


class Order(BaseModel):
    """A customer order with lifecycle tracking."""

    id: int  # Sequential, starting from 800
    items: list[OrderItem]
    total: float
    status: str  # placed, preparing, ready, served, completed, voided
    placed_at: datetime
    updated_at: datetime
    void_reason: str | None = None


class HourlySales(BaseModel):
    """Sales summary for a single hour."""

    hour: int
    revenue: float
    order_count: int


class DailySales(BaseModel):
    """Daily sales summary with hourly breakdown."""

    date: str  # ISO date
    total_revenue: float
    order_count: int
    avg_order_value: float
    void_count: int
    hourly_breakdown: list[HourlySales]


class POSState(BaseModel):
    """Complete POS state for the state store."""

    orders: list[Order] = []
    daily_sales: DailySales | None = None
    next_order_id: int = 800
