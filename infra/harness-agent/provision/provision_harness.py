# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision (or tear down) the AgentCore Managed Harness for the agent layer.

Spec 022 Task 1. The harness has no stable CloudFormation construct at GA, so
it is created out-of-band against the ``bedrock-agentcore-control`` data plane,
referencing the execution role created by ``HarnessAgentStack``.

Idempotent: re-running ``provision`` reuses an existing harness whose name
matches (the service appends a ``-<10>`` suffix to ``harnessName``), so this is
safe to run repeatedly. The resolved harness ARN is written to
``provision/.harness_arn`` and printed.

Design choices baked in (spec 022 design.md):
  - OD2: a SINGLE harness exposes all store tools. Tools are passed at INVOKE
    time (runtime override), so the harness resource here is minimal
    (name + role + memory + model + limits + system prompt).
  - OD3: the existing AgentCore Memory resource is attached for automatic
    same-session continuity; cross-session recall is deferred.

Usage:
    python provision/provision_harness.py \\
        --role-arn arn:aws:iam::<ACCOUNT_ID>:role/restaurant-harness-agent-exec-role
    python provision/provision_harness.py --teardown

The role ARN defaults to the ``executionRoleName`` convention in the caller's
account (resolved via STS) but SHOULD be passed explicitly from the
``HarnessExecutionRoleArn`` stack output after ``cdk deploy``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import boto3

REGION = "us-west-2"
HARNESS_NAME = "restaurant_manager_agent"  # [a-zA-Z][a-zA-Z0-9_]{0,39}; service appends -<10>
ROLE_NAME = "restaurant-harness-agent-exec-role"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
ARN_FILE = pathlib.Path(__file__).with_name(".harness_arn")

#: The CDK context file the companion stack reads ``memoryArn`` from. It is
#: gitignored because the value is account-specific, so this script resolves the
#: same value from the same place rather than carrying a second copy that could
#: drift (or leak an account id into source).
CDK_CONTEXT_FILE = pathlib.Path(__file__).resolve().parents[1] / "cdk.context.json"


def _resolve_account() -> str:
    """Return the caller's AWS account id.

    Derived at runtime instead of hardcoded so no account identifier lives in
    source and the script follows whichever credentials are in effect.
    """
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def _default_role_arn() -> str:
    return f"arn:aws:iam::{_resolve_account()}:role/{ROLE_NAME}"


def _default_memory_arn() -> str | None:
    """Read ``memoryArn`` from the stack's gitignored cdk.context.json, if present."""
    try:
        return json.loads(CDK_CONTEXT_FILE.read_text(encoding="utf-8")).get("memoryArn")
    except (OSError, json.JSONDecodeError):
        return None

#: Minimal system prompt establishing the assistant role. The full tool catalog
#: and analytical/routing guidance are supplied at INVOKE time by the backend
#: (spec 022 Task 2), per the runtime-override design.
SYSTEM_PROMPT = [
    {
        "text": (
            "You are a restaurant manager assistant. You help restaurant "
            "operators get information and take actions on their store's "
            "operations (point of sale, kitchen equipment, inventory, and "
            "staff scheduling). The default store is sim-store-001 unless the "
            "user specifies otherwise. Always base answers on tool results; "
            "never speculate about data you have not retrieved. If a tool "
            "result is flagged stale or cached, say so and include the "
            "timestamp. Give concise, well-organized answers."
        )
    }
]


def _client():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _assert_operation(client, op: str) -> None:
    """Fail fast with a clear message if the GA service model lacks ``op``."""
    available = set(client.meta.service_model.operation_names)
    if op not in available:
        sys.exit(
            f"Operation {op!r} not in the bedrock-agentcore-control service "
            f"model (boto3 {boto3.__version__}). Upgrade boto3 (>=1.43.30) or "
            f"re-validate the GA API. Available: {sorted(available)}"
        )


def _find_existing(client) -> tuple[str | None, str | None]:
    """Return (harnessId, harnessArn) of an existing harness matching our name.

    The service appends a ``-<10>`` suffix to ``harnessName``, so we match on
    the base name as a prefix. Returns (None, None) if none found or if the
    list operation is unavailable.
    """
    try:
        _assert_operation(client, "ListHarnesses")
    except SystemExit:
        return None, None
    paginator_names = {p for p in client.meta.service_model.operation_names}
    next_token = None
    while True:
        kwargs = {"nextToken": next_token} if next_token else {}
        resp = client.list_harnesses(**kwargs)
        for item in resp.get("harnesses", resp.get("harnessSummaries", [])):
            name = item.get("harnessName") or item.get("name") or ""
            # Skip a harness that is mid-delete: reusing it leads to a
            # ResourceNotFound race in the READY poll (observed at GA when
            # re-provisioning right after --teardown).
            status = (item.get("status") or "").upper()
            if status in ("DELETING", "DELETE_IN_PROGRESS"):
                continue
            if name == HARNESS_NAME or name.startswith(f"{HARNESS_NAME}-"):
                hid = item.get("harnessId") or item.get("name") or name
                arn = item.get("harnessArn") or item.get("arn")
                return hid, arn
        next_token = resp.get("nextToken")
        if not next_token:
            return None, None


def _wait_ready(client, harness_id: str, fallback_arn: str | None) -> str:
    for _ in range(60):
        g = client.get_harness(harnessId=harness_id)
        h = g.get("harness", g)
        status = h.get("status")
        arn = h.get("harnessArn") or h.get("arn") or fallback_arn
        if status == "READY":
            print(f"READY: {arn}")
            if arn:
                ARN_FILE.write_text(arn)
            return arn or ""
        if status in ("CREATE_FAILED", "FAILED"):
            sys.exit(f"FAILED status={status}: {json.dumps(h, default=str)[:400]}")
        # Harness creation is asynchronous; this polls GetHarness until READY.
        # nosemgrep: arbitrary-sleep
        time.sleep(5)
    sys.exit("timed out waiting for harness READY")


def provision(args: argparse.Namespace) -> None:
    client = _client()
    _assert_operation(client, "CreateHarness")
    _assert_operation(client, "GetHarness")

    existing_id, existing_arn = _find_existing(client)
    if existing_id:
        print(f"reusing existing harness id={existing_id} arn={existing_arn}")
        _wait_ready(client, existing_id, existing_arn)
        return

    resp = client.create_harness(
        harnessName=HARNESS_NAME,
        executionRoleArn=args.role_arn,
        systemPrompt=SYSTEM_PROMPT,
        model={"bedrockModelConfig": {"modelId": args.model_id}},
        memory={"agentCoreMemoryConfiguration": {"arn": args.memory_arn}},
        maxIterations=args.max_iterations,
        timeoutSeconds=args.timeout_seconds,
    )
    harness = resp.get("harness", resp)
    harness_id = harness.get("harnessId") or harness.get("name") or HARNESS_NAME
    arn = harness.get("harnessArn") or harness.get("arn")
    print(
        "create_harness ->",
        json.dumps(
            {k: harness.get(k) for k in ("harnessId", "harnessArn", "arn", "status")},
            default=str,
        ),
    )
    _wait_ready(client, harness_id, arn)


def teardown(_args: argparse.Namespace) -> None:
    client = _client()
    _assert_operation(client, "DeleteHarness")
    existing_id, existing_arn = _find_existing(client)
    if not existing_id:
        print("no matching harness to delete")
        ARN_FILE.unlink(missing_ok=True)
        return
    client.delete_harness(harnessId=existing_id)
    print(f"delete_harness requested for id={existing_id} arn={existing_arn}")
    ARN_FILE.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision/tear down the agent-layer harness.")
    parser.add_argument(
        "--role-arn",
        default=None,
        help="Harness execution role ARN (default: the HarnessExecutionRoleArn "
        f"convention {ROLE_NAME!r} in the caller's account).",
    )
    parser.add_argument(
        "--memory-arn",
        default=None,
        help="AgentCore Memory ARN (default: 'memoryArn' from the stack's "
        "gitignored cdk.context.json).",
    )
    parser.add_argument("--model", dest="model_id", default=DEFAULT_MODEL_ID, help="Default model id.")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--teardown", action="store_true", help="Delete the harness instead of creating it.")
    args = parser.parse_args()

    if args.teardown:
        teardown(args)
        return

    # Resolve the account-derived defaults lazily, so teardown needs no STS call
    # and a fully-specified invocation never depends on ambient context.
    if args.role_arn is None:
        args.role_arn = _default_role_arn()
    if args.memory_arn is None:
        args.memory_arn = _default_memory_arn()
        if not args.memory_arn:
            parser.error(
                "--memory-arn is required: no 'memoryArn' key was found in "
                f"{CDK_CONTEXT_FILE}. Pass the AgentCore Memory ARN explicitly, "
                "or add it to that (gitignored) file alongside the stack's own "
                "context value."
            )
    provision(args)


if __name__ == "__main__":
    main()
