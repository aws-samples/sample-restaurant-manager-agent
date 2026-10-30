# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inventory API routes."""

from fastapi import APIRouter, HTTPException, Request

from simulator.models.simulation import LogWasteRequest

router = APIRouter(tags=["inventory"])


@router.get("/inventory/levels")
async def get_inventory_levels(request: Request):
    state = await request.app.state.store.get_domain_state("inventory")
    return {"items": state["items"]}


@router.post("/inventory/waste")
async def log_waste(body: LogWasteRequest, request: Request):
    try:
        engine = request.app.state.engine
        engine.inventory.log_waste(body.item_id, body.quantity, body.reason, engine.clock.current_time)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}
