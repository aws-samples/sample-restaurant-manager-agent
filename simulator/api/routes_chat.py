# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chat API routes.

Exposes three endpoints:

- ``POST /chat`` — start a supervisor execution asynchronously. Returns
  HTTP 202 with ``{executionId, session_id, status: "started"}``.
- ``GET /chat/result/{execution_id}`` — poll for the supervisor result.
  Returns HTTP 200 with one of three Poll_Response shapes.
- ``GET /chat/history/{session_id}`` — retrieve conversation history
  from AgentCore Memory (unchanged from spec 008).

Error-kind to HTTP status mapping lives in this module per design
decision D5: the chat-backend layer stays HTTP-unaware and carries
an ``error_kind`` attribute on every ``ChatServiceError``; the route
layer translates that to the appropriate HTTP status code.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from simulator.api.chat_service import (
    ChatErrorResponse,
    ChatHistoryResponse,
    ChatRequest,
    ChatServiceError,
)
from simulator.api.harness_backend import (
    MIN_RUNTIME_SESSION_ID_LENGTH,
    HarnessChatBackend,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Error-kind to HTTP status mappings (design decision D5)
# ---------------------------------------------------------------------------

#: Mapping for the ``POST /chat`` endpoint.
#:
#: ``execution_not_found`` maps to 502 on this endpoint — the supervisor
#: execution started but the backend could not resolve its id, which is
#: an upstream-dependency failure, not a "not found" from the caller's
#: perspective (Requirement 6.5).
_ERROR_KIND_TO_HTTP_CHAT: dict[str, int] = {
    "supervisor_not_configured": 503,
    "invalid_session_id": 400,
    "invoke_failed": 502,
    "execution_failed": 502,
    "execution_not_found": 502,
    "result_parse_failed": 502,
}

#: Mapping for the ``GET /chat/result/{execution_id}`` endpoint.
#:
#: ``execution_not_found`` maps to 404 on this endpoint — the caller
#: supplied an unknown execution id (Requirement 6.5 endpoint-specific
#: override).
_ERROR_KIND_TO_HTTP_RESULT: dict[str, int] = {
    "supervisor_not_configured": 503,
    "invalid_session_id": 400,
    "invoke_failed": 502,
    "execution_failed": 502,
    "execution_not_found": 404,
    "result_parse_failed": 502,
}


def _http_status_for(
    error_kind: str, *, endpoint: Literal["chat", "result"]
) -> int:
    """Map an ``error_kind`` string to an HTTP status code."""
    mapping = (
        _ERROR_KIND_TO_HTTP_CHAT
        if endpoint == "chat"
        else _ERROR_KIND_TO_HTTP_RESULT
    )
    return mapping.get(error_kind, 502)


# ---------------------------------------------------------------------------
# Lazy chat-backend instance — deferred until first request so that
# ``load_dotenv()`` in ``main.py`` has already run by the time we read
# environment variables.
# ---------------------------------------------------------------------------

_chat_service: "HarnessChatBackend | None" = None


def _get_chat_service() -> "HarnessChatBackend":
    """Return the module-level chat backend, creating it on first call.

    The backend is unconditionally the AgentCore Managed Harness. The former
    ``AGENT_BACKEND`` flag and its durable-supervisor alternative were removed
    with the durable agent layer (spec 022 Task 6); the flag had defaulted to
    ``durable``, so a missing env var used to silently select a backend that no
    longer exists.
    """
    global _chat_service
    if _chat_service is None:
        _chat_service = HarnessChatBackend(
            region=os.environ.get("AWS_REGION", "us-west-2"),
            harness_arn=os.environ.get("HARNESS_ARN"),
            memory_id=os.environ.get("AGENTCORE_MEMORY_ID"),
            model_id=os.environ.get("BEDROCK_MODEL_ID"),
            store_id=os.environ.get("DEFAULT_STORE_ID", "sim-store-001"),
            iot_endpoint=os.environ.get("IOT_ENDPOINT"),
            actor_id=os.environ.get("AGENTCORE_ACTOR_ID", "default"),
        )
        if not _chat_service.configured:
            logger.warning(
                "HARNESS_ARN is not set — "
                "POST /chat and GET /chat/result will return 503"
            )
        if not _chat_service.memory_configured:
            logger.warning(
                "AGENTCORE_MEMORY_ID is not set — GET /chat/history will return 503"
            )
    return _chat_service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/chat/confirm",
    responses={
        202: {"description": "Confirmation recorded; resume polling the executionId"},
        404: {"model": ChatErrorResponse},
        410: {"model": ChatErrorResponse},
    },
)
async def chat_confirm(payload: dict, http_request: Request) -> JSONResponse:
    """Approve or decline a gated mutating tool call (spec 031, threat model TM-02).

    Deliberately a separate endpoint rather than a field on `POST /chat`: the
    decision must not be expressible by the model, and a distinct endpoint makes
    that structural rather than a matter of request parsing. The pending id is
    single-use, so a double-click cannot execute the tool twice.
    """
    svc = _get_chat_service()
    pending_id = (payload or {}).get("pending_id") or ""
    decision = (payload or {}).get("decision") or ""
    if not pending_id:
        return JSONResponse(status_code=400, content={"error": "pending_id is required."})
    try:
        svc.confirm_pending(
            pending_id=pending_id,
            decision=decision,
            manager_sub=http_request.headers.get("x-manager-sub"),
        )
    except ChatServiceError as exc:
        status = {
            "pending_not_found": 404,
            "pending_expired": 410,
            "invalid_request": 400,
        }.get(exc.error_kind, 502)
        return JSONResponse(status_code=status, content={"error": str(exc)})
    return JSONResponse(status_code=202, content={"status": "accepted"})


@router.post(
    "/chat",
    responses={
        202: {"description": "Supervisor execution started"},
        400: {"model": ChatErrorResponse},
        502: {"model": ChatErrorResponse},
        503: {"model": ChatErrorResponse},
    },
)
async def chat(request: ChatRequest, http_request: Request) -> JSONResponse:
    """Start an agent turn asynchronously.

    Returns HTTP 202 with ``{executionId, session_id, status: "started"}``
    on success. The frontend polls ``GET /chat/result/{execution_id}``
    for the final agent response.
    """
    svc = _get_chat_service()

    if not svc.configured:
        return JSONResponse(
            status_code=503,
            content={
                "error": ("Chat feature is not configured. Set HARNESS_ARN.")
            },
        )

    # Pydantic ``min_length=1`` rejects empty strings, but
    # whitespace-only strings still pass — check explicitly.
    if request.message.strip() == "":
        return JSONResponse(
            status_code=400,
            content={"error": "Message is required"},
        )

    # A caller-supplied session_id must satisfy AgentCore's runtimeSessionId
    # minimum. Checked HERE, before the 202, because this endpoint is
    # accept-then-poll: a failure raised later in the background turn loop
    # reaches the client as a generic "failed" on some later poll, with nothing
    # naming the field (it cost a debugging session during the spec 022 Task 6
    # verification). An omitted session_id is fine — the backend generates one.
    if request.session_id is not None and request.session_id.strip() != "":
        if len(request.session_id) < MIN_RUNTIME_SESSION_ID_LENGTH:
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"session_id must be at least "
                        f"{MIN_RUNTIME_SESSION_ID_LENGTH} characters "
                        f"(got {len(request.session_id)}). It is used as the "
                        "AgentCore runtimeSessionId, which enforces that "
                        "minimum. Omit it to have one generated."
                    )
                },
            )

    try:
        # TM-16: attribute the turn to the authenticated viewer. The Lambda@Edge
        # authenticator stamps the verified Cognito `sub` here and overwrites any
        # client-supplied value, so this is trustworthy for AUDIT purposes. It is
        # deliberately not used for authorization -- the store boundary is IAM and
        # server-side routing, so a missing header costs traceability, not
        # containment (which is why there is no 4xx when it is absent).
        result = svc.invoke(
            request.message,
            request.session_id,
            manager_sub=http_request.headers.get("x-manager-sub"),
        )
    except ChatServiceError as exc:
        return JSONResponse(
            status_code=_http_status_for(exc.error_kind, endpoint="chat"),
            content={"error": str(exc)},
        )

    return JSONResponse(
        status_code=202,
        content={
            "executionId": result.execution_id,
            "session_id": result.session_id,
            "status": "started",
        },
    )


@router.get(
    "/chat/result/{execution_id:path}",
    responses={
        200: {"description": "Poll_Response body"},
        404: {"model": ChatErrorResponse},
        502: {"model": ChatErrorResponse},
        503: {"model": ChatErrorResponse},
    },
)
async def chat_result(
    execution_id: str, session_id: str | None = None
) -> JSONResponse:
    """Poll for the final result of an agent turn.

    Returns HTTP 200 with one of three Poll_Response shapes:

    - ``{"status": "in_progress"}``
    - ``{"status": "succeeded", "response", "session_id", "timestamp"}``
    - ``{"status": "failed", "error"}``

    Returns HTTP 4xx/5xx only for backend-side lookup failures (unknown
    execution id → 404, invoke client errors → 502, harness unset → 503).
    """
    svc = _get_chat_service()

    if not svc.configured:
        return JSONResponse(
            status_code=503,
            content={
                "error": ("Chat feature is not configured. Set HARNESS_ARN.")
            },
        )

    try:
        result = svc.get_execution_result(execution_id, session_id)
    except ChatServiceError as exc:
        return JSONResponse(
            status_code=_http_status_for(exc.error_kind, endpoint="result"),
            content={"error": str(exc)},
        )

    return JSONResponse(status_code=200, content=result)


@router.get(
    "/chat/history/{session_id}",
    response_model=ChatHistoryResponse,
    responses={
        502: {"model": ChatErrorResponse},
        503: {"model": ChatErrorResponse},
    },
)
async def chat_history(session_id: str) -> ChatHistoryResponse | JSONResponse:
    """Retrieve conversation history from AgentCore Memory.

    Contract preserved verbatim from spec 008 (Requirement 5).
    """
    svc = _get_chat_service()

    if not svc.memory_configured:
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    "Chat history is not configured. "
                    "Set AGENTCORE_MEMORY_ID environment variable."
                )
            },
        )

    try:
        messages = svc.get_history(session_id)
    except ChatServiceError as exc:
        logger.error("chat history retrieval failed", exc_info=exc)
        return JSONResponse(
            status_code=502,
            content={"error": str(exc)},
        )

    return ChatHistoryResponse(
        session_id=session_id,
        messages=messages,
    )
