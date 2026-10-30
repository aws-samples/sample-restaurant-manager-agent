# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IoT Topic Rule response handler (spec 020, productionized from 019 spike).

Triggered by the IoT rule on ``stores/+/responses/+``. For each store response:
  1. Resolve correlation_id (envelope, fallback to topic()).
  2. ``CommandResults`` lookup -> tool_name, domain, store_id.
  3. **Terminal write onto the CommandResults row** (spec 006 R5.2) -- status,
     result, completed_at, duration_ms. This is what makes the correlation
     pollable.
  4. Monotonic ``StoreStateCache`` write-through for read tools (spec 006 R5.3),
     which feeds the last-known-state fallback.

History worth knowing, because both bugs were invisible in the logs:

* Steps 3 and 4 used to sit BEHIND a durable-callback check, so once the durable
  agent layer was destroyed (spec 022 Task 6) the handler resolved nothing at all --
  rows aged out on their TTL and the cache never filled, while every transport hop
  looked healthy. Restored to the R5.2/R5.3 contract 2026-08-07.
* The callback resume itself (``SendDurableExecutionCallbackSuccess``, spec 020 R3.1)
  was **removed** later the same day along with the 31 MB vendored boto3 it needed
  (R3.4): with the durable layer gone there was nothing to resume, and a dead branch
  guarding live requirements is what caused the first bug.

Idempotent under IoT-rule / QoS-1 redelivery: the terminal write is a plain SET
guarded against regressing a newer completion, and the cache write is conditional on
a newer timestamp.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ddb_ops import (
    get_command_result_item,
    update_command_result_terminal,
    update_state_cache,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# tool_name -> domain, for the cache write-through when the row omits domain.
_TOOL_TO_DOMAIN = {
    "get_daily_sales": "pos", "get_active_orders": "pos",
    "get_transaction_history": "pos", "void_order": "pos",
    "get_equipment_status": "equipment", "set_timer": "equipment",
    "get_temperature_log": "equipment", "acknowledge_alert": "equipment",
    "get_stock_levels": "inventory", "log_waste": "inventory",
    "get_usage_report": "inventory", "check_reorder_status": "inventory",
    "get_current_shifts": "schedule", "clock_in": "schedule",
    "clock_out": "schedule", "get_labor_cost": "schedule",
}
# Read tools whose results are worth caching for stale-fallback reads.
_CACHEABLE = {
    "get_daily_sales", "get_active_orders", "get_transaction_history",
    "get_equipment_status", "get_temperature_log", "get_stock_levels",
    "get_usage_report", "check_reorder_status", "get_current_shifts", "get_labor_cost",
}


def _correlation_id(event: dict) -> str | None:
    cid = event.get("correlation_id")
    if cid:
        return cid
    parts = event.get("topic", "").split("/")
    if len(parts) == 4 and parts[0] == "stores" and parts[2] == "responses":
        return parts[3]
    return None


def _store_id_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    return parts[1] if len(parts) == 4 and parts[0] == "stores" else None


def handler(event: dict, _context) -> dict:
    cid = _correlation_id(event)
    print(json.dumps({"marker": "iot_response_entry", "correlation_id": cid}))
    if not cid or cid == "_invalid":
        return {"status": "dropped", "reason": "no_correlation"}

    row = get_command_result_item(cid)
    if not row:
        # No row at all: nothing to resolve. Either the correlation expired on its
        # TTL before the store answered, or this is a response to a command that
        # this deployment never issued.
        logger.info("event=unknown_correlation | correlation_id=%s", cid)
        return {"status": "dropped", "reason": "unknown_correlation"}

    tool_name = row.get("tool_name") or event.get("tool_name")
    domain = row.get("domain") or _TOOL_TO_DOMAIN.get(tool_name or "")
    store_id = row.get("store_id") or _store_id_from_topic(event.get("topic", ""))
    result = event.get("result", {})
    status = event.get("status", "success")

    # 1. Terminal write onto CommandResults (spec 006 R5.2). Unconditional, because
    #    this is what makes the correlation POLLABLE -- it must not depend on there
    #    being a durable callback to resume. Before this, the row stayed `pending`
    #    until its 300 s TTL and the outcome was unobservable to any caller.
    written = update_command_result_terminal(
        correlation_id=cid, status=status, result=result,
        duration_ms=event.get("duration_ms"), completed_at=int(time.time()),
    )

    # 2. Cache write-through for read tools (spec 006 R5.3), gated only on the tool
    #    being a read and the call having succeeded. This feeds the last-known-state
    #    fallback.
    cached = False
    if tool_name in _CACHEABLE and store_id and domain and status == "success":
        try:
            update_state_cache(
                store_id=store_id, domain=domain, tool_name=tool_name,
                result=result, correlation_id=cid, observed_at_ms=int(time.time() * 1000),
            )
            cached = True
        except Exception as exc:  # noqa: BLE001 - cache write must not fail resolution
            # Still best-effort, but no longer silent about WHAT failed: the bug this
            # replaced (raw floats rejected by the resource API) stayed invisible
            # precisely because only the exception class was logged.
            logger.warning(
                "event=cache_write_error | cid=%s | exc=%s | msg=%s",
                cid, type(exc).__name__, str(exc)[:200],
            )

    print(json.dumps({
        "marker": "iot_response_exit", "correlation_id": cid,
        "result_written": written, "cached": cached,
    }))
    return {
        "status": "resolved" if written else "not_written",
        "correlation_id": cid,
        "cached": cached,
    }
