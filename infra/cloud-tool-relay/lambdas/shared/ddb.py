# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Typed DynamoDB helpers for ``CommandResults`` and ``StoreStateCache``.

This module provides thin wrappers around ``boto3.resource('dynamodb')``
table operations, keeping the DynamoDB item shapes and update expressions
in one place so the gateway Lambdas (task 4) and the Fargate response
handler (task 5) share a single definition.

Requirements covered:
    R4.4  — ``PutItem`` pending record on ``CommandResults``.
    R4.9  — ``UpdateItem`` to ``status="failed"`` on publish failure.
    R5.2  — ``UpdateItem`` to ``status="complete"`` / ``"error"`` on
            response receipt.
    R5.3  — ``PutItem`` write-through on ``StoreStateCache`` for read
            tools.
    R5.5  — idempotent updates (re-writing ``complete`` → ``complete``
            with the same result is safe).
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Final

import boto3

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

#: Default AWS region for DynamoDB operations.
_DEFAULT_REGION: Final[str] = "us-west-2"


# -----------------------------------------------------------------------
# Float → Decimal conversion for DynamoDB
# -----------------------------------------------------------------------


def _floats_to_decimals(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB.

    The boto3 DynamoDB Table resource (high-level API) does not accept
    Python floats — it requires ``decimal.Decimal``. MQTT response
    payloads from the simulator contain floats (e.g. ``4523.50``), so
    we convert them before writing.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


# -----------------------------------------------------------------------
# Client helpers
# -----------------------------------------------------------------------


def _get_table(table_name: str, region: str = _DEFAULT_REGION) -> Any:
    """Return a ``boto3.resource('dynamodb').Table`` handle.

    Each call creates a fresh resource/table handle. In production code
    the caller should cache the handle (e.g. at module scope in a Lambda)
    to avoid repeated resource creation.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)
    return dynamodb.Table(table_name)


# -----------------------------------------------------------------------
# CommandResults operations
# -----------------------------------------------------------------------


def put_pending_command(
    table_name: str,
    item: dict[str, Any],
    region: str = _DEFAULT_REGION,
) -> None:
    """Write a pending ``CommandResults`` item (R4.4).

    Parameters
    ----------
    table_name:
        Physical name of the ``CommandResults`` table.
    item:
        A dict produced by ``correlation.build_pending_item``.
    region:
        AWS region.
    """
    table = _get_table(table_name, region=region)
    table.put_item(Item=_floats_to_decimals(item))


def update_command_result(
    table_name: str,
    correlation_id: str,
    updates: dict[str, Any],
    region: str = _DEFAULT_REGION,
) -> None:
    """Update a ``CommandResults`` item with the given attribute values.

    Used by:
    - The response handler to mark a record ``complete`` or ``error``
      (R5.2).
    - The gateway Lambda to mark a record ``failed`` on publish failure
      (R4.9).

    The update is idempotent: re-applying the same ``status`` and
    ``result`` is a no-op from a data-correctness standpoint (R5.5).

    Parameters
    ----------
    table_name:
        Physical name of the ``CommandResults`` table.
    correlation_id:
        Partition key value.
    updates:
        A dict of attribute names → values to set. Produced by one of
        ``correlation.build_complete_update``,
        ``correlation.build_error_update``, or
        ``correlation.build_failed_update``.
    region:
        AWS region.
    """
    if not updates:
        return

    table = _get_table(table_name, region=region)

    # Build a SET update expression from the updates dict.
    expr_parts: list[str] = []
    attr_names: dict[str, str] = {}
    attr_values: dict[str, Any] = {}

    for i, (key, value) in enumerate(updates.items()):
        name_placeholder = f"#k{i}"
        value_placeholder = f":v{i}"
        expr_parts.append(f"{name_placeholder} = {value_placeholder}")
        attr_names[name_placeholder] = key
        attr_values[value_placeholder] = _floats_to_decimals(value)

    table.update_item(
        Key={"correlation_id": correlation_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def get_command_result(
    table_name: str,
    correlation_id: str,
    region: str = _DEFAULT_REGION,
) -> dict[str, Any] | None:
    """Fetch a ``CommandResults`` item by correlation ID.

    Returns the item dict if found, or ``None`` if the item does not
    exist. Used by domain agents to poll for completion (R8.2).

    Parameters
    ----------
    table_name:
        Physical name of the ``CommandResults`` table.
    correlation_id:
        Partition key value.
    region:
        AWS region.
    """
    table = _get_table(table_name, region=region)
    response = table.get_item(Key={"correlation_id": correlation_id})
    return response.get("Item")


# -----------------------------------------------------------------------
# StoreStateCache operations
# -----------------------------------------------------------------------


def put_state_cache(
    table_name: str,
    store_id: str,
    domain: str,
    result: Any,
    correlation_id: str,
    tool_name: str,
    region: str = _DEFAULT_REGION,
) -> None:
    """Write-through update to ``StoreStateCache`` (R5.3).

    Called by the response handler for every successful read-tool
    response. The item is keyed by ``{store_id}#{domain}`` (design.md
    D4) and carries the last-known result, timestamp, and the
    correlation ID that produced it.

    This operation is idempotent: re-writing the same item with the
    same values is safe (R5.5).

    Parameters
    ----------
    table_name:
        Physical name of the ``StoreStateCache`` table.
    store_id:
        Store identifier (e.g. ``"sim-store-001"``).
    domain:
        Domain name (``"pos"``, ``"equipment"``, etc.).
    result:
        The result payload from the MQTT response.
    correlation_id:
        The correlation ID that produced this result.
    tool_name:
        The tool name that produced this result.
    region:
        AWS region.
    """
    table = _get_table(table_name, region=region)
    table.put_item(
        Item=_floats_to_decimals({
            "store_domain_key": f"{store_id}#{domain}",
            "tool_name": tool_name,
            "last_known_state": result,
            "last_updated_at": int(time.time()),
            "last_correlation_id": correlation_id,
        })
    )


def get_state_cache(
    table_name: str,
    store_id: str,
    domain: str,
    region: str = _DEFAULT_REGION,
) -> dict[str, Any] | None:
    """Fetch the cached state for a ``(store, domain)`` pair.

    Returns the item dict if found, or ``None`` if no cached state
    exists. Used by domain agents on timeout fallback (R8.2 step 5).

    Parameters
    ----------
    table_name:
        Physical name of the ``StoreStateCache`` table.
    store_id:
        Store identifier.
    domain:
        Domain name.
    region:
        AWS region.
    """
    table = _get_table(table_name, region=region)
    response = table.get_item(
        Key={"store_domain_key": f"{store_id}#{domain}"}
    )
    return response.get("Item")
