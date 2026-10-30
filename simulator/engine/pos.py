# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POS / Order Generator simulation module."""

import random
from datetime import datetime

from simulator.models.pos import DailySales, HourlySales, Order, OrderItem, MenuItem, POSState
from simulator.models.simulation import SimulationConfig
from simulator.state.store import StateStore

# Order lifecycle transitions (index = current, value = next)
_LIFECYCLE = ["placed", "preparing", "ready", "served", "completed"]


class POSModule:
    """Generates orders based on time-of-day and advances order lifecycles."""

    def __init__(
        self,
        state_store: StateStore,
        menu_items: list[MenuItem],
        sim_config: SimulationConfig,
        inventory_module=None,  # InventoryModule, set after construction to avoid circular
    ) -> None:
        self.store = state_store
        self.menu = menu_items
        self.config = sim_config
        self.inventory = inventory_module
        # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
        self._rng = random.Random(sim_config.random_seed)  # nosec B311
        self._fractional_orders = 0.0  # accumulator for sub-1 order generation

    def _is_peak(self, hour: int) -> bool:
        return any(start <= hour < end for start, end in self.config.peak_hours)

    def _orders_per_hour(self, hour: int) -> int:
        if self._is_peak(hour):
            return self.config.peak_orders_per_hour
        return self.config.off_peak_orders_per_hour

    def _generate_order(self, sim_time: datetime, pos: POSState) -> Order:
        count = self._rng.randint(1, 4)
        items = []
        for mi in self._rng.choices(self.menu, k=count):
            items.append(OrderItem(
                menu_item_id=mi.id, menu_item_name=mi.name, price=mi.price, quantity=1,
            ))
        order = Order(
            id=pos.next_order_id,
            items=items,
            total=round(sum(i.price * i.quantity for i in items), 2),
            status="placed",
            placed_at=sim_time,
            updated_at=sim_time,
        )
        pos.next_order_id += 1
        return order

    def _deduct_ingredients(self, order: Order) -> None:
        if not self.inventory:
            return
        menu_map = {m.id: m for m in self.menu}
        for item in order.items:
            mi = menu_map.get(item.menu_item_id)
            if not mi:
                continue
            for ing in mi.ingredients:
                self.inventory.deduct(ing.id, ing.quantity * item.quantity,
                                      sim_time=order.placed_at, order_id=order.id)

    async def tick(self, sim_time: datetime, delta_seconds: float) -> None:
        pos = self.store.pos
        hour = sim_time.hour
        rate = self._orders_per_hour(hour)

        # Generate orders proportional to elapsed simulated time
        expected = rate * (delta_seconds / 3600.0) + self._fractional_orders
        num_orders = int(expected)
        self._fractional_orders = expected - num_orders

        new_orders: list[Order] = []
        for _ in range(num_orders):
            order = self._generate_order(sim_time, pos)
            self._deduct_ingredients(order)
            pos.orders.append(order)
            new_orders.append(order)

        # Advance existing active orders through lifecycle
        prep_seconds = self.config.avg_prep_time_min * 60
        stage_time = prep_seconds / max(len(_LIFECYCLE) - 1, 1)

        for order in pos.orders:
            if order.status in ("completed", "voided"):
                continue
            # Void check
            if order.status != "completed" and self._rng.random() < self.config.void_rate * (delta_seconds / 3600.0):
                order.status = "voided"
                order.void_reason = "customer_cancelled"
                order.updated_at = sim_time
                continue
            # Advance lifecycle based on elapsed time since last update
            elapsed = (sim_time - order.updated_at).total_seconds()
            if elapsed >= stage_time:
                idx = _LIFECYCLE.index(order.status)
                if idx < len(_LIFECYCLE) - 1:
                    order.status = _LIFECYCLE[idx + 1]
                    order.updated_at = sim_time

        # Rebuild daily sales
        today = sim_time.date().isoformat()
        completed = [o for o in pos.orders if o.status == "completed"]
        voided = [o for o in pos.orders if o.status == "voided"]
        revenue = sum(o.total for o in completed)
        count = len([o for o in pos.orders if o.status != "voided"])
        hourly: dict[int, HourlySales] = {}
        for o in completed:
            h = o.placed_at.hour
            hs = hourly.setdefault(h, HourlySales(hour=h, revenue=0, order_count=0))
            hs.revenue = round(hs.revenue + o.total, 2)
            hs.order_count += 1

        pos.daily_sales = DailySales(
            date=today,
            total_revenue=round(revenue, 2),
            order_count=count,
            avg_order_value=round(revenue / count, 2) if count else 0,
            void_count=len(voided),
            hourly_breakdown=sorted(hourly.values(), key=lambda x: x.hour),
        )

        if new_orders:
            await self.store.update("pos", "orders_updated", {
                "orders": pos.orders,
                "daily_sales": pos.daily_sales,
                "next_order_id": pos.next_order_id,
            }, sim_time=sim_time)
        elif any(o.status not in ("completed", "voided") for o in pos.orders):
            await self.store.update("pos", "orders_updated", {
                "orders": pos.orders,
                "daily_sales": pos.daily_sales,
            }, sim_time=sim_time)
