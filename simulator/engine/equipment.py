# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Kitchen equipment simulation module."""

import asyncio
import logging
import random
from datetime import datetime
from typing import TYPE_CHECKING

from simulator.models.equipment import Alert, Equipment, EquipmentState, TempReading
from simulator.state.store import StateStore

if TYPE_CHECKING:
    from simulator.mqtt.events import AlertEventSink

logger = logging.getLogger(__name__)

_FAILURE_TYPES = [
    ("oil_change_needed", "warning"),
    ("compressor_fault", "critical"),
    ("door_ajar", "warning"),
    ("thermostat_malfunction", "critical"),
]


class EquipmentModule:
    """Simulates kitchen equipment thermal behavior and failures."""

    def __init__(self, state_store: StateStore, equipment_config: list[dict], random_seed: int | None = None) -> None:
        self.store = state_store
        # bandit B311 false positive: random drives seeded simulation data / retry jitter, not anything security-sensitive.
        self._rng = random.Random(random_seed)  # nosec B311
        # Initialize equipment state from config
        equip_list = []
        for cfg in equipment_config:
            equip_list.append(Equipment(
                id=cfg["id"], name=cfg["name"], type=cfg["type"],
                target_temp=cfg["target_temp"], current_temp=cfg["target_temp"],
                unit=cfg.get("unit", "F"),
                warning_threshold=cfg["warning_threshold"],
                alert_threshold=cfg["alert_threshold"],
                failure_prob_per_hour=cfg.get("failure_prob_per_hour", 0.01),
                status="normal",
            ))
        self.store.equipment = EquipmentState(
            equipment=equip_list,
            temp_history={e.id: [] for e in equip_list},
        )
        # Optional store->cloud alert-event sink (spec 023 Task 3). Wired by
        # main.py after the MQTT bridge starts; None in local/dev (publish is a
        # no-op). Fire-and-forget publish tasks are tracked to avoid GC.
        self.alert_sink: "AlertEventSink | None" = None
        self._alert_tasks: set[asyncio.Task] = set()

    def _classify_status(self, equip: Equipment) -> str:
        diff = abs(equip.current_temp - equip.target_temp)
        if diff > equip.alert_threshold:
            return "alert"
        if diff > equip.warning_threshold:
            return "warning"
        return "normal"

    async def tick(self, sim_time: datetime, delta_seconds: float) -> None:
        state = self.store.equipment
        for equip in state.equipment:
            # Gaussian drift: mean-revert toward target with noise
            drift = (equip.target_temp - equip.current_temp) * 0.05
            noise = self._rng.gauss(0, 0.5)
            equip.current_temp = round(equip.current_temp + drift + noise, 2)

            equip.status = self._classify_status(equip)

            # Record temperature
            state.temp_history.setdefault(equip.id, []).append(
                TempReading(timestamp=sim_time, temperature=equip.current_temp, equipment_id=equip.id)
            )

            # Random failures scaled by delta
            prob = equip.failure_prob_per_hour * (delta_seconds / 3600.0)
            if not equip.active_alert and self._rng.random() < prob:
                ftype, severity = self._rng.choice(_FAILURE_TYPES)
                self.raise_alert(equip, Alert(type=ftype, severity=severity, timestamp=sim_time))

            # Timer countdown
            if equip.timer_remaining_seconds is not None:
                equip.timer_remaining_seconds = max(0, equip.timer_remaining_seconds - delta_seconds)
                if equip.timer_remaining_seconds == 0:
                    await self.store.update("equipment", "timer_expired", {
                        "equipment_id": equip.id,
                    }, sim_time=sim_time)
                    equip.timer_remaining_seconds = None

        await self.store.update("equipment", "temperature_updated", {
            "equipment": state.equipment,
            "temp_history": state.temp_history,
        }, sim_time=sim_time)

    def raise_alert(self, equip: Equipment, alert: Alert) -> None:
        """Set an equipment's active alert and emit a proactive alert event.

        Local state (``active_alert``) is updated first; the event publish is
        fire-and-forget and can never abort the caller, so a failed or slow
        publish does not break the simulation tick (spec 023 R1.3).
        """
        equip.active_alert = alert
        self._emit_alert_event(equip, alert)

    def _emit_alert_event(self, equip: Equipment, alert: Alert) -> None:
        if self.alert_sink is None:
            return
        try:
            task = asyncio.create_task(self._safe_publish(equip, alert))
        except RuntimeError:
            # No running event loop (e.g. some unit contexts); local state is
            # already set, so just skip the publish.
            logger.debug("no running loop; skipping alert event publish")
            return
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    async def _safe_publish(self, equip: Equipment, alert: Alert) -> None:
        try:
            await self.alert_sink.publish_equipment_alert(equip, alert)
        except Exception:  # noqa: BLE001 - a publish failure must not break the tick
            logger.warning(
                "alert_event_publish_failed equipment=%s condition=%s",
                equip.id, alert.type, exc_info=True,
            )

    def set_timer(self, equipment_id: str, minutes: float) -> None:
        for equip in self.store.equipment.equipment:
            if equip.id == equipment_id:
                equip.timer_remaining_seconds = minutes * 60
                return
        raise ValueError(f"Equipment not found: {equipment_id}")
