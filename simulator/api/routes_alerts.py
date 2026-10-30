# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Alerts inbox API routes (spec 023 Task 6)."""

import os
import uuid

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["alerts"])

# DEV-ONLY seed endpoint gate: a dedicated, OFF-by-default env flag. Kept
# deliberately separate from VOICE_ENABLED (which is ON by default in the
# deployed simulator) so this state-mutating test helper is unreachable in
# normal/deployed environments -- it only activates when a developer explicitly
# sets DEV_SEED_ALERTS_ENABLED locally.
_DEV_SEED_FLAG = "DEV_SEED_ALERTS_ENABLED"


def _dev_seed_enabled() -> bool:
    return os.environ.get(_DEV_SEED_FLAG, "").strip().lower() in ("1", "true", "yes")


@router.get("/alerts")
async def get_alerts(request: Request):
    """Return the in-app alert inbox (proactive notifications received from the cloud)."""
    state = await request.app.state.store.get_domain_state("alerts")
    return {"alerts": state["entries"]}


@router.post("/alerts/_dev_seed")
async def dev_seed_alert(request: Request):
    """DEV-ONLY local-test helper (spec 025 Task 1b): seed a synthetic proactive
    alert into the inbox so the display's poll forwards it to be spoken.

    Gated by the dedicated, OFF-by-default ``DEV_SEED_ALERTS_ENABLED`` env flag
    (returns 404 when unset). Intentionally decoupled from ``VOICE_ENABLED``
    -- which is on by default in the deployed simulator -- so this endpoint stays
    unreachable in normal/deployed environments. NOT a product endpoint."""
    if not _dev_seed_enabled():
        raise HTTPException(status_code=404)
    store = request.app.state.store
    clock = request.app.state.clock
    notification = {
        "alert_id": f"equipment-freezer_1-thermostat_malfunction-{uuid.uuid4().hex[:8]}",
        "store_id": "sim-store-001",
        "severity": "critical",
        "domain": "equipment",
        "source_id": "freezer_1",
        "condition": "thermostat_malfunction",
        "reading": {"temp_f": 28},
        "message": "Freezer 1 thermostat malfunction (28F)",
        "timestamp": clock.current_time.isoformat(),
    }
    await store.add_alert_notification(notification, sim_time=clock.current_time)
    return {"seeded": notification}


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: str, request: Request):
    """Acknowledge a proactive alert: mark it in the inbox and publish an ack to
    the cloud so the waiting workflow resumes (spec 023 Task 7)."""
    store = request.app.state.store
    clock = request.app.state.clock
    # Shared store-side path (also used by the agent's acknowledge_proactive_alert
    # tool): mark the inbox entry + publish events/ack so the cloud workflow
    # resumes (spec 023 Task 7/11).
    return await store.acknowledge_proactive_alert(alert_id, sim_time=clock.current_time)
