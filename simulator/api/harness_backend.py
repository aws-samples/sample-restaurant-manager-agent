# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Harness chat backend (spec 022, ``AGENT_BACKEND=harness``).

Drives the AgentCore Managed Harness on the inline-function tool path and
exposes the same surface the route layer expects from the durable
``ChatService`` (``configured`` / ``memory_configured`` / ``invoke`` /
``get_execution_result`` / ``get_history``), so ``routes_chat.py`` can select
either backend by environment with no contract change (R8).

Design (spec 022 design.md):
  - OD4: the POST-202 + GET-poll contract is preserved by running the
    (synchronous, multi-turn) harness loop in a background thread keyed by an
    ``execution_id``; ``get_execution_result`` polls the in-process task state.
  - OD1: inline tool path — on ``stopReason: tool_use`` the backend validates
    params, executes the tool via the IoT round-trip (Correlation A), and
    re-invokes with the assistant ``toolUse`` + user ``toolResult`` (the harness
    does not persist the partial turn).
  - OD3: memory is harness-managed (attached resource); there is no manual
    ``create_event`` write here. ``get_history`` reads the same memory resource.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import boto3

from simulator.api.history_text import clean_history_text
from simulator.api.chat_service import (
    ChatInvokeResult,
    ChatServiceError,
    HistoryMessage,
)
from simulator.api.harness_tools import (
    CONFIRMATION_REQUIRED_TOOLS,
    MUTATING_TOOL_NAMES,
    ToolParamError,
    execute_tool,
    select_inline_tools,
    stream_collect,
    validate_params,
)

logger = logging.getLogger(__name__)

#: How long a pending confirmation stays executable (spec 031 R1.5a).
#:
#: 90s is a CONSTRAINT, not a preference. The frontend abandons polling after
#: 300s measured from POST (useChat.js POLL_TIMEOUT_MS) and a baseline turn
#: already spends 25-55s of that. A longer window would let a confirmation
#: outlive the client that must consume it: the manager sees "Request timed out",
#: retries, and a still-executable pending record means the same void could be
#: approved twice. Expiring well inside the client's budget makes that
#: impossible.
CONFIRMATION_WINDOW_S: float = 90.0

#: The harness persists its session under this actor in the attached AgentCore
#: Memory resource (live-probed 2026-06-19: actorId == "default", sessionId ==
#: runtimeSessionId). NOT "chat-ui-user" (that was the durable path's actor).
DEFAULT_ACTOR_ID = "default"


def _event_sort_key(event: dict) -> tuple[int, str]:
    """Chronological sort key for an AgentCore Memory event.

    ``eventTimestamp`` is normally a ``datetime``, but tolerate a string (or a
    missing value) so a shape change cannot raise inside the history read.
    Returns a ``(has_timestamp, iso_string)`` tuple so undated events sort first
    and dated events sort by value; comparing ISO-8601 strings is equivalent to
    comparing the instants.
    """
    ts = event.get("eventTimestamp")
    if isinstance(ts, datetime):
        return (1, ts.isoformat())
    if ts:
        return (1, str(ts))
    return (0, "")
#: In-process task records older than this are evicted.
_TASK_TTL = timedelta(hours=1)

#: Minimum length AgentCore enforces on ``runtimeSessionId``.
#:
#: A service-side constraint, not ours: ``invoke_harness`` rejects anything shorter
#: with ``ParamValidationError: Invalid length for parameter runtimeSessionId ...
#: valid min length: 33``. Named here because the route needs the same number to
#: reject early, and a magic 33 in two files drifts. Also recorded in
#: architecture-decisions.md, "AgentCore Managed Harness (GA)", item 5.
MIN_RUNTIME_SESSION_ID_LENGTH = 33


class HarnessChatBackend:
    """AgentCore-harness implementation of the chat backend surface."""

    def __init__(
        self,
        *,
        region: str,
        harness_arn: str | None,
        memory_id: str | None,
        model_id: str | None = None,
        store_id: str = "sim-store-001",
        iot_endpoint: str | None = None,
        actor_id: str = DEFAULT_ACTOR_ID,
        max_turns: int = 10,
        tool_timeout_s: float = 30.0,
        tool_executor: Callable[..., dict] | None = None,
    ) -> None:
        self.region = region
        self.harness_arn = (harness_arn or "").strip() or None
        self.memory_id = (memory_id or "").strip() or None
        self.model_id = (model_id or "").strip() or None
        self.store_id = store_id
        self.iot_endpoint = (iot_endpoint or "").strip() or None
        self.actor_id = actor_id
        self.max_turns = max_turns
        self.tool_timeout_s = tool_timeout_s
        # Injectable for tests; defaults to the real IoT round-trip.
        self._tool_executor = tool_executor or execute_tool

        self._agentcore_client: Any | None = None
        self._lock = threading.Lock()
        # REVIEW (cravinho, 2026-08-18): task state is in-process only. The
        # POST-202/GET-poll contract therefore assumes a SINGLE app instance --
        # behind >1 instance (or after a restart / instance replacement) the poll
        # can land on a process that never ran the turn and returns
        # execution_not_found (404) for a real, in-flight execution. Deploy model
        # is a single long-lived EC2 today (see the CDK stack), so this holds, but
        # it should be an explicit, documented invariant since horizontal scaling
        # would silently break it. Spec 031's pending confirmations inherit the
        # same constraint: a pending record lives only in the process that created
        # it, so a confirmation cannot be completed by a different instance.
        self._tasks: dict[str, dict] = {}

    # -- Backend-agnostic configuration gates (used by routes) --------------

    @property
    def configured(self) -> bool:
        return self.harness_arn is not None

    @property
    def memory_configured(self) -> bool:
        return self.memory_id is not None

    # -- Lazy client --------------------------------------------------------

    @property
    def agentcore_client(self) -> Any:
        if self._agentcore_client is None:
            self._agentcore_client = boto3.client("bedrock-agentcore", region_name=self.region)
        return self._agentcore_client

    # -- Session id ---------------------------------------------------------

    @staticmethod
    def generate_session_id() -> str:
        """A session id satisfying ``MIN_RUNTIME_SESSION_ID_LENGTH``.

        A uuid4 is 36 chars, so the suffix is belt-and-braces rather than
        required; ``test_generated_session_id_meets_the_minimum`` pins the
        property so a future "simplification" to a bare uuid4 (36) or a shorter
        token cannot quietly drop below the limit.
        """
        return f"{uuid.uuid4()}-{str(uuid.uuid4())[:8]}"

    # -- Task registry ------------------------------------------------------

    def _evict_expired_locked(self, now: datetime) -> None:
        cutoff = now - _TASK_TTL
        for exec_id in [
            k for k, v in self._tasks.items() if v.get("created_at", now) < cutoff
        ]:
            del self._tasks[exec_id]

    # -- Invoke (POST /chat) ------------------------------------------------

    def invoke(
        self,
        message: str,
        session_id: str | None = None,
        manager_sub: str | None = None,
    ) -> ChatInvokeResult:
        """Start one chat turn.

        ``manager_sub`` is the authenticated viewer's opaque Cognito ``sub``,
        stamped onto the origin request by the Lambda@Edge authenticator and used
        for AUDIT ATTRIBUTION ONLY (threat model TM-16). It is never consulted for
        authorization: the store boundary is enforced by IAM and by server-side
        store routing, so a missing or wrong value costs traceability, never
        containment.
        """
        if not self.configured:
            raise ChatServiceError(
                "Chat feature is not configured. Set HARNESS_ARN.",
                error_kind="supervisor_not_configured",
            )
        resolved_session = session_id if (session_id and session_id.strip()) else self.generate_session_id()

        # Defence-in-depth for non-HTTP callers (the voice broker also drives
        # sessions). The route validates first and returns 400; without this guard
        # a short id reaches invoke_harness deep inside the background turn loop,
        # where botocore's ParamValidationError is caught and reported to the
        # caller as the generic "The assistant failed to produce a response" --
        # unattributable without reading the instance log. Raising here keeps the
        # failure on the synchronous path, before the 202.
        if len(resolved_session) < MIN_RUNTIME_SESSION_ID_LENGTH:
            raise ChatServiceError(
                f"session_id must be at least {MIN_RUNTIME_SESSION_ID_LENGTH} "
                f"characters (got {len(resolved_session)}); it is passed to "
                "AgentCore as runtimeSessionId, which enforces that minimum.",
                error_kind="invalid_session_id",
            )

        execution_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with self._lock:
            self._evict_expired_locked(now)
            self._tasks[execution_id] = {
                "state": "in_progress",
                "session_id": resolved_session,
                "created_at": now,
            }

        logger.info(
            "marker=chat_harness_invoke execution_id=%s session_id=%s", execution_id, resolved_session
        )
        thread = threading.Thread(
            target=self._run_turn_loop,
            args=(execution_id, message, resolved_session, manager_sub),
            name=f"harness-turn-{execution_id[:8]}",
            daemon=True,
        )
        thread.start()

        return ChatInvokeResult(
            execution_id=execution_id,
            session_id=resolved_session,
            invoked_at=now.isoformat(),
        )

    # -- Background turn loop ----------------------------------------------

    def _run_turn_loop(
        self,
        execution_id: str,
        message: str,
        session_id: str,
        manager_sub: str | None = None,
    ) -> None:
        try:
            answer = self._drive(
                message, session_id, manager_sub=manager_sub, execution_id=execution_id
            )
            if answer is None:
                # Pending confirmation published; the task state is already
                # confirmation_required. Do NOT mark succeeded.
                return
            with self._lock:
                task = self._tasks.get(execution_id)
                if task is not None:
                    task.update(
                        state="succeeded",
                        response=answer,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("harness turn loop failed execution_id=%s", execution_id)
            with self._lock:
                task = self._tasks.get(execution_id)
                if task is not None:
                    task.update(state="failed", error="The assistant failed to produce a response.")
            _ = exc

    def _drive(
        self,
        message: str,
        session_id: str,
        manager_sub: str | None = None,
        execution_id: str | None = None,
        resume_messages: list[dict] | None = None,
    ) -> str | None:
        """Run the inline tool loop until end_turn (or max_turns).

        Returns the answer, or ``None`` when a gated tool published a pending
        confirmation and the turn is to be continued later (spec 031).
        """
        messages: list[dict] = resume_messages or [
            {"role": "user", "content": [{"text": message}]}
        ]
        final_text = ""
        # spec 027: optionally offer a relevant subset of tools (default OFF ->
        # all tools). Computed once per turn so the offered set is stable across
        # the tool-use loop.
        tools = select_inline_tools(message)
        for _turn in range(self.max_turns):
            kwargs: dict[str, Any] = {
                "harnessArn": self.harness_arn,
                "runtimeSessionId": session_id,
                "messages": messages,
                "tools": tools,
            }
            if self.model_id:
                kwargs["model"] = {"bedrockModelConfig": {"modelId": self.model_id}}
            resp = self.agentcore_client.invoke_harness(**kwargs)
            text, tu, stop = stream_collect(resp)

            if stop == "tool_use" and tu.get("id") and tu.get("name"):
                # REVIEW (cravinho, 2026-08-18): `messages` is REPLACED (not
                # appended) with just the toolUse + toolResult pair, dropping the
                # original user turn and any prior tool exchanges from the
                # payload. This is correct ONLY if the harness reconstructs the
                # full turn from server-side runtimeSessionId state; if it
                # instead relies on the passed `messages`, a multi-tool turn
                # loses context and the model can re-request the same tool. Also
                # note the loop handles ONE toolUse block per turn (a
                # parallel/multi-tool stop would execute just the first).
                #
                # ANSWERED (2026-08-19): the harness DOES keep session history
                # server-side, keyed by `runtimeSessionId` -- reusing the id
                # gives same-session continuity with no caller-side message
                # replay (validated in the spec 021 GA re-evaluation, and again
                # by the spec 031 live probe, where a multi-tool turn stayed
                # coherent across a suspend/resume that passed only a two-message
                # payload). So replacing rather than appending is correct here.
                # The single-toolUse-per-turn observation stands and is untested;
                # worth a look if a model version starts emitting parallel tool
                # use.
                # spec 031: a gated tool suspends the turn instead of executing.
                # Returning None means "pending published, stop driving" -- the
                # thread ends here rather than blocking (L4), and a fresh thread
                # resumes from the confirm endpoint.
                built = self._build_tool_result_messages(
                    tu, manager_sub=manager_sub, execution_id=execution_id
                )
                if built is None:
                    return None
                messages = built
                continue

            final_text = text
            return final_text

        return final_text or "I reached the maximum number of steps before finishing. Please try rephrasing."

    def _build_tool_result_messages(
        self,
        tu: dict,
        manager_sub: str | None = None,
        execution_id: str | None = None,
        confirmed: bool = False,
    ) -> list[dict] | None:
        """Validate + execute the requested tool and build the next-turn
        assistant ``toolUse`` + user ``toolResult`` messages.

        Emits one structured audit record per invocation (threat model TM-16).
        """
        try:
            args = json.loads(tu.get("input") or "{}")
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}

        try:
            clean = validate_params(tu["name"], args)
        except ToolParamError as exc:
            tool_payload = {"status": "error", "error": {"code": "invalid_input", "message": str(exc)}}
        else:
            # TM-01: the store is a property of the SESSION, never of the
            # conversation. This previously read
            #     sid = clean.get("store_id") or self.store_id
            # which let a prompt-injected model name the store its tool call was
            # routed to -- `execute_tool` publishes to
            # `stores/{store_id}/commands/{domain}`, so that was a cross-tenant
            # write primitive. `validate_params` now drops `store_id` (it is not
            # an allowed key on any tool) and the schema no longer offers it, so
            # this is the third of three independent controls; it is written
            # explicitly rather than relying on the other two.
            sid = self.store_id
            params = {k: v for k, v in clean.items() if k != "store_id"}

            # spec 031 / TM-02: a gated tool does not execute on the model's word.
            # The gate sits AFTER validation so the human approves the exact
            # validated parameters that will run (R1.3) -- never something
            # re-derived from the conversation afterwards.
            if (
                tu["name"] in CONFIRMATION_REQUIRED_TOOLS
                and not confirmed
                and execution_id is not None
            ):
                self._publish_pending_confirmation(
                    execution_id=execution_id,
                    tool_use_id=tu["id"],
                    tool_name=tu["name"],
                    parameters=params,
                    store_id=sid,
                    args=args,
                    manager_sub=manager_sub,
                )
                return None

            tool_payload = self._tool_executor(
                tool_name=tu["name"],
                parameters=params,
                store_id=sid,
                iot_endpoint=self.iot_endpoint or "",
                region=self.region,
                timeout_s=self.tool_timeout_s,
            )

        result_text = json.dumps(tool_payload)
        status = "success" if tool_payload.get("status") == "success" else "error"

        # --- TM-16 audit record -------------------------------------------------
        # One line per tool invocation, including rejected ones: "which tool calls
        # were attempted" is as much of an audit question as "which succeeded",
        # and a validation rejection is exactly what an injection attempt looks
        # like from here.
        #
        # `manager` is the opaque Cognito `sub` stamped by the Lambda@Edge
        # authenticator, or "unattributed" for a non-HTTP caller such as the voice
        # broker. Deliberately NOT an email: an audit log should not accumulate
        # PII when a stable opaque id answers "who did this".
        #
        # Emitted at INFO unconditionally -- mutating tools especially must not be
        # discoverable only at DEBUG. The tool NAME and store are logged; tool
        # PARAMETERS are not, because they are model-controlled free text that can
        # carry whatever an injected prompt puts there, and a log is a poor place
        # to land it.
        logger.info(
            "marker=tool_audit manager=%s session_tool=%s store_id=%s outcome=%s mutating=%s",
            manager_sub or "unattributed",
            tu.get("name"),
            self.store_id,
            status,
            tu.get("name") in MUTATING_TOOL_NAMES,
        )

        return [
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": args}}]},
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": tu["id"], "content": [{"text": result_text}], "status": status}}
                ],
            },
        ]

    # -- Poll (GET /chat/result) -------------------------------------------

    def _publish_pending_confirmation(
        self,
        *,
        execution_id: str,
        tool_use_id: str,
        tool_name: str,
        parameters: dict,
        store_id: str,
        args: dict,
        manager_sub: str | None,
    ) -> None:
        """Record a gated tool call awaiting human approval (spec 031).

        Stores the VALIDATED parameters and the resolved store, plus enough
        in-flight turn state to resume: the assistant ``toolUse`` message this
        turn already produced. Nothing is re-derived on resume.
        """
        pending_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._lock:
            task = self._tasks.get(execution_id)
            if task is None:  # evicted mid-turn; nothing to confirm against
                return
            task.update(
                state="confirmation_required",
                pending={
                    "pending_id": pending_id,
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "parameters": parameters,
                    "store_id": store_id,
                    "args": args,
                    "manager_sub": manager_sub,
                    "expires_at": now + timedelta(seconds=CONFIRMATION_WINDOW_S),
                },
            )
        logger.info(
            "marker=tool_audit manager=%s session_tool=%s store_id=%s outcome=%s mutating=%s",
            manager_sub or "unattributed",
            tool_name,
            store_id,
            "confirmation_required",
            True,
        )

    def confirm_pending(
        self, *, pending_id: str, decision: str, manager_sub: str | None = None
    ) -> None:
        """Approve or decline a pending gated tool call (spec 031).

        Single-use by construction: the record is POPPED under the lock before the
        resuming thread starts, so a double-click cannot execute the tool twice
        (R1.4). Expiry is enforced here as well as on read, because a stale client
        could otherwise complete an abandoned turn (R1.5b).
        """
        if decision not in ("approve", "deny"):
            raise ChatServiceError(
                "decision must be 'approve' or 'deny'.", error_kind="invalid_request"
            )

        now = datetime.now(timezone.utc)
        with self._lock:
            match = None
            for execution_id, task in self._tasks.items():
                pending = task.get("pending")
                if pending and pending.get("pending_id") == pending_id:
                    match = (execution_id, task, pending)
                    break
            if match is None:
                raise ChatServiceError(
                    "No pending confirmation with that id. It may have already been "
                    "used.",
                    error_kind="pending_not_found",
                )
            execution_id, task, pending = match
            if pending["expires_at"] <= now:
                task.pop("pending", None)
                task.update(
                    state="failed",
                    error="The confirmation window expired, so nothing was changed.",
                )
                self._audit_confirmation(pending, "confirmation_expired")
                raise ChatServiceError(
                    "The confirmation window expired.", error_kind="pending_expired"
                )
            # Consume it: no second execution is possible from here.
            task.pop("pending", None)
            task["state"] = "in_progress"

        approved = decision == "approve"
        self._audit_confirmation(
            pending, "confirmation_granted" if approved else "confirmation_denied"
        )

        thread = threading.Thread(
            target=self._resume_after_confirmation,
            args=(execution_id, task.get("session_id"), pending, approved, manager_sub),
            name=f"harness-confirm-{execution_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _audit_confirmation(self, pending: dict, outcome: str) -> None:
        logger.info(
            "marker=tool_audit manager=%s session_tool=%s store_id=%s outcome=%s mutating=%s",
            pending.get("manager_sub") or "unattributed",
            pending.get("tool_name"),
            pending.get("store_id"),
            outcome,
            True,
        )

    def _resume_after_confirmation(
        self,
        execution_id: str,
        session_id: str | None,
        pending: dict,
        approved: bool,
        manager_sub: str | None,
    ) -> None:
        """Finish a turn that was suspended for confirmation (spec 031)."""
        try:
            tool_use_id = pending["tool_use_id"]
            tool_name = pending["tool_name"]

            if approved:
                tool_payload = self._tool_executor(
                    tool_name=tool_name,
                    parameters=pending["parameters"],
                    store_id=pending["store_id"],
                    iot_endpoint=self.iot_endpoint or "",
                    region=self.region,
                    timeout_s=self.tool_timeout_s,
                )
                status = "success" if tool_payload.get("status") == "success" else "error"
            else:
                # A decline is a tool RESULT, not an error: the model should tell
                # the manager their action was not taken. Surfacing it as a failure
                # would read as "the assistant broke", which is both wrong and
                # unhelpful. The store is never called.
                tool_payload = {
                    "status": "declined",
                    "error": {
                        "code": "declined_by_manager",
                        "message": (
                            f"The manager declined the {tool_name} action, so nothing "
                            "was changed."
                        ),
                    },
                }
                status = "error"

            logger.info(
                "marker=tool_audit manager=%s session_tool=%s store_id=%s outcome=%s mutating=%s",
                manager_sub or pending.get("manager_sub") or "unattributed",
                tool_name,
                pending["store_id"],
                status if approved else "declined",
                True,
            )

            messages = [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": tool_use_id,
                                "name": tool_name,
                                "input": pending.get("args") or {},
                            }
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"text": json.dumps(tool_payload)}],
                                "status": "success" if status == "success" else "error",
                            }
                        }
                    ],
                },
            ]

            answer = self._drive(
                "",
                session_id or self.generate_session_id(),
                manager_sub=manager_sub,
                execution_id=execution_id,
                resume_messages=messages,
            )
            if answer is None:
                return  # another gated tool -> a further pending state was published
            with self._lock:
                task = self._tasks.get(execution_id)
                if task is not None:
                    task.update(
                        state="succeeded",
                        response=answer,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "confirmation resume failed execution_id=%s", execution_id
            )
            with self._lock:
                task = self._tasks.get(execution_id)
                if task is not None:
                    task.update(
                        state="failed",
                        error="The assistant failed to complete the confirmed action.",
                    )

    def get_execution_result(self, execution_id: str, session_id: str | None) -> dict:
        if not self.configured:
            raise ChatServiceError(
                "Chat feature is not configured. Set HARNESS_ARN.",
                error_kind="supervisor_not_configured",
            )
        with self._lock:
            self._evict_expired_locked(datetime.now(timezone.utc))
            task = self._tasks.get(execution_id)
            if task is None:
                raise ChatServiceError("Execution not found.", error_kind="execution_not_found")
            state = task.get("state")
            if state == "in_progress":
                return {"status": "in_progress"}
            if state == "confirmation_required":
                pending = task.get("pending") or {}
                # Expiry is checked here rather than by a background sweeper: a
                # sweeper is a second thing to get wrong, and this path already
                # runs on every poll (spec 031 design).
                expires_at = pending.get("expires_at")
                if not pending or (expires_at and expires_at <= datetime.now(timezone.utc)):
                    task.pop("pending", None)
                    task.update(
                        state="failed",
                        error=(
                            "The confirmation window expired, so nothing was changed."
                        ),
                    )
                    if pending:
                        self._audit_confirmation(pending, "confirmation_expired")
                    return {
                        "status": "failed",
                        "error": "The confirmation window expired, so nothing was changed.",
                    }
                remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
                return {
                    "status": "confirmation_required",
                    "pending": {
                        "pending_id": pending["pending_id"],
                        "tool_name": pending["tool_name"],
                        # Safe to render: already key-allowlisted and type-checked.
                        "parameters": pending["parameters"],
                        "expires_in_s": max(0, int(remaining)),
                    },
                }
            if state == "failed":
                return {"status": "failed", "error": task.get("error", "Execution failed.")}
            # succeeded
            resolved_session = (
                session_id if (session_id and session_id.strip()) else task.get("session_id")
            ) or self.generate_session_id()
            return {
                "status": "succeeded",
                "response": task.get("response", ""),
                "session_id": resolved_session,
                "timestamp": task.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }

    # -- History (GET /chat/history) ---------------------------------------

    def get_history(self, session_id: str) -> list[HistoryMessage]:
        """Return conversation history for a session from the attached memory.

        Reads ``list_events`` for ``actorId`` (default ``"default"`` — the actor
        the harness writes under) and extracts ``conversational`` payloads.

        **This works for harness sessions** — live-verified 2026-08-07 against
        the deployed AgentCore Memory resource, which returned the user question and
        the assistant's prose reply in order. An earlier note here (2026-06-19)
        said the GA harness persisted *only* ``blob`` state checkpoints and that
        this method therefore always returned empty, pending a conversational
        extraction strategy. That is no longer true: the harness interleaves
        ``blob`` state with ``conversational`` events carrying the transcript, so
        **no extraction strategy is required for this endpoint**. (Cross-session
        recall is a different feature and does still need a configured strategy —
        see architecture-decisions, "AgentCore Managed Harness (GA)" item 7.)

        Two properties of the real payloads are load-bearing, both handled below:

        * ``content.text`` is usually a **serialized record**, not prose --
          ``{"message": {"role": ..., "content": [{"text": ...}]}, "message_id": 0,
          ...}`` -- so it is passed through ``clean_history_text``, which recovers
          the prose and drops tool-use/tool-result-only turns.
        * ``list_events`` returns events **newest-first**, so they must be sorted.

        The practical limit on history is **retention, not extraction**: the memory
        resource's ``eventExpiryDuration`` is **3 days**, so a session older than
        that has no events at all and legitimately returns an empty list. Raise
        that value if a longer transcript view is ever wanted.

        The endpoint contract (R8) holds either way: a valid, possibly empty,
        history is returned rather than an error.
        """
        if not self.memory_configured:
            raise ChatServiceError(
                "Chat history is not configured", error_kind="supervisor_not_configured"
            )
        try:
            response = self.agentcore_client.list_events(
                memoryId=self.memory_id,
                actorId=self.actor_id,
                sessionId=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Not logged here: the route that handles ChatServiceError logs it
            # once, with the chained list_events traceback.
            raise ChatServiceError(
                "Failed to retrieve chat history", error_kind="invoke_failed"
            ) from exc

        messages: list[HistoryMessage] = []
        # ``list_events`` returns events NEWEST-FIRST, so the raw order is
        # reverse-chronological: an assistant reply arrives before the user turn
        # that prompted it. Consumers need chronological order --
        #   * the chat UI renders the conversation backwards otherwise; and
        #   * seeding a reversed history into Nova Sonic put an ASSISTANT turn
        #     first, which the service rejects outright with
        #     "First message in chat history should not be Assistant.",
        #     killing the entire voice session (2026-08-05 outage).
        # Each event carries at most one conversational payload, so ordering the
        # events is sufficient; there is no intra-event ordering to preserve.
        for event in sorted(
            response.get("events", []), key=lambda e: _event_sort_key(e)
        ):
            ts = event.get("eventTimestamp", "")
            ts = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            for item in event.get("payload", []):
                conv = item.get("conversational")
                if conv is None:
                    continue
                # Sanitise: Memory can hold turns whose "text" is a serialized
                # tool-use RECORD rather than prose (see history_text).
                content = clean_history_text(conv.get("content", {}).get("text", ""))
                if content is None:
                    continue
                messages.append(
                    HistoryMessage(
                        role=conv.get("role", "").lower(),
                        content=content,
                        timestamp=ts,
                    )
                )
        return messages
