# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inventory tracker simulation module."""

import logging
import random
from datetime import datetime

from simulator.models.inventory import ConsumptionEvent, InventoryItem, InventoryState, WasteEvent, WasteReason
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)
_WASTE_REASONS = list(WasteReason)
_MAX_CONSUMPTION_LOG = 10_000


class InventoryModule:
    """Tracks stock levels, deductions, waste events, and reorder alerts."""

    def __init__(self, state_store: StateStore, ingredients_config: list[dict], sim_config, random_seed: int | None = None) -> None:
        self.store = state_store
        self.config = sim_config
        # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
        self._rng = random.Random(random_seed)  # nosec B311
        self._items_map: dict[str, InventoryItem] = {}
        # Wired by main.py once the MQTT bridge is up (spec 023 ext): an
        # AlertEventPublisher. Emits a low-stock alert on the needs_reorder
        # False->True transition; _low_alerted tracks currently-alerted items
        # (re-armed when stock recovers above threshold).
        self.alert_sink = None
        self._low_alerted: set[str] = set()
        items = []
        for ing in ingredients_config:
            item = InventoryItem(
                id=ing["id"], name=ing["name"], unit=ing["unit"],
                current_quantity=ing["initial_quantity"],
                reorder_threshold=ing["reorder_threshold"],
                max_quantity=ing["max_quantity"],
            )
            item.needs_reorder = item.current_quantity < item.reorder_threshold
            items.append(item)
            self._items_map[item.id] = item
        self.store.inventory = InventoryState(items=items)

    def _log_consumption(self, item_id: str, quantity: float, reason: str,
                         timestamp: datetime, order_id: int | None = None) -> None:
        item = self._items_map.get(item_id)
        name = item.name if item else item_id
        log = self.store.inventory.consumption_log
        log.append(ConsumptionEvent(
            item_id=item_id, item_name=name, quantity=quantity,
            reason=reason, order_id=order_id, timestamp=timestamp,
        ))
        # FIFO eviction when cap exceeded
        if len(log) > _MAX_CONSUMPTION_LOG:
            self.store.inventory.consumption_log = log[-_MAX_CONSUMPTION_LOG:]

    def deduct(self, ingredient_id: str, quantity: float, sim_time: datetime | None = None, order_id: int | None = None) -> None:
        item = self._items_map.get(ingredient_id)
        if not item:
            return
        item.current_quantity = max(0, round(item.current_quantity - quantity, 4))
        item.needs_reorder = item.current_quantity < item.reorder_threshold
        self._log_consumption(ingredient_id, quantity, "order", sim_time or datetime.now(), order_id)

    def log_waste(self, item_id: str, quantity: float, reason: str, sim_time: datetime | None = None) -> None:
        item = self._items_map.get(item_id)
        if not item:
            raise ValueError(f"Inventory item not found: {item_id}")
        item.current_quantity = max(0, round(item.current_quantity - quantity, 4))
        item.needs_reorder = item.current_quantity < item.reorder_threshold
        ts = sim_time or datetime.now()
        self.store.inventory.waste_history.append(WasteEvent(
            item_id=item_id, item_name=item.name, quantity=quantity,
            reason=WasteReason(reason), timestamp=ts,
        ))
        self._log_consumption(item_id, quantity, "waste", ts)

    async def tick(self, sim_time: datetime, delta_seconds: float) -> None:
        # Stochastic waste events
        prob = self.config.waste_probability * (delta_seconds / 3600.0)
        for item in self.store.inventory.items:
            if item.current_quantity > 0 and self._rng.random() < prob:
                waste_qty = round(self._rng.uniform(0.1, 1.0), 2)
                waste_qty = min(waste_qty, item.current_quantity)
                reason = self._rng.choice(_WASTE_REASONS)
                item.current_quantity = max(0, round(item.current_quantity - waste_qty, 4))
                item.needs_reorder = item.current_quantity < item.reorder_threshold
                self.store.inventory.waste_history.append(WasteEvent(
                    item_id=item.id, item_name=item.name, quantity=waste_qty,
                    reason=reason, timestamp=sim_time,
                ))
                self._log_consumption(item.id, waste_qty, "waste", sim_time)

        await self.store.update("inventory", "stock_updated", {
            "items": self.store.inventory.items,
            "waste_history": self.store.inventory.waste_history,
        }, sim_time=sim_time)
        await self._emit_low_stock_alerts(sim_time)

    async def _emit_low_stock_alerts(self, sim_time: datetime) -> None:
        """Emit a proactive low-stock alert when an item transitions below its
        reorder threshold; re-arm when it recovers (spec 023 ext). Per-item
        fire-and-forget -- a publish failure must not break the tick."""
        if self.alert_sink is None:
            return
        for item in self.store.inventory.items:
            if item.needs_reorder and item.id not in self._low_alerted:
                self._low_alerted.add(item.id)
                try:
                    await self.alert_sink.publish_inventory_alert(item, sim_time)
                except Exception:  # noqa: BLE001 - publish must not break the tick
                    logger.exception("inventory low-stock alert publish failed item=%s", item.id)
            elif not item.needs_reorder and item.id in self._low_alerted:
                self._low_alerted.discard(item.id)  # recovered -> re-arm
