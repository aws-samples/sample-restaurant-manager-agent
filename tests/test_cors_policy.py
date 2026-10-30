# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CORS policy regression guard.

A security scan (2026-09-13) flagged ``simulator/api/app.py`` for
``allow_origins=["*"]``: any site on the internet could read this API's
responses cross-origin.

The fix was to remove CORS entirely rather than narrow it, because **no
environment needs it**:

* Deployed, FastAPI serves the SPA itself, so the dashboard is same-origin.
* Locally, the Vite dev server proxies ``/api`` and ``/ws`` to this process
  (``frontend/vite.config.js``), so the browser sees same-origin there too.

These tests pin the three properties that matter, rather than asserting one
literal configuration:

1. By default no ``access-control-allow-origin`` header is emitted at all.
2. An explicit origin list is honoured when set.
3. ``*`` in the list is REJECTED at startup. This is the load-bearing one --
   without it, the safe state depends on an operator remembering not to type a
   wildcard, and the original finding walks straight back in.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from simulator.api.app import _CORS_ORIGINS_ENV, _parse_cors_origins, create_app
from simulator.engine.clock import VirtualClock
from simulator.engine.engine import SimulationEngine
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.state.store import StateStore

_SIM_TIME = datetime(2026, 9, 13, 9, 0, 0)
_ORIGIN = "https://dashboard.example"


def _client() -> TestClient:
    config = SimulationConfig(peak_hours=[[11, 13], [17, 20]])
    clock = VirtualClock(start_time=_SIM_TIME, speed_multiplier=1.0)
    store = StateStore(
        clock_state=ClockState(current_time=_SIM_TIME, speed_multiplier=1.0, is_paused=False),
        config=config,
    )
    engine = SimulationEngine(clock, store, config)
    return TestClient(create_app(store, engine, clock))


# --- the parser -------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", ",", " , "])
def test_cors_disabled_by_default(raw: str | None) -> None:
    """Unset / blank means no CORS, not wildcard CORS."""
    assert _parse_cors_origins(raw) == []


def test_cors_parses_explicit_origin_list() -> None:
    assert _parse_cors_origins("https://a.example, https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


@pytest.mark.parametrize("raw", ["*", "https://a.example,*", "*,https://a.example"])
def test_wildcard_origin_is_rejected(raw: str) -> None:
    """The wildcard must be impossible to configure, not merely discouraged.

    This is the finding itself, expressed as a test: if this ever passes a '*'
    through, the API is world-readable cross-origin again.
    """
    with pytest.raises(ValueError, match=r"\*.*not allowed"):
        _parse_cors_origins(raw)


# --- the wired app ----------------------------------------------------------


def test_no_cors_header_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-origin request gets no allow-origin header, so the browser blocks it."""
    monkeypatch.delenv(_CORS_ORIGINS_ENV, raising=False)
    response = _client().get("/api/health", headers={"Origin": _ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_configured_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CORS_ORIGINS_ENV, _ORIGIN)
    response = _client().get("/api/health", headers={"Origin": _ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ORIGIN


def test_unconfigured_origin_is_not_reflected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allow-list must not echo back arbitrary origins."""
    monkeypatch.setenv(_CORS_ORIGINS_ENV, _ORIGIN)
    response = _client().get("/api/health", headers={"Origin": "https://evil.example"})

    assert response.headers.get("access-control-allow-origin") != "https://evil.example"


def test_app_startup_fails_on_wildcard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loud at construction -- never silently downgrade to wildcard CORS."""
    monkeypatch.setenv(_CORS_ORIGINS_ENV, "*")
    with pytest.raises(ValueError):
        _client()
