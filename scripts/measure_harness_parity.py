#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spec 022 Task 5 — live parity matrix for the harness chat backend.

Drives the REAL ``HarnessChatBackend`` (live AgentCore harness + live store via
the Correlation-A IoT round-trip) across the full query matrix and asserts
functional parity (SP1) with the retired durable layer, plus same-session
memory (SP3) and a latency table vs the durable baseline (SP5).

Run from a host with AWS creds that can ``bedrock-agentcore:InvokeHarness`` and
connect/subscribe/publish to IoT Core (a dev host with Admin, or the EC2 role).

    python scripts/measure_harness_parity.py

Exit code 0 == SP1 PASS (all classes returned live store data); non-zero == a
HARD-GATE failure (do NOT cut over).

This is a measurement/validation script — it changes no infrastructure and is
safe to re-run. The one mutating query (log_waste) writes a waste event to the
running simulator, which is the intended parity check for the mutation class.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from simulator.api import harness_tools  # noqa: E402
from simulator.api.harness_backend import HarnessChatBackend  # noqa: E402

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"
ARN_FILE = pathlib.Path(__file__).resolve().parents[1] / "infra/harness-agent/provision/.harness_arn"

#: The simulator stack's CDK context file, which holds this deployment's
#: ``iotEndpoint`` and ``agentcoreMemoryId``. It is gitignored because both are
#: account-specific, so they are resolved from there rather than hardcoded here
#: (which would duplicate the values and leak an account identifier into source).
CDK_CONTEXT_FILE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "infra/simulator-ec2-alb/cdk.context.json"
)


def _cdk_context(key: str) -> str | None:
    """Read one key from the simulator stack's gitignored cdk.context.json."""
    try:
        return json.loads(CDK_CONTEXT_FILE.read_text(encoding="utf-8")).get(key)
    except (OSError, json.JSONDecodeError):
        return None

#: Durable-layer baseline band (ADR-001 POC / spec 017), for the SP5 table.
DURABLE_BASELINE = "~12-21s (durable warm-cold)"

# Query matrix: each class must answer with LIVE store data (SP1).
MATRIX = [
    {"name": "simple-equipment", "tools": {"get_equipment_status"},
     "query": "What is the current status of all kitchen equipment?",
     "markers": ["fryer", "grill", "cooler", "freezer", "temp", "°", "350"]},
    {"name": "simple-pos", "tools": {"get_daily_sales"},
     "query": "What are the total sales so far today?", "markers": []},
    {"name": "simple-inventory", "tools": {"get_stock_levels"},
     "query": "What are the current inventory stock levels?", "markers": []},
    {"name": "simple-schedule", "tools": {"get_current_shifts"},
     "query": "Who is currently on shift right now?", "markers": []},
    {"name": "mutation-equipment", "tools": {"set_timer"},
     "query": "Set a 5 minute timer on Deep Fryer #1 (equipment id fryer_1).", "markers": []},
    {"name": "multi-domain", "tools": {"get_equipment_status", "check_reorder_status", "get_stock_levels"},
     "query": "Give me a combined status: kitchen equipment health, and which inventory items need reordering.",
     "markers": [], "min_tools": 2},
    {"name": "analytical", "tools": {"check_reorder_status", "get_stock_levels", "get_usage_report"},
     "query": "Analyze our inventory: are we at risk of running out of anything soon, and what should we reorder?",
     "markers": []},
]


class RecordingExecutor:
    """Wraps the real IoT round-trip and records every tool call + result."""

    def __init__(self, endpoint: str, region: str) -> None:
        self.endpoint = endpoint
        self.region = region
        self.calls: list[dict] = []

    def __call__(self, *, tool_name: str, parameters: dict, store_id: str, iot_endpoint: str, region: str, timeout_s: float) -> dict:
        result = harness_tools.execute_tool(
            tool_name=tool_name, parameters=parameters, store_id=store_id,
            iot_endpoint=self.endpoint, region=self.region, timeout_s=timeout_s,
        )
        self.calls.append({"tool": tool_name, "params": parameters, "status": result.get("status")})
        return result


def _poll(backend: HarnessChatBackend, exec_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = backend.get_execution_result(exec_id, None)
        if r["status"] != "in_progress":
            return r
        # Deadline-bounded poll interval, not an unconditional wait.
        # nosemgrep: arbitrary-sleep
        time.sleep(0.25)
    return {"status": "failed", "error": "measurement poll timed out"}


def run_matrix(backend: HarnessChatBackend, recorder: RecordingExecutor) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    all_pass = True
    for case in MATRIX:
        recorder.calls.clear()
        t0 = time.monotonic()
        res = backend.invoke(case["query"])
        final = _poll(backend, res.execution_id)
        elapsed = time.monotonic() - t0

        answer = (final.get("response") or "").lower() if final["status"] == "succeeded" else ""
        executed = [c for c in recorder.calls if c["tool"] in case["tools"] and c["status"] == "success"]
        distinct_ok = len({c["tool"] for c in executed}) >= case.get("min_tools", 1)
        markers_ok = (not case["markers"]) or any(m in answer for m in case["markers"])
        passed = (
            final["status"] == "succeeded"
            and bool(answer.strip())
            and bool(executed)
            and distinct_ok
            and markers_ok
        )
        all_pass = all_pass and passed
        rows.append({
            "name": case["name"],
            "status": final["status"],
            "tools": ",".join(sorted({c["tool"] for c in executed})) or "(none-success)",
            "elapsed_s": round(elapsed, 1),
            "markers_ok": markers_ok,
            "pass": passed,
            "answer": (final.get("response") or final.get("error") or "")[:160],
        })
        print(f"[{'PASS' if passed else 'FAIL'}] {case['name']:18} {elapsed:5.1f}s "
              f"status={final['status']} tools={rows[-1]['tools']} markers_ok={markers_ok}")
        print(f"        answer: {rows[-1]['answer']!r}")
    return rows, all_pass


def run_memory_check(backend: HarnessChatBackend) -> bool:
    """SP3: 2-turn same-session recall with no caller-side replay."""
    session = backend.generate_session_id()
    r1 = backend.invoke("What is the status of the deep fryers? Use the equipment tool.", session_id=session)
    _poll(backend, r1.execution_id)
    r2 = backend.invoke("Which specific fryer did you just mention? Answer with its name only.", session_id=session)
    final2 = _poll(backend, r2.execution_id)
    ans = (final2.get("response") or "").lower()
    ok = final2["status"] == "succeeded" and ("fryer" in ans)
    print(f"\n[SP3 memory] same-session recall: {'PASS' if ok else 'FAIL'} -> {ans[:120]!r}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness parity matrix (spec 022 Task 5).")
    parser.add_argument("--harness-arn", default=None)
    parser.add_argument(
        "--iot-endpoint",
        default=None,
        help="IoT ATS endpoint (default: 'iotEndpoint' from the simulator "
        "stack's gitignored cdk.context.json).",
    )
    parser.add_argument(
        "--memory-id",
        default=None,
        help="AgentCore Memory id (default: 'agentcoreMemoryId' from the "
        "simulator stack's gitignored cdk.context.json).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--store-id", default="sim-store-001")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    # Account-specific values come from the deployment's CDK context.
    for flag, ctx_key in (("iot_endpoint", "iotEndpoint"), ("memory_id", "agentcoreMemoryId")):
        if getattr(args, flag) is None:
            resolved = _cdk_context(ctx_key)
            if not resolved:
                sys.exit(
                    f"No --{flag.replace('_', '-')}: key {ctx_key!r} not found in "
                    f"{CDK_CONTEXT_FILE}. Pass it explicitly."
                )
            setattr(args, flag, resolved)

    harness_arn = args.harness_arn or (ARN_FILE.read_text().strip() if ARN_FILE.exists() else None)
    if not harness_arn:
        sys.exit("No harness ARN (pass --harness-arn or provision the harness first).")

    print(f"harness={harness_arn}\nstore={args.store_id} endpoint={args.iot_endpoint}\n")

    recorder = RecordingExecutor(args.iot_endpoint, args.region)
    backend = HarnessChatBackend(
        region=args.region,
        harness_arn=harness_arn,
        memory_id=args.memory_id,
        model_id=args.model,
        store_id=args.store_id,
        iot_endpoint=args.iot_endpoint,
        max_turns=10,
        tool_timeout_s=30.0,
        tool_executor=recorder,
    )

    print("=== SP1 parity matrix ===")
    rows, sp1_pass = run_matrix(backend, recorder)
    sp3_pass = run_memory_check(backend)

    print("\n=== SP5 latency table (vs durable baseline " + DURABLE_BASELINE + ") ===")
    for r in rows:
        print(f"  {r['name']:18} {r['elapsed_s']:5.1f}s")

    print("\n=== VERDICT ===")
    print(f"  SP1 (functional parity): {'PASS' if sp1_pass else 'FAIL'}")
    print(f"  SP3 (same-session memory): {'PASS' if sp3_pass else 'FAIL'}")
    print(f"  SP1 is the HARD GATE: {'cutover may proceed' if sp1_pass else 'DO NOT CUT OVER'}")
    sys.exit(0 if sp1_pass else 1)


if __name__ == "__main__":
    main()
