# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simulation engine orchestrator — wires clock, store, and all modules."""

import json
import logging
from datetime import datetime
from pathlib import Path

from simulator.engine.clock import VirtualClock
from simulator.engine.equipment import EquipmentModule
from simulator.engine.inventory import InventoryModule
from simulator.engine.pos import POSModule
from simulator.engine.schedule import ScheduleModule
from simulator.models.pos import MenuItem, IngredientRequirement
from simulator.models.simulation import SimulationConfig
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)


class SimulationEngine:
    """Orchestrates all simulation modules on each clock tick."""

    def __init__(self, clock: VirtualClock, state_store: StateStore, config: SimulationConfig, config_dir: str = "config") -> None:
        self.clock = clock
        self.store = state_store
        self.config = config
        self.config_dir = Path(config_dir)
        self.pos: POSModule | None = None
        self.equipment: EquipmentModule | None = None
        self.inventory: InventoryModule | None = None
        self.schedule: ScheduleModule | None = None

    async def initialize(self) -> None:
        """Load config files and create all modules."""
        menu_data = json.loads((self.config_dir / "menu.json").read_text())
        equip_data = json.loads((self.config_dir / "equipment.json").read_text())
        staff_data = json.loads((self.config_dir / "staff.json").read_text())

        menu_items = [
            MenuItem(id=m["id"], name=m["name"], price=m["price"], category=m["category"],
                     prep_time_min=m["prep_time_min"],
                     ingredients=[IngredientRequirement(**i) for i in m.get("ingredients", [])])
            for m in menu_data["items"]
        ]

        seed = self.config.random_seed
        self.inventory = InventoryModule(self.store, menu_data["ingredients"], self.config, random_seed=seed)
        self.pos = POSModule(self.store, menu_items, self.config, inventory_module=self.inventory)
        self.equipment = EquipmentModule(self.store, equip_data["equipment"], random_seed=seed)
        self.schedule = ScheduleModule(self.store, staff_data["employees"], self.config, pos_module=self.pos, random_seed=seed)

        self.clock.on_tick(self.on_tick)
        # semgrep false positive: `is_running` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        self.store.simulation.is_running = True

    async def on_tick(self, sim_time: datetime, delta_seconds: float) -> None:
        """Called by the clock each tick. Runs all modules in sequence."""
        try:
            await self.pos.tick(sim_time, delta_seconds)
            await self.equipment.tick(sim_time, delta_seconds)
            await self.inventory.tick(sim_time, delta_seconds)
            await self.schedule.tick(sim_time, delta_seconds)
        except Exception:
            logger.exception("Error during simulation tick")

        # Update clock state in store
        self.store.simulation.clock = self.clock.get_state()
        await self.store.update("simulation", "tick", {
            "clock": self.clock.get_state(),
        }, sim_time=sim_time)

    async def inject_event(self, event_type: str, params: dict) -> dict:
        """Manually inject a simulation event."""
        sim_time = self.clock.current_time
        if event_type == "rush":
            # Generate a burst of orders
            burst = params.get("count", 10)
            for _ in range(burst):
                order = self.pos._generate_order(sim_time, self.store.pos)
                self.pos._deduct_ingredients(order)
                self.store.pos.orders.append(order)
            await self.store.update("pos", "rush_event", {"orders": self.store.pos.orders}, sim_time=sim_time)
            return {"injected": "rush", "orders_added": burst}

        if event_type == "equipment_failure":
            eid = params.get("equipment_id")
            for e in self.store.equipment.equipment:
                if e.id == eid:
                    from simulator.models.equipment import Alert
                    self.equipment.raise_alert(
                        e,
                        Alert(type=params.get("failure_type", "compressor_fault"), severity="critical", timestamp=sim_time),
                    )
                    await self.store.update("equipment", "equipment_failure", {"equipment": self.store.equipment.equipment}, sim_time=sim_time)
                    return {"injected": "equipment_failure", "equipment_id": eid}
            return {"error": f"Equipment {eid} not found"}

        if event_type == "waste":
            self.inventory.log_waste(params["item_id"], params["quantity"], params["reason"], sim_time)
            await self.store.update("inventory", "waste_logged", {
                "items": self.store.inventory.items, "waste_history": self.store.inventory.waste_history,
            }, sim_time=sim_time)
            return {"injected": "waste"}

        if event_type == "no_show":
            eid = params.get("employee_id")
            for emp in self.store.schedule.employees:
                if emp.id == eid:
                    # semgrep false positive: `is_clocked_in` is a bool attribute/@property, not a method -- no call intended.
                    # nosemgrep: is-function-without-parentheses
                    emp.is_clocked_in = False
                    emp.clock_in_time = None
                    await self.store.update("schedule", "no_show", {"employees": self.store.schedule.employees}, sim_time=sim_time)
                    return {"injected": "no_show", "employee_id": eid}
            return {"error": f"Employee {eid} not found"}

        return {"error": f"Unknown event type: {event_type}"}
