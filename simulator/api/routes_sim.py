# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simulation control API routes."""

from fastapi import APIRouter, HTTPException, Request

from simulator.models.simulation import InjectEventRequest, SimControlRequest

router = APIRouter(tags=["simulation"])


@router.post("/sim/control")
async def sim_control(body: SimControlRequest, request: Request):
    clock = request.app.state.clock
    store = request.app.state.store
    if body.action == "pause":
        clock.pause()
    elif body.action == "resume":
        clock.resume()
    elif body.action == "reset":
        clock.pause()
    else:
        raise HTTPException(status_code=400, detail="action must be 'pause', 'resume', or 'reset'")
    if body.speed is not None:
        try:
            clock.set_speed(body.speed)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Broadcast the new clock state immediately so WebSocket clients (and the
    # dashboard Pause/Resume button, which renders from
    # ``state.simulation.clock.is_paused``) reflect the change right away.
    # Without this, ``pause`` stops the tick loop and the engine's per-tick
    # "simulation" update is the only other place clock state is emitted, so the
    # button's visual state would never update after a pause. Mirrors the
    # engine tick's ``store.update("simulation", ...)`` shape so the frontend
    # merges it identically.
    new_clock = clock.get_state()
    await store.update(
        "simulation", "control", {"clock": new_clock}, sim_time=clock.current_time
    )
    return {"success": True}


@router.post("/sim/inject-event")
async def inject_event(body: InjectEventRequest, request: Request):
    result = await request.app.state.engine.inject_event(body.type, body.params)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/sim/state")
async def get_sim_state(request: Request):
    return await request.app.state.store.get_snapshot()


@router.get("/health")
async def health():
    return {"status": "ok"}
