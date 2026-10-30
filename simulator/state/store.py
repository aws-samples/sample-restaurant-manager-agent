# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralized in-memory state store with async event emission."""

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from simulator.models.alerts import AlertInboxState, AlertNotification
from simulator.models.equipment import EquipmentState
from simulator.models.inventory import InventoryState
from simulator.models.pos import POSState
from simulator.models.schedule import ScheduleState
from simulator.models.simulation import ClockState, SimulationConfig, SimulationState, StateEvent

Subscriber = Callable[[StateEvent], Awaitable[None]]

DOMAINS = ("pos", "equipment", "inventory", "schedule", "simulation", "alerts")


class StateStore:
    """Centralized in-memory state with domain-scoped event emission."""

    def __init__(self, clock_state: ClockState, config: SimulationConfig) -> None:
        self.pos = POSState()
        self.equipment = EquipmentState()
        self.inventory = InventoryState()
        self.schedule = ScheduleState()
        self.simulation = SimulationState(clock=clock_state, config=config)
        self.alerts = AlertInboxState()
        # Wired by main.py once the MQTT bridge is up: async (alert_id) -> None,
        # publishes the events/ack envelope so the cloud workflow resumes (Task 11).
        self.alert_ack_publisher = None
        # Wired by main.py once the MQTT bridge is up (spec 025 Task 1d):
        #   push_subscription_publisher: async (subscription: dict) -> None
        #   push_request_publisher:      async (alert_id: str, notification: dict) -> None
        self.push_subscription_publisher = None
        self.push_request_publisher = None
        self._lock = asyncio.Lock()
        self._subscribers: dict[str | None, list[Subscriber]] = defaultdict(list)

    def _get_domain(self, domain: str):
        """Return the state object for a domain."""
        return getattr(self, domain)

    async def update(self, domain: str, mutation_type: str, data: dict, sim_time: datetime) -> None:
        """Update domain state and emit a StateEvent to subscribers."""
        async with self._lock:
            state = self._get_domain(domain)
            for key, value in data.items():
                setattr(state, key, value)

        # Serialize data for event consumers (WebSocket needs JSON-safe dicts)
        serialized = {}
        for key, value in data.items():
            if hasattr(value, 'model_dump'):
                serialized[key] = value.model_dump(mode='json')
            elif isinstance(value, list) and value and hasattr(value[0], 'model_dump'):
                serialized[key] = [v.model_dump(mode='json') for v in value]
            elif isinstance(value, dict):
                serialized[key] = {
                    k: ([r.model_dump(mode='json') for r in v] if isinstance(v, list) and v and hasattr(v[0], 'model_dump') else v)
                    for k, v in value.items()
                }
            else:
                serialized[key] = value

        event = StateEvent(domain=domain, mutation_type=mutation_type, data=serialized, sim_time=sim_time)
        await self._emit(event)

    async def _emit(self, event: StateEvent) -> None:
        """Notify domain-specific and global subscribers."""
        targets = list(self._subscribers.get(event.domain, []))
        targets += list(self._subscribers.get(None, []))
        await asyncio.gather(*(cb(event) for cb in targets), return_exceptions=True)

    def subscribe(self, domain: str | None, callback: Subscriber) -> None:
        """Subscribe to state changes. domain=None subscribes to all domains."""
        self._subscribers[domain].append(callback)

    async def get_snapshot(self) -> dict:
        """Return full state snapshot as a serializable dict."""
        async with self._lock:
            return {d: self._get_domain(d).model_dump(mode='json') for d in DOMAINS}

    async def get_domain_state(self, domain: str) -> dict:
        """Return a specific domain's state as a dict."""
        async with self._lock:
            return self._get_domain(domain).model_dump(mode='json')

    async def add_alert_notification(
        self, notification: dict, sim_time: datetime, max_entries: int = 100
    ) -> None:
        """Record a cloud-pushed proactive notification in the in-app inbox.

        Appends (bounded to ``max_entries``) and emits an ``alerts`` StateEvent,
        so the existing WebSocket broadcast pushes it to the always-on display
        (spec 023 Task 6). The cloud ``Alerts`` table remains the system of
        record; this inbox is the store-side view.
        """
        entry = AlertNotification(**notification) if isinstance(notification, dict) else notification
        entries = (self.alerts.entries + [entry])[-max_entries:]
        await self.update("alerts", "alert_received", {"entries": entries}, sim_time=sim_time)

    async def acknowledge_alert_entry(self, alert_id: str, sim_time: datetime) -> bool:
        """Mark an inbox alert acknowledged by ``alert_id`` and emit so the UI
        updates (spec 023 Task 7). Returns True if a matching entry was found."""
        found = False
        for entry in self.alerts.entries:
            if entry.alert_id == alert_id:
                entry.acknowledged = True
                found = True
        if found:
            await self.update(
                "alerts", "alert_acknowledged", {"entries": self.alerts.entries}, sim_time=sim_time
            )
        return found

    async def acknowledge_proactive_alert(self, alert_id: str, sim_time: datetime) -> dict:
        """Acknowledge a proactive alert end-to-end (spec 023 Task 11): mark the
        inbox entry AND publish the upstream ack so the cloud Step Functions
        workflow resumes. Shared by ``POST /api/alerts/{id}/ack`` and the agent's
        ``acknowledge_proactive_alert`` tool, so both origins behave identically.
        Idempotent: the ack handler no-ops a consumed/expired token."""
        found = await self.acknowledge_alert_entry(alert_id, sim_time)
        published = False
        if self.alert_ack_publisher is not None:
            await self.alert_ack_publisher(alert_id)
            published = True
        return {"alert_id": alert_id, "acknowledged": found, "ack_published": published}

    async def list_active_alerts(self) -> list[dict]:
        """Return unacknowledged proactive-alert inbox entries (newest first) for
        the get_active_alerts tool (spec 025 1c). The inbox (alerts.entries) holds
        cloud-raised notifications -- distinct from each equipment's active_alert."""
        async with self._lock:
            return [
                e.model_dump(mode="json")
                for e in reversed(self.alerts.entries)
                if not e.acknowledged
            ]

    async def relay_push_subscription(self, subscription: dict) -> bool:
        """Relay a browser Web Push subscription to the cloud registry over the
        outbound IoT bridge (spec 025 Task 1d). Returns False (no-op) when the
        bridge isn't wired (local dev without MQTT) or the subscription is empty."""
        if self.push_subscription_publisher is None or not subscription:
            return False
        await self.push_subscription_publisher(subscription)
        return True

    async def request_push_reengagement(self, alert_id: str, notification: dict) -> bool:
        """Ask the cloud push-sender to re-engage a backgrounded display for an
        alert (spec 025 Task 1d). Emitted on the notification path ONLY when no
        display is connected (the connectivity gate is wired at deploy/Task 1f).
        Returns False (no-op) when the bridge isn't wired."""
        if self.push_request_publisher is None:
            return False
        await self.push_request_publisher(alert_id, notification)
        return True

    async def handle_incoming_notification(
        self, notification: dict, sim_time: datetime, display_connected: bool
    ) -> dict:
        """Process a cloud-approved proactive notification (spec 023 Task 6 +
        spec 025 Task 1d/1f): record it in the in-app inbox, and -- when NO
        display is connected -- ask the cloud push-sender to wake one via Web
        Push. The connected path already surfaces the alert in-app (and speaks
        it if a voice session is live), so push fires only for a closed/asleep
        display. Returns ``{inboxed, push_requested}``."""
        await self.add_alert_notification(notification, sim_time=sim_time)
        push_requested = False
        if not display_connected:
            push_requested = await self.request_push_reengagement(
                notification.get("alert_id"), notification
            )
        return {"inboxed": True, "push_requested": push_requested}


def load_all_configs(config_dir: str | Path = "config") -> dict:
    """Load and parse all simulator JSON config files into a dict.

    Returns a dict with keys: ``menu``, ``equipment``, ``staff``, ``simulation``.
    """
    d = Path(config_dir)
    return {
        "menu": json.loads((d / "menu.json").read_text()),
        "equipment": json.loads((d / "equipment.json").read_text()),
        "staff": json.loads((d / "staff.json").read_text()),
        "simulation": json.loads((d / "simulation.json").read_text()),
    }


def _build_state_store_from_configs(
    configs: dict,
    start_time: datetime | None = None,
) -> "StateStore":
    """Internal: construct a fully-populated StateStore from loaded configs.

    Uses the same module constructors the engine uses so the state shape is
    identical. The clock is not started and no tick callbacks are registered.
    """
    # Local imports to avoid circular dependencies at module load time.
    from simulator.engine.equipment import EquipmentModule
    from simulator.engine.inventory import InventoryModule
    from simulator.engine.schedule import ScheduleModule
    from simulator.models.pos import IngredientRequirement, MenuItem

    sim_config = SimulationConfig(**configs["simulation"])
    resolved_start = start_time or datetime.now().replace(
        hour=7, minute=0, second=0, microsecond=0
    )
    clock_state = ClockState(
        current_time=resolved_start,
        speed_multiplier=sim_config.default_speed_multiplier,
        is_paused=False,
    )
    store = StateStore(clock_state=clock_state, config=sim_config)

    # Populate each domain using the engine modules' constructors. We don't
    # keep references; the modules' side effect is mutating ``store`` state.
    _ = InventoryModule(
        store,
        configs["menu"]["ingredients"],
        sim_config,
        random_seed=sim_config.random_seed,
    )
    _ = EquipmentModule(
        store,
        configs["equipment"]["equipment"],
        random_seed=sim_config.random_seed,
    )
    _ = ScheduleModule(
        store,
        configs["staff"]["employees"],
        sim_config,
        pos_module=None,
        random_seed=sim_config.random_seed,
    )

    # Convert menu items to Pydantic models and stash on POS state for
    # MCP tool lookups (e.g., validating menu_item_ids in the future).
    menu_items = [
        MenuItem(
            id=m["id"],
            name=m["name"],
            price=m["price"],
            category=m["category"],
            prep_time_min=m["prep_time_min"],
            ingredients=[IngredientRequirement(**i) for i in m.get("ingredients", [])],
        )
        for m in configs["menu"]["items"]
    ]
    # Menu items are not stored in POSState today; keep this a no-op unless
    # a future field is added. The variable suppresses unused-import warnings.
    _ = menu_items

    return store


# Attach ``from_configs`` as a classmethod on StateStore without modifying its
# earlier body.  This keeps the core class definition compact while providing
# the helper required by spec 001 (standalone MCP stdio runner).
def _from_configs(cls, configs: dict, start_time: datetime | None = None) -> "StateStore":
    """Construct a fully-populated StateStore from parsed config dicts.

    Parameters
    ----------
    configs:
        Dict produced by :func:`load_all_configs`.
    start_time:
        Optional simulated start datetime. Defaults to today at 07:00.

    The returned store has every domain populated exactly as the full
    simulator would initialise it (via each module's constructor), but no
    clock is started and no tick callbacks are registered. This is the
    store expected by the standalone MCP stdio runner.
    """
    return _build_state_store_from_configs(configs, start_time=start_time)


StateStore.from_configs = classmethod(_from_configs)  # type: ignore[attr-defined]
