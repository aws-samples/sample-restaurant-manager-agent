# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression test: POST /sim/control emits a clock-state event so WebSocket
clients (and the dashboard Pause/Resume button) update immediately.

Bug: `clock.pause()`/`resume()` only flipped `is_paused` and emitted no state
event; since a pause stops the tick loop, no further "simulation" event was
broadcast and the button's visual state (driven by
`state.simulation.clock.is_paused`) never updated.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from simulator.api.routes_sim import router as sim_router
from simulator.engine.clock import VirtualClock
from simulator.models.simulation import StateEvent
from simulator.state.store import StateStore, load_all_configs
from simulator.models.simulation import SimulationConfig


@pytest.fixture
def client_and_capture():
    configs = load_all_configs("config")
    config = SimulationConfig(**configs["simulation"])
    clock = VirtualClock(datetime(2026, 1, 1, 7, 0, 0), speed_multiplier=10, tick_interval_seconds=1)
    store = StateStore(clock_state=clock.get_state(), config=config)

    events: list[StateEvent] = []

    async def _capture(event: StateEvent) -> None:
        events.append(event)

    store.subscribe(None, _capture)

    app = FastAPI()
    app.include_router(sim_router, prefix="/api")
    app.state.clock = clock
    app.state.store = store
    app.state.engine = None
    return TestClient(app), clock, events


def _clock_events(events):
    return [e for e in events if e.domain == "simulation" and "clock" in e.data]


def test_pause_emits_clock_event_with_is_paused_true(client_and_capture):
    client, clock, events = client_and_capture
    resp = client.post("/api/sim/control", json={"action": "pause"})
    assert resp.status_code == 200
    assert clock.is_paused is True
    ce = _clock_events(events)
    assert ce, "no clock state event emitted on pause"
    assert ce[-1].data["clock"]["is_paused"] is True


def test_resume_emits_clock_event_with_is_paused_false(client_and_capture):
    client, clock, events = client_and_capture
    client.post("/api/sim/control", json={"action": "pause"})
    events.clear()
    resp = client.post("/api/sim/control", json={"action": "resume"})
    assert resp.status_code == 200
    assert clock.is_paused is False
    ce = _clock_events(events)
    assert ce, "no clock state event emitted on resume"
    assert ce[-1].data["clock"]["is_paused"] is False


def test_speed_change_emits_clock_event(client_and_capture):
    client, clock, events = client_and_capture
    events.clear()
    resp = client.post("/api/sim/control", json={"action": "resume", "speed": 30})
    assert resp.status_code == 200
    assert clock.speed_multiplier == 30
    ce = _clock_events(events)
    assert ce and ce[-1].data["clock"]["speed_multiplier"] == 30


def test_invalid_action_returns_400_and_emits_nothing(client_and_capture):
    client, clock, events = client_and_capture
    events.clear()
    resp = client.post("/api/sim/control", json={"action": "bogus"})
    assert resp.status_code == 400
    assert _clock_events(events) == []
