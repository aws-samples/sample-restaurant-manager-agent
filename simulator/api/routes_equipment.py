# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Equipment API routes."""

from fastapi import APIRouter, HTTPException, Request

from simulator.models.simulation import SetTimerRequest

router = APIRouter(tags=["equipment"])


@router.get("/equipment/status")
async def get_equipment_status(request: Request):
    state = await request.app.state.store.get_domain_state("equipment")
    return {"equipment": state["equipment"]}


@router.get("/equipment/{equipment_id}/log")
async def get_equipment_log(equipment_id: str, request: Request):
    state = await request.app.state.store.get_domain_state("equipment")
    history = state["temp_history"].get(equipment_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return {"equipment_id": equipment_id, "readings": history}


@router.post("/equipment/{equipment_id}/timer")
async def set_timer(equipment_id: str, body: SetTimerRequest, request: Request):
    try:
        request.app.state.engine.equipment.set_timer(equipment_id, body.minutes)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"success": True}
