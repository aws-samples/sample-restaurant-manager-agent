# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for threat-model findings TM-01 and TM-03.

TM-01 (High) -- the model must not be able to choose which store a tool call is
routed to. `execute_tool` publishes to `stores/{store_id}/commands/{domain}`, so
a model-supplied `store_id` was a cross-tenant write primitive reachable by
prompt injection. The two tool paths disagreed: the voice broker discarded the
model's value, the text path honoured it.

Three independent controls now exist, and each is tested separately, because the
point of defence in depth is that no single one is load-bearing:

  1. the tool schemas do not advertise `store_id` (the model is never offered it)
  2. `validate_params` drops it (it is not an allowed key on any tool)
  3. the text path reads the store from session config and never from params

The IAM control -- scoping the EC2 role's `iot:Publish` to this host's store
rather than `stores/*` -- is asserted in
`infra/simulator-ec2-alb/tests/test_stack.py`, since it lives in the stack.

TM-03 (Medium) -- Amazon's "Generative AI Tool Use and Agency" guidance requires
every tool parameter to be validated against its expected data type before the
tool is invoked. Only strings were checked, so an `integer` field could receive a
string, a float, or a nested object and be serialized into the store command.
"""

from __future__ import annotations

import pytest

from simulator.api.harness_tools import (
    INLINE_TOOLS,
    _PARAM_TYPES,
    _TOOL_PARAMS,
    ToolParamError,
    validate_params,
)

ALL_TOOL_NAMES = sorted(_TOOL_PARAMS)


# ---------------------------------------------------------------------------
# TM-01 control 1: the model is never offered store_id
# ---------------------------------------------------------------------------


def test_no_tool_schema_advertises_store_id() -> None:
    """A parameter the model is never shown is one it cannot be steered into."""
    offenders = [
        tool["name"]
        for tool in INLINE_TOOLS
        if "store_id"
        in (tool["config"]["inlineFunction"]["inputSchema"].get("properties") or {})
    ]
    assert not offenders, (
        f"tool schema(s) advertise store_id to the model: {offenders}. The store "
        "is session configuration, not a conversation parameter (TM-01)."
    )


# ---------------------------------------------------------------------------
# TM-01 control 2: validation drops it even if the model invents it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ALL_TOOL_NAMES)
def test_store_id_is_not_an_allowed_param_on_any_tool(tool_name: str) -> None:
    assert "store_id" not in _TOOL_PARAMS[tool_name]["allowed"], (
        f"{tool_name} allows store_id; a model-supplied value would reach topic "
        "routing (TM-01)"
    )


def test_validate_params_drops_injected_store_id() -> None:
    """The model can still *emit* store_id -- validation must discard it."""
    cleaned = validate_params(
        "get_equipment_status", {"store_id": "victim-store-042"}
    )
    assert cleaned == {}, f"store_id survived validation: {cleaned}"


def test_validate_params_drops_store_id_without_losing_real_args() -> None:
    cleaned = validate_params(
        "void_order",
        {"order_id": 7, "reason": "spill", "store_id": "victim-store-042"},
    )
    assert cleaned == {"order_id": 7, "reason": "spill"}


# ---------------------------------------------------------------------------
# TM-01 control 3: the text path ignores params entirely for store selection
# ---------------------------------------------------------------------------


def test_text_path_routes_to_the_session_store_not_the_models_choice() -> None:
    """Drive the real tool-dispatch path with an injected store_id.

    This is the control that actually failed before: `harness_backend` read
    `clean.get("store_id") or self.store_id`.
    """
    from simulator.api.harness_backend import HarnessChatBackend

    seen: dict[str, object] = {}

    def fake_executor(**kwargs):
        seen.update(kwargs)
        return {"status": "success", "result": {"ok": True}}

    backend = HarnessChatBackend(
        harness_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/x-EXAMPLE123",
        memory_id="Memory-EXAMPLE123",
        store_id="sim-store-001",
        iot_endpoint="example-ats.iot.us-west-2.amazonaws.com",
        region="us-west-2",
        tool_executor=fake_executor,
    )

    backend._build_tool_result_messages(
        {
            "id": "tu1",
            "name": "get_equipment_status",
            "input": '{"store_id": "victim-store-042"}',
        }
    )

    assert seen["store_id"] == "sim-store-001", (
        f"tool call was routed to {seen['store_id']!r} -- the model's injected "
        "store_id won, which is the cross-tenant bug TM-01 describes"
    )
    assert "store_id" not in seen["parameters"]


# ---------------------------------------------------------------------------
# TM-03: per-parameter type validation
# ---------------------------------------------------------------------------


def test_every_declared_param_has_a_known_type() -> None:
    """Guard the guard: an unknown type name would silently skip validation."""
    from simulator.api.harness_tools import _JSON_TYPE_CHECKS

    unknown = {
        f"{tool}.{param}": declared
        for tool, params in _PARAM_TYPES.items()
        for param, declared in params.items()
        if declared not in _JSON_TYPE_CHECKS
    }
    assert not unknown, (
        f"parameters declare types with no runtime check, so they are validated "
        f"by nothing: {unknown}"
    )
    # And the map must be non-empty, or every assertion below is vacuous.
    assert sum(len(p) for p in _PARAM_TYPES.values()) > 5


@pytest.mark.parametrize(
    "tool_name,params,bad_param",
    [
        ("void_order", {"order_id": "42; DROP TABLE", "reason": "r"}, "order_id"),
        ("void_order", {"order_id": 4.5, "reason": "r"}, "order_id"),
        ("void_order", {"order_id": {"$ne": None}, "reason": "r"}, "order_id"),
        ("void_order", {"order_id": [1, 2], "reason": "r"}, "order_id"),
        ("void_order", {"order_id": 1, "reason": 99}, "reason"),
        ("set_timer", {"equipment_id": "fryer_1", "minutes": "sixty"}, "minutes"),
        ("set_timer", {"equipment_id": "fryer_1", "minutes": {"x": 1}}, "minutes"),
        ("get_temperature_log", {"equipment_id": 12345}, "equipment_id"),
    ],
)
def test_wrong_parameter_type_is_rejected(
    tool_name: str, params: dict, bad_param: str
) -> None:
    with pytest.raises(ToolParamError, match=f"parameter '{bad_param}' must be"):
        validate_params(tool_name, params)


def test_booleans_are_not_accepted_as_numbers() -> None:
    """`bool` is an `int` subclass in Python; `minutes=True` is not a duration."""
    with pytest.raises(ToolParamError, match="must be number"):
        validate_params("set_timer", {"equipment_id": "fryer_1", "minutes": True})


def test_correctly_typed_parameters_still_pass() -> None:
    """The fix must not break legitimate calls -- the boring half of the test."""
    assert validate_params("void_order", {"order_id": 42, "reason": "customer"}) == {
        "order_id": 42,
        "reason": "customer",
    }
    assert validate_params(
        "set_timer", {"equipment_id": "fryer_1", "minutes": 10}
    ) == {"equipment_id": "fryer_1", "minutes": 10}
    # `number` accepts an int as well as a float.
    assert validate_params(
        "set_timer", {"equipment_id": "fryer_1", "minutes": 2.5}
    ) == {"equipment_id": "fryer_1", "minutes": 2.5}


# ---------------------------------------------------------------------------
# TM-16: audit attribution
# ---------------------------------------------------------------------------


def _run_tool_with_audit(monkeypatch, caplog, tool_name, tool_input, manager_sub):
    """Drive one tool dispatch and return the emitted audit log records."""
    import logging

    from simulator.api.harness_backend import HarnessChatBackend

    backend = HarnessChatBackend(
        harness_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/x-EXAMPLE123",
        memory_id="Memory-EXAMPLE123",
        store_id="sim-store-001",
        iot_endpoint="example-ats.iot.us-west-2.amazonaws.com",
        region="us-west-2",
        tool_executor=lambda **kw: {"status": "success", "result": {"ok": True}},
    )
    with caplog.at_level(logging.INFO, logger="simulator.api.harness_backend"):
        backend._build_tool_result_messages(
            {"id": "tu1", "name": tool_name, "input": tool_input},
            manager_sub=manager_sub,
        )
    return [r.getMessage() for r in caplog.records if "marker=tool_audit" in r.getMessage()]


def test_tool_invocation_is_attributed_to_the_manager(monkeypatch, caplog) -> None:
    """TM-16: "who voided that order?" must be answerable from the logs."""
    lines = _run_tool_with_audit(
        monkeypatch, caplog, "void_order", '{"order_id": 7, "reason": "spill"}', "sub-abc-123"
    )
    assert len(lines) == 1, f"expected exactly one audit record, got {lines}"
    line = lines[0]
    assert "manager=sub-abc-123" in line
    assert "void_order" in line
    assert "store_id=sim-store-001" in line
    assert "mutating=True" in line, "a mutating tool must be marked as such"


def test_unattributed_callers_are_labelled_not_silent(caplog) -> None:
    """The voice broker has no HTTP identity; the record must still exist."""
    lines = _run_tool_with_audit(None, caplog, "get_equipment_status", "{}", None)
    assert len(lines) == 1
    assert "manager=unattributed" in lines[0]
    assert "mutating=False" in lines[0]


def test_rejected_tool_calls_are_also_audited(caplog) -> None:
    """A validation rejection is what an injection attempt looks like from here.

    Auditing only successful calls would hide exactly the events worth reviewing.
    """
    lines = _run_tool_with_audit(
        None, caplog, "void_order", '{"order_id": "not-an-int", "reason": "r"}', "sub-abc-123"
    )
    assert len(lines) == 1
    assert "outcome=error" in lines[0]
    assert "manager=sub-abc-123" in lines[0]


def test_audit_record_does_not_log_tool_parameters(caplog) -> None:
    """Parameters are model-controlled free text; a log is a poor place to land it."""
    secretish = "PLEASE-DO-NOT-LOG-a1b2c3"
    lines = _run_tool_with_audit(
        None, caplog, "void_order", '{"order_id": 1, "reason": "%s"}' % secretish, "sub-abc-123"
    )
    assert len(lines) == 1
    assert secretish not in lines[0], "tool parameters must not reach the audit line"


def test_every_tool_is_classified_as_mutating_or_not() -> None:
    """The mutating/read partition must be total.

    Anything consuming MUTATING_TOOL_NAMES treats an unlisted tool as read-only,
    so a newly added mutating tool would be silently misclassified in the audit
    log and in any future confirmation gate (TM-02).
    """
    from simulator.api.harness_tools import KNOWN_TOOLS, MUTATING_TOOL_NAMES

    unknown = sorted(MUTATING_TOOL_NAMES - KNOWN_TOOLS)
    assert not unknown, f"MUTATING_TOOL_NAMES names tools that do not exist: {unknown}"
    # Read-only by omission is the risky direction, so spell out the expected
    # read set: a new tool forces a deliberate choice here.
    reads = KNOWN_TOOLS - MUTATING_TOOL_NAMES
    assert len(reads) + len(MUTATING_TOOL_NAMES) == len(KNOWN_TOOLS)
    for name in sorted(reads):
        assert name.startswith(("get_", "check_")), (
            f"{name!r} is treated as read-only but is not named like a read. If it "
            "changes store state, add it to MUTATING_TOOL_NAMES."
        )
