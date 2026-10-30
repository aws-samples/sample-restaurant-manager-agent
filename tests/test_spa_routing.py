# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SPA fallback routing tests.

Regression guard for a bug found during the spec-030 deploy (2026-07-31): the
single-segment ``/{filename}`` route was registered BEFORE the ``/{full_path:path}``
catch-all and raised 404 for anything not in ``_DIST_ROOT_FILES``. Because FastAPI
matches routes in registration order, that shadowed the catch-all for every
single-segment path -- so a hard refresh or a deep link to ``/chat``,
``/voice`` or ``/architecture`` returned **404**.

It went unnoticed because navigating from ``/`` is handled client-side by
react-router and never reaches the server, and because `vite preview` (used in
local dev) has its own SPA fallback that masks the difference.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simulator.api.app import create_app
from simulator.engine.clock import VirtualClock
from simulator.engine.engine import SimulationEngine
from simulator.models.simulation import ClockState, SimulationConfig
from simulator.state.store import StateStore

_SIM_TIME = datetime(2026, 7, 31, 9, 0, 0)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with a minimal built-frontend directory on disk."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    # Real service worker: the 2026-08-05 bug served index.html for this URL.
    (dist / "sw.js").write_text("self.addEventListener('push', () => {})", encoding="utf-8")
    # Stands in for any future frontend/public/ file: existence alone must be
    # enough to have it served as itself.
    (dist / "extra-root-file.txt").write_text("root file", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("// bundle", encoding="utf-8")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(dist))

    config = SimulationConfig(peak_hours=[[11, 13], [17, 20]])
    clock = VirtualClock(start_time=_SIM_TIME, speed_multiplier=1.0)
    store = StateStore(
        clock_state=ClockState(current_time=_SIM_TIME, speed_multiplier=1.0, is_paused=False),
        config=config,
    )
    engine = SimulationEngine(clock, store, config)
    return TestClient(create_app(store, engine, clock))


SPA_ROUTES = ["/chat", "/architecture", "/voice"]


@pytest.mark.parametrize("route", SPA_ROUTES)
def test_client_side_routes_serve_the_spa(client: TestClient, route: str) -> None:
    """A direct hit on a client-side route returns index.html, not 404.

    This is what a hard refresh, a bookmark, or a deep link does.
    """
    response = client.get(route)
    assert response.status_code == 200, f"{route} must serve the SPA, got {response.status_code}"
    assert "<title>spa</title>" in response.text


def test_deep_link_with_query_string_serves_the_spa(client: TestClient) -> None:
    """spec 030 uses ``/architecture?flow=<id>`` for shareable/screenshot links."""
    response = client.get("/architecture?flow=voice-edge")
    assert response.status_code == 200
    assert "<title>spa</title>" in response.text


def test_nested_client_side_route_serves_the_spa(client: TestClient) -> None:
    """Multi-segment routes hit the catch-all and must also work."""
    response = client.get("/some/nested/route")
    assert response.status_code == 200
    assert "<title>spa</title>" in response.text


def test_root_serves_the_spa(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>spa</title>" in response.text


def test_real_dist_root_file_is_still_served_as_itself(client: TestClient) -> None:
    """The SPA fallback must not swallow genuine files at the dist root."""
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert "<svg/>" in response.text


def test_bundled_assets_are_still_served(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "// bundle" in response.text


def test_service_worker_is_served_as_javascript(client: TestClient) -> None:
    """``/sw.js`` must be real JavaScript, not the SPA fallback.

    Regression guard for the 2026-08-05 Web Push bug: ``sw.js`` shipped in the
    build but was missing from the dist-root allowlist, so the request fell
    through to the SPA catch-all and returned ``index.html``. The browser then
    refused registration with "The script has an unsupported MIME type
    ('text/html')", so push alerts could never be enabled.
    """
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    ctype = resp.headers["content-type"]
    assert "html" not in ctype, f"sw.js served as {ctype!r} - the SPA fallback shadowed it"
    assert "javascript" in ctype, f"expected a JavaScript content-type, got {ctype!r}"
    assert "<!doctype html" not in resp.text.lower()


def test_any_dist_root_file_is_served_as_itself(client: TestClient) -> None:
    """Existence decides, not an allowlist - so a new public/ file cannot regress.

    The allowlist was the defect: adding a file to ``frontend/public/`` silently
    served HTML for it until someone remembered to update a tuple in the backend.
    """
    resp = client.get("/extra-root-file.txt")
    assert resp.status_code == 200
    assert resp.text == "root file"
    assert "html" not in resp.headers["content-type"]


@pytest.mark.parametrize(
    "path",
    ["/../conftest.py", "/..%2fconftest.py", "/%2e%2e%2fconftest.py"],
)
def test_dist_root_route_does_not_escape_the_dist_directory(
    client: TestClient, path: str
) -> None:
    """Dropping the allowlist must not become an arbitrary-file-read.

    ``_spa_root_file`` interpolates the URL segment into a filesystem path, so it
    confines the result to files directly inside the dist root. A traversal
    attempt must fall through to the SPA, never return file contents.
    """
    resp = client.get(path)
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        # The SPA fallback is the only acceptable 200 here.
        assert "<!doctype html" in resp.text.lower()
        assert "import" not in resp.text  # not a Python source file


def test_client_side_routes_still_fall_through_to_the_spa(client: TestClient) -> None:
    """A single-segment path with no matching file is still a client-side route."""
    resp = client.get("/voice")
    assert resp.status_code == 200
    assert "<!doctype html" in resp.text.lower()


def test_api_routes_are_not_shadowed_by_the_spa_fallback(client: TestClient) -> None:
    """``/api/...`` must keep returning JSON, not index.html."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
