# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB helpers for the IoT response handler.

Descended from the spec-011 ``mqtt_response_subscriber/ddb_ops.py``. Two of the
three original responsibilities remain -- the ``CommandResults`` resolve/terminal
write and the monotonic ``StoreStateCache`` conditional write-through -- and both
are governed by spec 006 R5.2/R5.3.

The third, resuming a durable execution via ``SendDurableExecutionCallbackSuccess``
(spec 020 R3.1), was **removed 2026-08-07**: the durable agent layer was destroyed
in spec 022 Task 6, so nothing registers a callback. Its IAM grant had already been
removed as dead privilege, and the 31 MB vendored boto3 that existed only to reach
that API (R3.4) went with it -- the surviving code needs only
``boto3.resource("dynamodb")``, which every Lambda runtime ships.

Gotchas preserved: ``Decimal`` -> native on READ (architecture-decisions #4), and
``float`` -> ``Decimal`` on WRITE, without which the resource API rejects any
float-bearing store payload.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any, Final

import boto3

logger = logging.getLogger(__name__)

_COMMAND_RESULTS_TABLE: Final[str] = os.environ.get("COMMAND_RESULTS_TABLE_NAME", "")
_STATE_CACHE_TABLE: Final[str] = os.environ.get("STORE_STATE_CACHE_TABLE_NAME", "")

_ddb = boto3.resource("dynamodb")
_cr_table = _ddb.Table(_COMMAND_RESULTS_TABLE) if _COMMAND_RESULTS_TABLE else None
_cache_table = _ddb.Table(_STATE_CACHE_TABLE) if _STATE_CACHE_TABLE else None


def _convert_decimals(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    return obj


def _to_dynamo_numbers(obj: Any) -> Any:
    """Inverse of :func:`_convert_decimals`: floats -> Decimal, for WRITES.

    The boto3 DynamoDB *resource* API rejects Python ``float`` outright with
    ``TypeError: Float types are not supported. Use Decimal types instead.``
    Store results are full of floats -- ``get_equipment_status`` alone returns
    ``current_temp``, ``target_temp``, and both thresholds as floats -- so writing
    a raw result payload raises.

    This mattered more than a normal type bug because the only caller wrapped the
    write in ``except Exception`` as "best effort", so the failure surfaced as a
    single ``cache_write_error`` log line and the cache simply stayed empty. Same
    shape as the ConsumptionEvent.order_id outage in architecture-decisions: a
    construction/serialization error inside a caught block silently disables a
    write path while the service looks healthy.

    ``Decimal(str(v))`` rather than ``Decimal(v)`` to avoid dragging binary
    float noise (``0.1`` -> ``0.1000000000000000055511151231257827``) into the item.
    """
    if isinstance(obj, bool):  # bool is an int subclass; keep it a BOOL attribute
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dynamo_numbers(v) for v in obj]
    return obj


def get_command_result_item(correlation_id: str) -> dict[str, Any] | None:
    if _cr_table is None:
        raise RuntimeError("COMMAND_RESULTS_TABLE_NAME is not set")
    item = _cr_table.get_item(Key={"correlation_id": correlation_id}).get("Item")
    return _convert_decimals(item) if item is not None else None


def update_command_result_terminal(
    *, correlation_id: str, status: str, result: Any,
    duration_ms: Any = None, completed_at: int,
) -> bool:
    """Write the terminal outcome onto the ``CommandResults`` row (spec 006 R5.2).

    R5.2 requires the handler to set ``status`` (a value distinguishable from the
    gateway's ``pending``), ``result``, ``completed_at``, and ``duration_ms``. This
    is the pollable side of Correlation B: without it a caller has no way to learn
    the outcome, and the row just expires on its 300 s TTL.

    ``success`` is canonicalized to ``complete`` per R5.2's parenthetical; any other
    incoming status (notably ``error``) is written through unchanged so a failure is
    distinguishable from a success.

    Idempotent (R5.5): a plain ``SET`` of the same values on redelivery is a no-op
    in effect, and the condition below only guards against *regressing* a row that
    some other writer already completed -- re-completing with the same payload is
    explicitly permitted, so a blocked write is logged and reported, not raised.

    Returns True when the row was written, False when the conditional check
    blocked it (already terminal at a later time).
    """
    if _cr_table is None:
        raise RuntimeError("COMMAND_RESULTS_TABLE_NAME is not set")

    canonical = "complete" if status == "success" else status
    values: dict[str, Any] = {
        ":s": canonical,
        ":r": _to_dynamo_numbers(result),
        ":c": completed_at,
    }
    expression = "SET #st = :s, #res = :r, completed_at = :c"
    if duration_ms is not None:
        expression += ", duration_ms = :d"
        values[":d"] = _to_dynamo_numbers(duration_ms)

    try:
        _cr_table.update_item(
            Key={"correlation_id": correlation_id},
            UpdateExpression=expression,
            # Only write onto a row that exists (the gateway wrote it) and do not
            # move a completed row backwards in time under out-of-order redelivery.
            ConditionExpression=(
                "attribute_exists(correlation_id) AND "
                "(attribute_not_exists(completed_at) OR completed_at <= :c)"
            ),
            # `status` and `result` are DynamoDB reserved words.
            ExpressionAttributeNames={"#st": "status", "#res": "result"},
            ExpressionAttributeValues=values,
        )
        return True
    except _cr_table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "event=command_result_write_blocked | cid=%s | reason=missing_or_newer_terminal",
            correlation_id,
        )
        return False


def update_state_cache(
    *, store_id: str, domain: str, tool_name: str, result: Any,
    correlation_id: str, observed_at_ms: int,
) -> None:
    if _cache_table is None:
        raise RuntimeError("STORE_STATE_CACHE_TABLE_NAME is not set")
    try:
        _cache_table.update_item(
            Key={"store_domain_key": f"{store_id}#{domain}"},
            UpdateExpression=(
                "SET last_known_state = :s, last_updated_at_ms = :t, "
                "last_updated_at = :ts, last_correlation_id = :c, tool_name = :tn"
            ),
            ConditionExpression=(
                "attribute_not_exists(last_updated_at_ms) OR last_updated_at_ms < :t"
            ),
            ExpressionAttributeValues={
                # _to_dynamo_numbers: the result payload carries floats (equipment
                # temperatures, labor cost), which the resource API rejects. Before
                # this conversion every cache write for a float-bearing read tool
                # raised TypeError into the caller's best-effort `except`, so the
                # cache silently never filled. See the docstring on the converter.
                ":s": _to_dynamo_numbers(result), ":t": observed_at_ms,
                ":ts": observed_at_ms // 1000,
                ":c": correlation_id, ":tn": tool_name,
            },
        )
    except _cache_table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("event=cache_stale_write_blocked | key=%s#%s | cid=%s",
                    store_id, domain, correlation_id)
