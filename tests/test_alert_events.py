# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for store->cloud alert events (spec 023 Task 3).

Covers: the pure envelope builder (regression for the caught-loop type gotcha --
built from the real Equipment + Alert models and JSON-serializable), the
publisher event path (topic + QoS + disconnect buffering), the AlertEventPublisher
chain, and EquipmentModule.raise_alert fire-and-forget semantics (sets local
state, never raises, no-op without a sink).
"""
import asyncio
import json
import uuid
from datetime import datetime

from simulator.engine.equipment import EquipmentModule
from simulator.models.equipment import Alert, Equipment
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.mqtt.events import AlertEventPublisher, build_equipment_alert_envelope
from simulator.mqtt.publisher import ResponsePublisher
from simulator.state.store import StateStore

_EQUIP = [
    {
        "id": "fryer-1",
        "name": "Fryer 1",
        "type": "fryer",
        "target_temp": 350.0,
        "unit": "F",
        "warning_threshold": 15.0,
        "alert_threshold": 30.0,
        "failure_prob_per_hour": 0.0,  # deterministic: no organic alerts
    }
]


def _equipment() -> Equipment:
    return Equipment(
        id="fryer-1", name="Fryer 1", type="fryer", target_temp=350.0,
        current_temp=395.0, unit="F", warning_threshold=15.0,
        alert_threshold=30.0, failure_prob_per_hour=0.0, status="alert",
    )


def _alert() -> Alert:
    return Alert(type="compressor_fault", severity="critical",
                 timestamp=datetime(2026, 6, 24, 14, 0, 0))


def _make_module(sink=None) -> EquipmentModule:
    store = StateStore(
        clock_state=ClockState(
            current_time=datetime(2026, 6, 24, 14, 0, 0),
            speed_multiplier=1.0, is_paused=False,
        ),
        config=SimulationConfig(peak_hours=[[11, 13], [17, 20]]),
    )
    module = EquipmentModule(store, _EQUIP, random_seed=1)
    module.alert_sink = sink
    return module


class _FakeClient:
    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.published: list[tuple[str, bytes, int]] = []

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.published.append((topic, payload, qos))


class _RecordingSink:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def publish_equipment_alert(self, equipment, alert) -> None:
        self.calls.append((equipment.id, alert.type))
        if self._fail:
            raise RuntimeError("boom")


# --- envelope (regression for the caught-loop type gotcha) ---

def test_build_envelope_from_real_models_is_json_serializable():
    env = build_equipment_alert_envelope("sim-store-001", _equipment(), _alert())
    assert env["schema_version"] == "1.0"
    assert env["store_id"] == "sim-store-001"
    assert env["domain"] == "equipment"
    assert env["event_type"] == "alert_raised"
    assert env["severity"] == "critical"
    assert env["source_id"] == "fryer-1"
    assert env["condition"] == "compressor_fault"
    assert env["reading"]["current_temp"] == 395.0
    assert env["reading"]["alert_threshold"] == 30.0
    assert env["timestamp"] == "2026-06-24T14:00:00"
    assert env["dedup_key"] == "sim-store-001#equipment#fryer-1#compressor_fault"
    uuid.UUID(env["event_id"])  # valid uuid
    # No non-serializable types leak through (the caught-loop gotcha class).
    assert json.loads(json.dumps(env))["source_id"] == "fryer-1"


# --- publisher event path ---

async def test_publish_event_goes_to_events_topic_at_qos1():
    client = _FakeClient(connected=True)
    pub = ResponsePublisher(client, store_id="sim-store-001", buffer_size=8)
    await pub.publish_event("equipment", b"{}")
    assert client.published == [("stores/sim-store-001/events/equipment", b"{}", 1)]


async def test_publish_event_buffers_when_disconnected():
    client = _FakeClient(connected=False)
    pub = ResponsePublisher(client, store_id="sim-store-001", buffer_size=8)
    await pub.publish_event("equipment", b"{}")  # must not raise
    assert client.published == []
    assert pub.buffered_count == 1


async def test_alert_event_publisher_builds_and_ships():
    client = _FakeClient(connected=True)
    pub = ResponsePublisher(client, store_id="sim-store-001", buffer_size=8)
    aep = AlertEventPublisher(pub, "sim-store-001")
    await aep.publish_equipment_alert(_equipment(), _alert())
    topic, payload, qos = client.published[0]
    assert topic == "stores/sim-store-001/events/equipment"
    assert qos == 1
    env = json.loads(payload)
    assert env["source_id"] == "fryer-1" and env["condition"] == "compressor_fault"


# --- EquipmentModule.raise_alert fire-and-forget ---

async def test_raise_alert_sets_state_and_emits():
    sink = _RecordingSink()
    module = _make_module(sink)
    equip = module.store.equipment.equipment[0]
    alert = _alert()
    module.raise_alert(equip, alert)
    assert equip.active_alert is alert  # local state set first
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert sink.calls == [("fryer-1", "compressor_fault")]


async def test_raise_alert_survives_failing_sink():
    sink = _RecordingSink(fail=True)
    module = _make_module(sink)
    equip = module.store.equipment.equipment[0]
    alert = _alert()
    module.raise_alert(equip, alert)  # must not raise despite the failing sink
    assert equip.active_alert is alert
    await asyncio.sleep(0)
    assert sink.calls == [("fryer-1", "compressor_fault")]  # attempted, error swallowed


def test_raise_alert_is_noop_without_sink():
    module = _make_module(sink=None)
    equip = module.store.equipment.equipment[0]
    alert = _alert()
    module.raise_alert(equip, alert)
    assert equip.active_alert is alert  # local state still set; no publish, no error


async def test_publish_ack_goes_to_events_ack_topic():
    client = _FakeClient(connected=True)
    pub = ResponsePublisher(client, store_id="sim-store-001", buffer_size=8)
    aep = AlertEventPublisher(pub, "sim-store-001")
    await aep.publish_ack("equipment-fryer-1-compressor_fault-123")
    topic, payload, qos = client.published[0]
    assert topic == "stores/sim-store-001/events/ack" and qos == 1
    body = json.loads(payload)
    assert body["event_type"] == "ack"
    assert body["alert_id"] == "equipment-fryer-1-compressor_fault-123"
    assert body["store_id"] == "sim-store-001"


# --- inventory low-stock alerts (spec 023 ext) ---

_INGREDIENTS = [
    {"id": "tomatoes", "name": "Tomatoes", "unit": "lbs",
     "initial_quantity": 100.0, "reorder_threshold": 20.0, "max_quantity": 200.0},
]


def _inv_item(qty: float):
    from simulator.models.inventory import InventoryItem
    return InventoryItem(id="tomatoes", name="Tomatoes", unit="lbs", current_quantity=qty,
                         reorder_threshold=20.0, max_quantity=200.0, needs_reorder=qty < 20.0)


class _RecordingInvSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def publish_inventory_alert(self, item, sim_time) -> None:
        self.calls.append((item.id, item.current_quantity))


def _inv_module(sink=None):
    from simulator.engine.inventory import InventoryModule
    store = StateStore(
        clock_state=ClockState(current_time=datetime(2026, 6, 29, 9, 0, 0),
                               speed_multiplier=1.0, is_paused=False),
        config=SimulationConfig(peak_hours=[[11, 13], [17, 20]], waste_probability=0.0),
    )
    m = InventoryModule(store, _INGREDIENTS, store.simulation.config, random_seed=1)
    m.alert_sink = sink
    return m


def test_build_inventory_alert_envelope_warning_and_critical():
    from simulator.mqtt.events import build_inventory_alert_envelope
    ts = datetime(2026, 6, 29, 9, 0, 0)
    env = build_inventory_alert_envelope("sim-store-001", _inv_item(5.0), ts)
    assert env["domain"] == "inventory" and env["condition"] == "low_stock"
    assert env["severity"] == "warning"           # low but in stock
    assert env["source_id"] == "tomatoes"
    assert env["reading"]["current_quantity"] == 5.0 and env["reading"]["reorder_threshold"] == 20.0
    assert env["dedup_key"] == "sim-store-001#inventory#tomatoes#low_stock"
    assert env["timestamp"] == "2026-06-29T09:00:00"
    assert build_inventory_alert_envelope("sim-store-001", _inv_item(0.0), ts)["severity"] == "critical"


async def test_inventory_low_stock_emits_once_on_transition_and_rearms():
    sink = _RecordingInvSink()
    m = _inv_module(sink)
    ts = datetime(2026, 6, 29, 9, 0, 0)
    await m.tick(ts, 1.0)                          # above threshold -> no alert
    assert sink.calls == []
    m.deduct("tomatoes", 90.0, sim_time=ts)        # 100 -> 10 (< 20)
    await m.tick(ts, 1.0)
    assert sink.calls == [("tomatoes", 10.0)]      # one alert on the transition
    await m.tick(ts, 1.0)
    assert sink.calls == [("tomatoes", 10.0)]      # still low -> no re-emit
    item = m._items_map["tomatoes"]
    item.current_quantity, item.needs_reorder = 50.0, False
    await m.tick(ts, 1.0)
    assert "tomatoes" not in m._low_alerted        # recovered -> re-armed


async def test_inventory_low_stock_noop_without_sink():
    m = _inv_module(sink=None)
    ts = datetime(2026, 6, 29, 9, 0, 0)
    m.deduct("tomatoes", 90.0, sim_time=ts)
    await m.tick(ts, 1.0)                           # must not raise without a sink
