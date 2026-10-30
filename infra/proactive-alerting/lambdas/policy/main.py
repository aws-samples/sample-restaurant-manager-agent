# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Alert-policy evaluation (spec 023 Task 5).

Invoked by the Step Functions orchestrator with the EventBridge ``AlertRaised``
event. Decides whether to push a proactive notification:

- **severity gate**: ``info`` -> inbox only (no push); ``warning``/``critical`` -> notify.
- **quiet hours** (configurable): suppress a ``warning`` during quiet hours...
- **critical override**: ...but ``critical`` always notifies.

Dedup is enforced upstream at ingest (Task 4), so each ``AlertRaised`` is already
a distinct alert -- no dedup step is needed here. This is a *pure decision*: it
returns the decision plus the composed notification payload; the ``Notify`` state
performs the publish (so Task 8's bounded re-surface can re-enter ``Notify``).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

QUIET_HOURS_START_HOUR = int(os.environ.get("QUIET_HOURS_START_HOUR", "22"))
QUIET_HOURS_END_HOUR = int(os.environ.get("QUIET_HOURS_END_HOUR", "6"))

# Acknowledgment SLA per severity (spec 023 Task 8): the Notify waitForTaskToken
# timeout. After it elapses with no ack, the state machine re-surfaces on the same
# display up to MAX times, then expires (#4 -- no routing to any other person/surface).
SLA_CRITICAL_SECONDS = int(os.environ.get("SLA_CRITICAL_SECONDS", "300"))  # 5 min
SLA_WARNING_SECONDS = int(os.environ.get("SLA_WARNING_SECONDS", "1800"))  # 30 min


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False  # quiet hours disabled
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps midnight


def _event_hour(detail: dict) -> int:
    ts = (detail.get("envelope") or {}).get("timestamp") or detail.get("timestamp") or ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc).hour


def evaluate_policy(detail: dict, hour: int) -> dict:
    """Pure decision: returns {notify: bool, action: str, reason: str}."""
    severity = detail.get("severity")
    if severity == "info":
        return {"notify": False, "action": "inbox_only", "reason": "info_severity"}
    quiet = _in_quiet_hours(hour, QUIET_HOURS_START_HOUR, QUIET_HOURS_END_HOUR)
    if quiet and severity != "critical":
        return {"notify": False, "action": "suppressed_quiet_hours", "reason": "quiet_hours"}
    reason = "critical_override" if (quiet and severity == "critical") else "severity_gate"
    return {"notify": True, "action": "notify", "reason": reason}


def _compose_message(detail: dict) -> str:
    sev = str(detail.get("severity", "")).title()
    return f"{sev} alert: {detail.get('condition')} on {detail.get('source_id')}"


def _sla_seconds(severity: str | None) -> int:
    """Ack-wait (waitForTaskToken) timeout by severity (spec 023 Task 8)."""
    return SLA_CRITICAL_SECONDS if severity == "critical" else SLA_WARNING_SECONDS


def handler(event: dict, _context) -> dict:
    # EventBridge -> Step Functions delivers the full event; the alert is under "detail".
    detail = event.get("detail", event)
    decision = evaluate_policy(detail, _event_hour(detail))
    notification = {
        "alert_id": detail.get("alert_id"),
        "store_id": detail.get("store_id"),
        "severity": detail.get("severity"),
        "domain": detail.get("domain"),
        "source_id": detail.get("source_id"),
        "condition": detail.get("condition"),
        "dedup_key": detail.get("dedup_key"),
        "message": _compose_message(detail),
        "reading": (detail.get("envelope") or {}).get("reading"),
        "timestamp": (detail.get("envelope") or {}).get("timestamp"),
    }
    return {
        "store_id": detail.get("store_id"),
        "alert_id": detail.get("alert_id"),
        "decision": decision,
        "notification": notification,
        "sla_seconds": _sla_seconds(detail.get("severity")),
    }
