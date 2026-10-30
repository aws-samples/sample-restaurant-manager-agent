# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for ``ConsumptionEvent.order_id`` typing.

The POS engine deducts ingredients using the integer ``Order.id`` (sequential
from 800). ``ConsumptionEvent.order_id`` was previously typed ``str | None``, so
constructing the event from an int raised a pydantic ``ValidationError`` inside
``POSModule.tick`` — aborting the engine tick and meaning order-reason
consumption events were never logged. The field is now ``int | None`` to match
``Order.id``. (Pre-existing spec-017 bug, found during the spec-020 IoT migration.)
"""
from datetime import datetime

from simulator.engine.inventory import InventoryModule
from simulator.models.inventory import ConsumptionEvent
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.state.store import StateStore

_INGREDIENTS = [
    {
        "id": "beef_patty",
        "name": "Beef Patty",
        "unit": "units",
        "initial_quantity": 100.0,
        "reorder_threshold": 10.0,
        "max_quantity": 200.0,
    },
]


def _make_store_and_config() -> tuple[StateStore, SimulationConfig]:
    config = SimulationConfig(peak_hours=[[11, 13], [17, 20]])
    clock = ClockState(
        current_time=datetime(2026, 6, 18, 8, 0, 0),
        speed_multiplier=1.0,
        is_paused=False,
    )
    return StateStore(clock_state=clock, config=config), config


def test_consumption_event_accepts_int_order_id() -> None:
    """ConsumptionEvent must accept an integer order_id (matches Order.id)."""
    ev = ConsumptionEvent(
        item_id="beef_patty",
        item_name="Beef Patty",
        quantity=2.0,
        reason="order",
        order_id=829,
        timestamp=datetime(2026, 6, 18, 8, 0, 0),
    )
    assert ev.order_id == 829
    assert isinstance(ev.order_id, int)
    # Serializes cleanly via the get_usage_report path (model_dump(mode="json")).
    assert ev.model_dump(mode="json")["order_id"] == 829


def test_consumption_event_allows_none_order_id() -> None:
    """Waste-reason events carry no order_id."""
    ev = ConsumptionEvent(
        item_id="beef_patty",
        item_name="Beef Patty",
        quantity=1.0,
        reason="waste",
        timestamp=datetime(2026, 6, 18, 8, 0, 0),
    )
    assert ev.order_id is None


def test_deduct_logs_consumption_with_int_order_id() -> None:
    """InventoryModule.deduct with an int order_id logs an event without raising."""
    store, config = _make_store_and_config()
    inv = InventoryModule(store, _INGREDIENTS, config, random_seed=1)

    inv.deduct(
        "beef_patty",
        2.0,
        sim_time=datetime(2026, 6, 18, 8, 0, 0),
        order_id=829,
    )

    log = store.inventory.consumption_log
    assert len(log) == 1
    assert log[0].order_id == 829
    assert log[0].reason == "order"
    assert log[0].item_id == "beef_patty"
