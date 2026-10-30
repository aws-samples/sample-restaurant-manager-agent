# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared data models and error types for the chat API.

This module holds the transport-agnostic pieces of the chat feature: the
Pydantic request/response models, the ``ChatInvokeResult`` returned by a
backend's ``invoke``, and ``ChatServiceError`` with its ``error_kind``
classification that the route layer maps to HTTP status codes.

The single chat backend is :class:`simulator.api.harness_backend.HarnessChatBackend`
(AgentCore Managed Harness). The durable-supervisor ``ChatService`` class that
previously lived here was removed with the durable agent layer in spec 022
Task 6 -- see ADR-003 for the agent-runtime decision and ADR-001 for the
retired durable-Lambda approach. Its history is in git.

The module keeps its ``chat_service`` name so the six importing modules and
test files were not churned by the removal; it is now a models module rather
than a service module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Incoming chat request from the frontend."""

    message: str = Field(..., min_length=1, description="User message text")
    session_id: str | None = Field(
        None, description="Session ID for conversation continuity"
    )


class HistoryMessage(BaseModel):
    """A single message in a conversation history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str  # ISO 8601


class ChatHistoryResponse(BaseModel):
    """Response for the chat history endpoint."""

    session_id: str
    messages: list[HistoryMessage]


class ChatErrorResponse(BaseModel):
    """Error response body."""

    error: str


# ---------------------------------------------------------------------------
# Backend-level result and exception types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatInvokeResult:
    """Immutable result returned by a chat backend's ``invoke``.

    Returned as soon as the turn has been accepted; carries just the
    identifiers the route layer needs to build the HTTP 202 response. The
    final agent text is retrieved later via ``get_execution_result``.
    """

    execution_id: str
    session_id: str
    invoked_at: str  # ISO 8601 UTC


#: Literal enumeration of ``ChatServiceError.error_kind`` values.
#:
#: The route layer uses this classification to select HTTP status codes
#: without string-matching error messages (see spec 015 Requirement 6.1).
#:
#: NOTE: ``supervisor_not_configured`` predates the harness cutover and now
#: means "the configured backend is missing required configuration" (e.g.
#: ``HARNESS_ARN`` unset). The name is kept because renaming it would ripple
#: into the route layer's status-code mapping and its tests for no behaviour
#: change.
ErrorKind = Literal[
    "supervisor_not_configured",
    # Caller-supplied input the downstream API will reject -- classified
    # separately so the route returns 4xx instead of the generic 502, which
    # would blame the service for a client mistake.
    "invalid_session_id",
    "invoke_failed",
    "execution_failed",
    "execution_not_found",
    "result_parse_failed",
]


class ChatServiceError(Exception):
    """Raised when a chat backend operation fails.

    This exception wraps internal errors (e.g. AWS SDK failures) so that
    callers never see raw AWS error details. The ``error_kind`` attribute
    carries a coarse classification used by the route layer to select
    HTTP status codes per spec 015 Requirement 6.
    """

    def __init__(self, message: str, *, error_kind: ErrorKind) -> None:
        super().__init__(message)
        self.error_kind: ErrorKind = error_kind
