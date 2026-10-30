# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the alert-policy evaluation Lambda (spec 023 Task 5)."""
import importlib.util
import os

os.environ["QUIET_HOURS_START_HOUR"] = "22"  # set before module load (read at import)
os.environ["QUIET_HOURS_END_HOUR"] = "6"

_PATH = os.path.join(os.path.dirname(__file__), "..", "lambdas", "policy", "main.py")


def _load():
    spec = importlib.util.spec_from_file_location("policy_main_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


policy = _load()


def _detail(severity="critical", condition="compressor_fault", source="fryer-1",
            ts="2026-06-24T14:00:00Z"):
    return {
        "alert_id": "a1", "store_id": "sim-store-001", "domain": "equipment",
        "severity": severity, "source_id": source, "condition": condition,
        "dedup_key": f"sim-store-001#equipment#{source}#{condition}",
        "envelope": {"timestamp": ts, "reading": {"current_temp": 410.0}},
    }


def test_info_is_inbox_only():
    d = policy.evaluate_policy(_detail(severity="info"), hour=14)
    assert d["notify"] is False and d["action"] == "inbox_only"


def test_warning_daytime_notifies():
    d = policy.evaluate_policy(_detail(severity="warning"), hour=14)
    assert d["notify"] is True and d["reason"] == "severity_gate"


def test_warning_in_quiet_hours_is_suppressed():
    d = policy.evaluate_policy(_detail(severity="warning"), hour=2)
    assert d["notify"] is False and d["action"] == "suppressed_quiet_hours"


def test_critical_in_quiet_hours_overrides():
    d = policy.evaluate_policy(_detail(severity="critical"), hour=2)
    assert d["notify"] is True and d["reason"] == "critical_override"


def test_critical_daytime_notifies():
    d = policy.evaluate_policy(_detail(severity="critical"), hour=14)
    assert d["notify"] is True and d["reason"] == "severity_gate"


def test_quiet_hours_wrap_midnight_and_disabled():
    assert policy._in_quiet_hours(23, 22, 6) is True
    assert policy._in_quiet_hours(5, 22, 6) is True
    assert policy._in_quiet_hours(12, 22, 6) is False
    assert policy._in_quiet_hours(3, 0, 0) is False  # disabled when start == end


def test_handler_composes_notification_from_detail():
    out = policy.handler({"detail": _detail(severity="critical")}, None)
    assert out["store_id"] == "sim-store-001" and out["alert_id"] == "a1"
    assert out["decision"]["notify"] is True
    n = out["notification"]
    assert n["source_id"] == "fryer-1" and n["condition"] == "compressor_fault"
    assert "Critical alert" in n["message"]
    assert n["reading"]["current_temp"] == 410.0


def test_sla_seconds_by_severity():
    assert policy._sla_seconds("critical") == 300   # 5 min default
    assert policy._sla_seconds("warning") == 1800   # 30 min default
    assert policy._sla_seconds("info") == 1800       # non-critical default


def test_handler_emits_per_severity_sla_seconds():
    assert policy.handler({"detail": _detail(severity="critical")}, None)["sla_seconds"] == 300
    assert policy.handler({"detail": _detail(severity="warning")}, None)["sla_seconds"] == 1800
