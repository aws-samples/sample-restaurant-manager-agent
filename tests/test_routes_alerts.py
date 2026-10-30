# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route-level tests for the alerts API (``simulator/api/routes_alerts.py``).

Focus: the DEV-ONLY ``/alerts/_dev_seed`` helper is gated OFF by default and only
active when the dedicated ``DEV_SEED_ALERTS_ENABLED`` flag is set -- deliberately
decoupled from ``VOICE_ENABLED`` (on by default in the deployed simulator)
so it stays unreachable in normal/deployed environments.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from simulator.api.routes_alerts import router as alerts_router
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.state.store import StateStore

_SIM_TIME = datetime(2026, 7, 7, 15, 0, 0)


class _Clock:
    current_time = _SIM_TIME


def _client() -> tuple[TestClient, StateStore]:
    store = StateStore(
        clock_state=ClockState(current_time=_SIM_TIME, speed_multiplier=1.0, is_paused=False),
        config=SimulationConfig(peak_hours=[[11, 13], [17, 20]]),
    )
    app = FastAPI()
    app.include_router(alerts_router, prefix="/api")
    app.state.store = store
    app.state.clock = _Clock()
    return TestClient(app), store


def test_dev_seed_404_when_flag_unset(monkeypatch):
    """Unset dev flag -> 404, even with the voice flag ON (proves decoupling)."""
    monkeypatch.delenv("DEV_SEED_ALERTS_ENABLED", raising=False)
    monkeypatch.setenv("VOICE_ENABLED", "true")
    client, store = _client()
    resp = client.post("/api/alerts/_dev_seed")
    assert resp.status_code == 404
    assert store.alerts.entries == []  # nothing was seeded


def test_dev_seed_404_when_flag_falsey(monkeypatch):
    monkeypatch.setenv("DEV_SEED_ALERTS_ENABLED", "0")
    client, store = _client()
    assert client.post("/api/alerts/_dev_seed").status_code == 404
    assert store.alerts.entries == []


def test_dev_seed_seeds_when_flag_set(monkeypatch):
    """Explicit dev flag -> seeds one critical equipment alert into the inbox."""
    monkeypatch.setenv("DEV_SEED_ALERTS_ENABLED", "1")
    client, store = _client()
    resp = client.post("/api/alerts/_dev_seed")
    assert resp.status_code == 200
    seeded = resp.json()["seeded"]
    assert seeded["domain"] == "equipment" and seeded["severity"] == "critical"
    assert len(store.alerts.entries) == 1
    assert store.alerts.entries[0].alert_id == seeded["alert_id"]
