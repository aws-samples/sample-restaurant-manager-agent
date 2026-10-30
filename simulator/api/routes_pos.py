# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""POS API routes."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["pos"])


@router.get("/pos/orders")
async def get_orders(request: Request):
    state = await request.app.state.store.get_domain_state("pos")
    return {"orders": state["orders"]}


@router.get("/pos/sales/daily")
async def get_daily_sales(request: Request):
    state = await request.app.state.store.get_domain_state("pos")
    return {"daily_sales": state["daily_sales"]}


@router.get("/pos/transactions")
async def get_transactions(request: Request):
    state = await request.app.state.store.get_domain_state("pos")
    return {"orders": state["orders"]}
