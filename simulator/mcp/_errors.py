# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared error envelope helpers for MCP tool functions.

Every MCP tool returns a JSON-serialisable dict. Successes return domain data
directly; failures return ``{"error": <message>, "code": <machine_code>}`` per
the standard error envelope defined in the spec 001 design document.
"""

from typing import Literal

ErrorCode = Literal[
    "not_found",
    "invalid_state",
    "invalid_input",
    "internal_error",
]


def error_envelope(message: str, code: ErrorCode) -> dict:
    """Return a structured error envelope for a failed tool invocation."""
    return {"error": message, "code": code}


def not_found(message: str) -> dict:
    return error_envelope(message, "not_found")


def invalid_state(message: str) -> dict:
    return error_envelope(message, "invalid_state")


def invalid_input(message: str) -> dict:
    return error_envelope(message, "invalid_input")


def internal_error(message: str) -> dict:
    return error_envelope(message, "internal_error")
