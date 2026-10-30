# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for store-side proactive-alert delivery (spec 023 Task 6).

Covers the lenient inbox model, the StateStore inbox (append + emit + bounded),
and the MQTT bridge's topic routing (notifications -> inbox handler; commands ->
dispatcher; malformed notification dropped).
"""
from datetime import datetime

from simulator.models.alerts import AlertNotification
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.mqtt.bridge import MQTTBridge
from simulator.mqtt.config import MQTTConnectionConfig
from simulator.state.store import StateStore

_SIM_TIME = datetime(2026, 6, 24, 15, 0, 0)
_NOTIF = {
    "alert_id": "a1", "store_id": "sim-store-001", "severity": "critical",
    "domain": "equipment", "source_id": "fryer-1", "condition": "compressor_fault",
    "message": "Critical alert: compressor_fault on fryer-1",
    "reading": {"current_temp": 410.0}, "timestamp": "2026-06-24T15:00:00Z",
    "extra_field": "should be ignored",
}


def _store() -> StateStore:
    return StateStore(
        clock_state=ClockState(current_time=_SIM_TIME, speed_multiplier=1.0, is_paused=False),
        config=SimulationConfig(peak_hours=[[11, 13], [17, 20]]),
    )


def _cfg() -> MQTTConnectionConfig:
    return MQTTConnectionConfig(enabled=False, store_id="sim-store-001")


def test_alert_notification_ignores_extra_fields():
    n = AlertNotification(**_NOTIF)
    assert n.alert_id == "a1" and n.severity == "critical"
    assert n.message.startswith("Critical alert")
    assert not hasattr(n, "extra_field")  # extra=ignore
    assert n.acknowledged is False


async def test_add_alert_notification_appends_and_emits():
    store = _store()
    events = []

    async def _rec(event):
        events.append(event)

    store.subscribe("alerts", _rec)
    await store.add_alert_notification(_NOTIF, sim_time=_SIM_TIME)

    assert len(store.alerts.entries) == 1
    assert store.alerts.entries[0].alert_id == "a1"
    assert len(events) == 1
    assert events[0].domain == "alerts" and events[0].mutation_type == "alert_received"
    assert len(events[0].data["entries"]) == 1  # serialized for the WebSocket push


async def test_inbox_is_bounded_newest_kept():
    store = _store()
    for i in range(5):
        await store.add_alert_notification({**_NOTIF, "alert_id": f"a{i}"}, sim_time=_SIM_TIME, max_entries=3)
    assert [e.alert_id for e in store.alerts.entries] == ["a2", "a3", "a4"]


async def test_alerts_in_snapshot():
    store = _store()
    await store.add_alert_notification(_NOTIF, sim_time=_SIM_TIME)
    snap = await store.get_snapshot()
    assert "alerts" in snap and len(snap["alerts"]["entries"]) == 1


async def test_route_message_notification_goes_to_handler():
    received = []

    async def _handler(n):
        received.append(n)

    bridge = MQTTBridge(_cfg(), tool_registry={}, notification_handler=_handler)
    await bridge._route_message(
        "stores/sim-store-001/notifications/alert", b'{"alert_id":"a1","message":"x"}'
    )
    assert received == [{"alert_id": "a1", "message": "x"}]


async def test_route_message_command_goes_to_dispatcher():
    class _FakeDispatcher:
        def __init__(self):
            self.calls = []

        async def on_message(self, topic, payload):
            self.calls.append((topic, payload))

    bridge = MQTTBridge(_cfg(), tool_registry={})
    fake = _FakeDispatcher()
    bridge._dispatcher = fake
    await bridge._route_message("stores/sim-store-001/commands/equipment", b'{"correlation_id":"c1"}')
    assert fake.calls == [("stores/sim-store-001/commands/equipment", b'{"correlation_id":"c1"}')]


async def test_route_message_drops_non_json_notification():
    received = []

    async def _handler(n):
        received.append(n)

    bridge = MQTTBridge(_cfg(), tool_registry={}, notification_handler=_handler)
    await bridge._route_message("stores/sim-store-001/notifications/alert", b"not-json")
    assert received == []  # dropped, no raise


async def test_acknowledge_alert_entry_marks_and_emits():
    store = _store()
    await store.add_alert_notification({**_NOTIF, "alert_id": "a1"}, sim_time=_SIM_TIME)
    events = []

    async def _rec(event):
        events.append(event)

    store.subscribe("alerts", _rec)
    found = await store.acknowledge_alert_entry("a1", sim_time=_SIM_TIME)
    assert found is True
    assert store.alerts.entries[-1].acknowledged is True
    assert events and events[-1].mutation_type == "alert_acknowledged"


async def test_acknowledge_unknown_alert_returns_false():
    store = _store()
    assert await store.acknowledge_alert_entry("nope", sim_time=_SIM_TIME) is False


async def test_acknowledge_proactive_alert_marks_and_publishes():
    store = _store()
    published = []

    async def _pub(alert_id):
        published.append(alert_id)

    store.alert_ack_publisher = _pub
    await store.add_alert_notification({**_NOTIF, "alert_id": "a1"}, sim_time=_SIM_TIME)
    result = await store.acknowledge_proactive_alert("a1", sim_time=_SIM_TIME)
    assert result == {"alert_id": "a1", "acknowledged": True, "ack_published": True}
    assert published == ["a1"]
    assert store.alerts.entries[-1].acknowledged is True


async def test_acknowledge_proactive_alert_without_publisher():
    store = _store()  # alert_ack_publisher not wired (e.g. MQTT disabled)
    await store.add_alert_notification({**_NOTIF, "alert_id": "a1"}, sim_time=_SIM_TIME)
    result = await store.acknowledge_proactive_alert("a1", sim_time=_SIM_TIME)
    assert result == {"alert_id": "a1", "acknowledged": True, "ack_published": False}


async def test_acknowledge_proactive_alert_unknown_still_publishes():
    # The SFN resume must still fire even if this store instance's in-memory
    # inbox no longer has the entry (idempotent ack handler no-ops a dead token).
    store = _store()
    published = []

    async def _pub(alert_id):
        published.append(alert_id)

    store.alert_ack_publisher = _pub
    result = await store.acknowledge_proactive_alert("nope", sim_time=_SIM_TIME)
    assert result == {"alert_id": "nope", "acknowledged": False, "ack_published": True}
    assert published == ["nope"]


async def test_proactive_ack_tool_in_registry_and_routes():
    from simulator.mcp.registry import DOMAIN_OF_TOOL, build_tool_registry

    assert DOMAIN_OF_TOOL.get("acknowledge_proactive_alert") == "equipment"
    store = _store()
    published = []

    async def _pub(alert_id):
        published.append(alert_id)

    store.alert_ack_publisher = _pub
    await store.add_alert_notification({**_NOTIF, "alert_id": "a1"}, sim_time=_SIM_TIME)
    reg = build_tool_registry(store)
    assert "acknowledge_proactive_alert" in reg
    result = await reg["acknowledge_proactive_alert"](alert_id="a1")
    assert result["acknowledged"] is True and result["ack_published"] is True
    assert published == ["a1"]


async def test_list_active_alerts_returns_unacked_newest_first():
    store = _store()
    for aid in ("a1", "a2", "a3"):
        await store.add_alert_notification({**_NOTIF, "alert_id": aid}, sim_time=_SIM_TIME)
    await store.acknowledge_alert_entry("a2", sim_time=_SIM_TIME)
    active = await store.list_active_alerts()
    assert [a["alert_id"] for a in active] == ["a3", "a1"]  # newest first; a2 acked -> excluded
    assert active[0]["severity"] == "critical" and active[0]["source_id"] == "fryer-1"


async def test_get_active_alerts_tool_in_registry_and_routes():
    from simulator.mcp.registry import DOMAIN_OF_TOOL, build_tool_registry

    assert DOMAIN_OF_TOOL.get("get_active_alerts") == "equipment"
    store = _store()
    await store.add_alert_notification({**_NOTIF, "alert_id": "a1"}, sim_time=_SIM_TIME)
    reg = build_tool_registry(store)
    assert "get_active_alerts" in reg
    result = await reg["get_active_alerts"]()
    assert result["count"] == 1
    assert result["active_alerts"][0]["alert_id"] == "a1"
    assert result["active_alerts"][0]["condition"] == "compressor_fault"


def test_get_active_alerts_offered_by_broker_and_inline_tools():
    from simulator.api.harness_tools import INLINE_TOOLS
    from simulator.voice import broker

    assert "get_active_alerts" in {t["name"] for t in INLINE_TOOLS}
    assert "get_active_alerts" in {t["toolSpec"]["name"] for t in broker._sonic_tools()}
