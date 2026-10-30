# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""UUID generation and ``CommandResults`` item shape helpers.

This module centralises the correlation-ID format and the DynamoDB item
shape for the ``CommandResults`` table so the gateway Lambdas (task 4)
and the response handler (task 5) share a single definition.

Requirements covered:
    R4.4  — UUID v4 correlation ID, pending record shape, ``expires_at``.
    R4.9  — ``status="failed"`` with ``error_message`` on publish failure.
    R5.2  — terminal-status update shape (``complete`` / ``error``).
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Final

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

#: Default TTL offset in seconds added to ``created_at`` to produce
#: ``expires_at``. R3.1 / R4.4 specify 300 s (5 minutes).
DEFAULT_TTL_OFFSET_S: Final[int] = 300

#: Canonical status values used in ``CommandResults`` items.
STATUS_PENDING: Final[str] = "pending"
STATUS_COMPLETE: Final[str] = "complete"
STATUS_ERROR: Final[str] = "error"
STATUS_FAILED: Final[str] = "failed"


# -----------------------------------------------------------------------
# ID generation
# -----------------------------------------------------------------------


def new_correlation_id() -> str:
    """Generate a new UUID v4 correlation ID as a lowercase hex string.

    The returned value is used as the ``CommandResults`` partition key
    and as the ``correlation_id`` field in the MQTT command payload.
    """
    return str(uuid.uuid4())


# -----------------------------------------------------------------------
# Item builders
# -----------------------------------------------------------------------


def build_pending_item(
    correlation_id: str,
    tool_name: str,
    store_id: str,
    domain: str,
    parameters: dict[str, Any] | None = None,
    ttl_offset_s: int = DEFAULT_TTL_OFFSET_S,
) -> dict[str, Any]:
    """Build a ``CommandResults`` item in the ``pending`` state.

    This is the shape the gateway Lambda writes via ``PutItem`` before
    publishing the MQTT command (R4.4).

    Parameters
    ----------
    correlation_id:
        UUID v4 string — the partition key.
    tool_name:
        MCP tool name (e.g. ``"get_equipment_status"``).
    store_id:
        Target store identifier (e.g. ``"sim-store-001"``).
    domain:
        Domain name (``"pos"``, ``"equipment"``, ``"inventory"``,
        ``"schedule"``).
    parameters:
        Tool invocation parameters forwarded from the MCP caller.
        Defaults to an empty dict.
    ttl_offset_s:
        Seconds from now until the record expires. Defaults to 300 s.

    Returns
    -------
    dict
        A dict ready to be passed to ``ddb.put_pending_command``.
    """
    now = int(time.time())
    return {
        "correlation_id": correlation_id,
        "tool_name": tool_name,
        "store_id": store_id,
        "domain": domain,
        "parameters": parameters if parameters is not None else {},
        "status": STATUS_PENDING,
        "created_at": now,
        "expires_at": now + ttl_offset_s,
    }


def build_mqtt_payload(
    correlation_id: str,
    tool_name: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload published to the MQTT command topic.

    Shape: ``{correlation_id, tool_name, parameters, timestamp}``.
    """
    return {
        "correlation_id": correlation_id,
        "tool_name": tool_name,
        "parameters": parameters if parameters is not None else {},
        "timestamp": int(time.time()),
    }


def build_complete_update(
    result: Any,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Build the attribute-value map for an ``UpdateItem`` that marks a
    ``CommandResults`` record as ``complete`` (R5.2).

    The response handler calls this after parsing a successful MQTT
    response.
    """
    update: dict[str, Any] = {
        "status": STATUS_COMPLETE,
        "result": result,
        "completed_at": int(time.time()),
    }
    if duration_ms is not None:
        update["duration_ms"] = duration_ms
    return update


def build_error_update(
    error_message: str,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Build the attribute-value map for an ``UpdateItem`` that marks a
    ``CommandResults`` record as ``error`` (R5.2).

    Used when the MQTT response carries ``status="error"``.
    """
    update: dict[str, Any] = {
        "status": STATUS_ERROR,
        "error_message": error_message,
        "completed_at": int(time.time()),
    }
    if duration_ms is not None:
        update["duration_ms"] = duration_ms
    return update


def build_failed_update(error_message: str) -> dict[str, Any]:
    """Build the attribute-value map for an ``UpdateItem`` that marks a
    ``CommandResults`` record as ``failed`` (R4.9).

    Used by the gateway Lambda when MQTT PUBLISH fails after the
    ``PutItem`` has already written the pending record.
    """
    return {
        "status": STATUS_FAILED,
        "error_message": error_message,
        "completed_at": int(time.time()),
    }
