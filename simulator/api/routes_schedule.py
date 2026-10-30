# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule API routes."""

from fastapi import APIRouter, HTTPException, Request

from simulator.models.simulation import ClockRequest

router = APIRouter(tags=["schedule"])


@router.get("/schedule/current")
async def get_current_schedule(request: Request):
    state = await request.app.state.store.get_domain_state("schedule")
    return state


@router.post("/schedule/clock")
async def clock_in_out(body: ClockRequest, request: Request):
    engine = request.app.state.engine
    sim_time = engine.clock.current_time
    try:
        if body.action == "in":
            engine.schedule.clock_in(body.employee_id, sim_time)
        elif body.action == "out":
            engine.schedule.clock_out(body.employee_id, sim_time)
        else:
            raise HTTPException(status_code=400, detail="action must be 'in' or 'out'")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}
