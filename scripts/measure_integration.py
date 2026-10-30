#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end integration test for the spec 015 durable-supervisor cutover.

Drives a running FastAPI backend through the POST-202 + poll + history
contract, asserting each of the seven observable behaviors from Requirement
10 of the frontend/supervisor cutover requirements (spec 015).

Exits 0 on PASS, 1 on FAIL. Prints a JSON summary on stdout (or to the path
passed via ``--output``) with per-step evidence, latencies, execution id,
session id, and an overall verdict.

Usage::

    python scripts/measure_integration.py --base-url http://localhost:8000
    python scripts/measure_integration.py --base-url https://<cloudfront-domain>

Notable flags::

    --initial-poll-delay-s FLOAT
        Default 3.0. Sleeps between the POST-202 ack and the first poll
        to tolerate ~1-2s of GetDurableExecution eventual consistency on
        freshly-started executions. See the 2026-05-05 finding
        'measure_integration.py first-poll race' for the underlying
        investigation. Set to 0 to disable for backends that don't need it.

Only the Python 3.12 standard library is used — no external dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    UnknownHandler,
)

#: Schemes ``_http`` will issue a request for. urllib supports far more than
#: HTTP (notably ``file://``), so this is an allow-list, not a denylist.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: An opener that can only speak HTTP(S). ``urlopen``'s default opener also
#: registers file/ftp/data handlers; this one has none, so a non-HTTP URL fails
#: with "unknown url type" even if it ever slipped past the allow-list check.
_HTTP_OPENER = OpenerDirector()
for _handler in (
    ProxyHandler(),
    HTTPHandler(),
    HTTPSHandler(),
    HTTPRedirectHandler(),
    HTTPDefaultErrorHandler(),
    HTTPErrorProcessor(),
    UnknownHandler(),  # without it, open() of an unhandled scheme returns None
):
    _HTTP_OPENER.add_handler(_handler)


# ---------------------------------------------------------------------------
# Small HTTP helper (stdlib only)
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """Normalized HTTP response."""

    status: int
    body: Any  # parsed JSON (dict/list) or raw str on parse failure
    raw: str
    latency_ms: float


def _http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    timeout_s: float = 30.0,
) -> HttpResponse:
    """Issue a single HTTP request and return a normalized response.

    4xx/5xx bodies are returned as ``HttpResponse`` with the status code
    intact (we do not raise on non-2xx, because several test steps
    explicitly validate error statuses).
    """
    # ``_HTTP_OPENER`` already cannot fetch ``file://`` (or ftp://, data://, ...);
    # this check just fails early with a clearer message than "unknown url
    # type". ``url`` derives from the operator's ``--base-url``, so this is not a
    # privilege boundary either way.
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Refusing to request {url!r}: scheme {scheme or '<none>'!r} is not "
            f"one of {sorted(_ALLOWED_URL_SCHEMES)}."
        )

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        resp = _HTTP_OPENER.open(req, timeout=timeout_s)
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = exc.code
    except URLError as exc:
        raise RuntimeError(f"Network error for {method} {url}: {exc}") from exc

    latency_ms = (time.perf_counter() - start) * 1000.0

    parsed: Any
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
    else:
        parsed = None

    return HttpResponse(status=status, body=parsed, raw=raw, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Summary accumulator
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    """Structured output for the measurement run."""

    started_at: str
    base_url: str
    prompt: str
    steps: list[dict] = field(default_factory=list)
    verdict: str = "UNKNOWN"
    notes: list[str] = field(default_factory=list)

    def record(self, **kwargs: Any) -> None:
        self.steps.append({"observed_at": _now_iso(), **kwargs})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


class AssertionFailure(Exception):
    """Raised to mark a single Requirement 10 assertion as failed."""


def _assert(
    condition: bool, summary: Summary, step_name: str, message: str
) -> None:
    if not condition:
        summary.record(step=step_name, status="FAILED", message=message)
        raise AssertionFailure(f"[{step_name}] {message}")


# ---------------------------------------------------------------------------
# Main measurement flow
# ---------------------------------------------------------------------------


def run_measurement(args: argparse.Namespace) -> Summary:
    """Execute the full Requirement 10 measurement flow.

    Steps mirror `design.md` Requirement 10 subclauses 10.1 through 10.7
    plus the two negative cases. A single failure at any step raises
    :class:`AssertionFailure`; the caller records the verdict.
    """
    base_url = args.base_url.rstrip("/")
    summary = Summary(
        started_at=_now_iso(),
        base_url=base_url,
        prompt=args.prompt,
    )

    # ----- Step 1: POST /api/chat -----------------------------------------
    print("[1/6] POST /api/chat ...", flush=True)
    post_resp = _http(
        "POST",
        f"{base_url}/api/chat",
        body={"message": args.prompt},
        timeout_s=30.0,
    )

    _assert(
        post_resp.status == 202,
        summary,
        "post_chat",
        f"expected 202, got {post_resp.status} (body: {post_resp.raw[:500]})",
    )
    _assert(
        isinstance(post_resp.body, dict),
        summary,
        "post_chat",
        f"expected JSON object, got {type(post_resp.body).__name__}",
    )
    exec_id = post_resp.body.get("executionId")
    session_id = post_resp.body.get("session_id")
    post_status = post_resp.body.get("status")
    _assert(
        isinstance(exec_id, str) and exec_id,
        summary,
        "post_chat",
        f"executionId missing or empty in body: {post_resp.body!r}",
    )
    _assert(
        isinstance(session_id, str) and len(session_id) >= 33,
        summary,
        "post_chat",
        f"session_id missing or too short (len={len(session_id) if isinstance(session_id, str) else 'n/a'}): {session_id!r}",
    )
    _assert(
        post_status == "started",
        summary,
        "post_chat",
        f"expected status=started, got {post_status!r}",
    )

    summary.record(
        step="post_chat",
        status="PASSED",
        http_status=post_resp.status,
        latency_ms=round(post_resp.latency_ms, 2),
        execution_id=exec_id,
        session_id=session_id,
    )
    print(
        f"  ok: 202, executionId={exec_id}, session_id={session_id}, "
        f"latency={post_resp.latency_ms:.1f}ms",
        flush=True,
    )

    # ----- Step 2: Poll GET /api/chat/result/{id} -------------------------
    print("[2/6] Poll GET /api/chat/result/{id} ...", flush=True)
    poll_url = (
        f"{base_url}/api/chat/result/{quote(exec_id)}"
        f"?session_id={quote(session_id)}"
    )

    # Eventual-consistency window on GetDurableExecution for just-started
    # executions: polling the backend (which calls get_durable_execution)
    # within ~1-2s of the POST-202 ack returns ResourceNotFoundException /
    # 404 for freshly-created executions, even though the full DurableExecutionArn
    # was already returned in the response header. See the 2026-05-05 finding
    # 'measure_integration.py first-poll race' (pending-validation/) for the
    # underlying investigation. A 3s default sleep removes the race with a
    # large safety margin; ``--initial-poll-delay-s 0`` bypasses it.
    if args.initial_poll_delay_s > 0:
        print(
            f"  waiting {args.initial_poll_delay_s:.1f}s before first poll "
            "(eventual-consistency buffer)",
            flush=True,
        )
        time.sleep(args.initial_poll_delay_s)

    overall_deadline = time.perf_counter() + args.overall_timeout_s
    poll_count = 0
    poll_latencies: list[float] = []
    terminal_resp: HttpResponse | None = None

    while time.perf_counter() < overall_deadline:
        poll_count += 1
        poll_resp = _http("GET", poll_url, timeout_s=30.0)
        poll_latencies.append(poll_resp.latency_ms)

        _assert(
            poll_resp.status == 200,
            summary,
            f"poll_{poll_count}",
            f"expected 200, got {poll_resp.status} (body: {poll_resp.raw[:500]})",
        )
        _assert(
            isinstance(poll_resp.body, dict),
            summary,
            f"poll_{poll_count}",
            f"expected JSON object, got {type(poll_resp.body).__name__}",
        )

        status_val = poll_resp.body.get("status")
        if status_val == "in_progress":
            time.sleep(args.poll_interval_s)
            continue
        if status_val in ("succeeded", "failed"):
            terminal_resp = poll_resp
            break
        _assert(
            False,
            summary,
            f"poll_{poll_count}",
            f"unexpected status={status_val!r} in body: {poll_resp.body!r}",
        )

    _assert(
        terminal_resp is not None,
        summary,
        "poll_terminal",
        f"did not reach terminal status within {args.overall_timeout_s}s "
        f"after {poll_count} polls",
    )
    assert terminal_resp is not None  # for type-narrowing

    terminal_status = terminal_resp.body.get("status")
    _assert(
        terminal_status == "succeeded",
        summary,
        "poll_terminal",
        f"expected terminal status=succeeded, got {terminal_status!r} "
        f"(body: {terminal_resp.body!r})",
    )

    response_text = terminal_resp.body.get("response")
    _assert(
        isinstance(response_text, str) and response_text,
        summary,
        "poll_terminal",
        f"succeeded body missing non-empty response: {terminal_resp.body!r}",
    )
    terminal_session_id = terminal_resp.body.get("session_id")
    _assert(
        terminal_session_id == session_id,
        summary,
        "poll_terminal",
        f"session_id drift: POST returned {session_id!r}, "
        f"SUCCEEDED body returned {terminal_session_id!r}",
    )

    avg_poll_ms = round(sum(poll_latencies) / len(poll_latencies), 2)
    total_poll_ms = round(sum(poll_latencies), 2)
    summary.record(
        step="poll_terminal",
        status="PASSED",
        poll_count=poll_count,
        avg_poll_latency_ms=avg_poll_ms,
        total_poll_latency_ms=total_poll_ms,
        response_length=len(response_text),
        response_preview=response_text[:200],
    )
    print(
        f"  ok: succeeded after {poll_count} polls, "
        f"avg_latency={avg_poll_ms}ms, response_len={len(response_text)}",
        flush=True,
    )

    # ----- Step 3: GET /api/chat/history/{session_id} ---------------------
    print("[3/6] GET /api/chat/history/{session_id} ...", flush=True)
    history_url = f"{base_url}/api/chat/history/{quote(session_id)}"
    history_resp = _http("GET", history_url, timeout_s=30.0)

    _assert(
        history_resp.status == 200,
        summary,
        "history_first",
        f"expected 200, got {history_resp.status} (body: {history_resp.raw[:500]})",
    )
    _assert(
        isinstance(history_resp.body, dict),
        summary,
        "history_first",
        f"expected JSON object, got {type(history_resp.body).__name__}",
    )
    messages = history_resp.body.get("messages", [])
    _assert(
        isinstance(messages, list),
        summary,
        "history_first",
        f"messages not a list: {messages!r}",
    )
    history_count_before = len(messages)

    # Require at least one USER message matching the prompt and one
    # ASSISTANT message matching the response (paired MemoryEvent).
    found_user = any(
        m.get("role") == "user" and m.get("content", "").strip() == args.prompt.strip()
        for m in messages
    )
    found_assistant = any(
        m.get("role") == "assistant" and m.get("content", "") == response_text
        for m in messages
    )
    _assert(
        found_user,
        summary,
        "history_first",
        f"no USER message matching prompt in history (got {history_count_before} messages)",
    )
    _assert(
        found_assistant,
        summary,
        "history_first",
        f"no ASSISTANT message matching supervisor response in history "
        f"(got {history_count_before} messages)",
    )

    summary.record(
        step="history_first",
        status="PASSED",
        http_status=history_resp.status,
        latency_ms=round(history_resp.latency_ms, 2),
        message_count=history_count_before,
    )
    print(
        f"  ok: {history_count_before} messages, paired USER+ASSISTANT present",
        flush=True,
    )

    # ----- Step 4: Idempotency re-poll + re-history -----------------------
    # Repeat the poll once more: Memory should NOT be written a second time
    # (Requirement 10.5 + Requirement 4.2 memory-write-once).
    print("[4/6] Re-poll + re-history (idempotency) ...", flush=True)
    repoll_resp = _http("GET", poll_url, timeout_s=30.0)
    _assert(
        repoll_resp.status == 200,
        summary,
        "repoll",
        f"expected 200, got {repoll_resp.status}",
    )
    _assert(
        repoll_resp.body.get("status") == "succeeded",
        summary,
        "repoll",
        f"repoll status not succeeded: {repoll_resp.body!r}",
    )

    # Small delay so a late Memory write from the first poll can surface if
    # the idempotency check is broken.
    # nosemgrep: arbitrary-sleep
    time.sleep(1.0)
    rehistory_resp = _http("GET", history_url, timeout_s=30.0)
    _assert(
        rehistory_resp.status == 200,
        summary,
        "rehistory",
        f"expected 200, got {rehistory_resp.status}",
    )
    rehistory_messages = rehistory_resp.body.get("messages", [])
    history_count_after = len(rehistory_messages)
    _assert(
        history_count_after == history_count_before,
        summary,
        "rehistory",
        f"memory_write_once violated: history_count_before="
        f"{history_count_before}, history_count_after={history_count_after}",
    )
    summary.record(
        step="rehistory",
        status="PASSED",
        history_count_before=history_count_before,
        history_count_after=history_count_after,
    )
    print(
        f"  ok: history still has {history_count_after} messages "
        f"(idempotent)",
        flush=True,
    )

    # ----- Step 5: Negative case A — whitespace message -------------------
    print("[5/6] Negative case A: whitespace-only message ...", flush=True)
    neg_a = _http(
        "POST",
        f"{base_url}/api/chat",
        body={"message": "   "},
        timeout_s=30.0,
    )
    _assert(
        neg_a.status == 400,
        summary,
        "negative_whitespace",
        f"expected 400 for whitespace message, got {neg_a.status} "
        f"(body: {neg_a.raw[:500]})",
    )
    summary.record(
        step="negative_whitespace",
        status="PASSED",
        http_status=neg_a.status,
    )
    print(f"  ok: 400", flush=True)

    # ----- Step 6: Negative case B — bogus execution id -------------------
    print("[6/6] Negative case B: unknown execution id ...", flush=True)
    bogus_id = f"bogus-{uuid.uuid4()}"
    neg_b = _http(
        "GET",
        f"{base_url}/api/chat/result/{quote(bogus_id)}",
        timeout_s=30.0,
    )
    _assert(
        neg_b.status == 404,
        summary,
        "negative_unknown_execution",
        f"expected 404 for unknown execution id, got {neg_b.status} "
        f"(body: {neg_b.raw[:500]})",
    )
    summary.record(
        step="negative_unknown_execution",
        status="PASSED",
        http_status=neg_b.status,
    )
    print(f"  ok: 404", flush=True)

    summary.verdict = "PASS"
    return summary


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end integration test for spec 015 cutover."
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the FastAPI backend (e.g. http://localhost:8000).",
    )
    parser.add_argument(
        "--prompt",
        default="What is the equipment status?",
        help="Prompt to send to the supervisor (single-domain happy-path).",
    )
    parser.add_argument(
        "--overall-timeout-s",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for a terminal poll status.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=2.0,
        help="Seconds between successive poll requests.",
    )
    parser.add_argument(
        "--initial-poll-delay-s",
        type=float,
        default=3.0,
        help=(
            "Seconds to sleep between the POST-202 ack and the first poll. "
            "Tolerates the short eventual-consistency window on "
            "GetDurableExecution for freshly-started durable executions "
            "(observed ~1-2s in us-west-2; a 3s default leaves headroom). "
            "Set to 0 to disable. See the 2026-05-05 finding "
            "'measure_integration.py first-poll race'."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write the JSON summary (defaults to stdout).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_measurement(args)
    except AssertionFailure as exc:
        # Assertion already recorded in summary; surface verdict.
        msg = str(exc)
        summary = _build_failed_summary(args, msg)
        _emit(summary, args.output)
        print(f"\nFAIL: {msg}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = f"unhandled error: {exc}"
        summary = _build_failed_summary(args, msg)
        _emit(summary, args.output)
        print(f"\nFAIL: {msg}", file=sys.stderr, flush=True)
        return 1

    _emit(summary, args.output)
    print("\nPASS", flush=True)
    return 0


def _build_failed_summary(args: argparse.Namespace, message: str) -> Summary:
    summary = Summary(
        started_at=_now_iso(),
        base_url=args.base_url,
        prompt=args.prompt,
    )
    summary.verdict = "FAIL"
    summary.notes.append(message)
    return summary


def _emit(summary: Summary, output_path: str | None) -> None:
    blob = json.dumps(
        {
            "started_at": summary.started_at,
            "finished_at": _now_iso(),
            "base_url": summary.base_url,
            "prompt": summary.prompt,
            "steps": summary.steps,
            "notes": summary.notes,
            "verdict": summary.verdict,
        },
        indent=2,
    )
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    print(blob, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
