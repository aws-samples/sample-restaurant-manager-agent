# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Harness inline-tool support for the AgentCore agent layer (spec 022).

This module is used only by the harness backend (``AGENT_BACKEND=harness``).
It provides:

  - ``INLINE_TOOLS`` — the 18 store tools as AgentCore inline-function tool
    specs, passed to ``invoke_harness`` at invoke time (R1.4 / R1.5).
  - ``validate_params`` — parameter validation/sanitization (R2.4): drop
    unknown keys, reject oversized/control-char strings, require non-empty
    mutating-tool fields.
  - ``sanitize_result`` — strip credential/PII-looking keys from tool results
    and bound their size (R2.4).
  - ``execute_tool`` — the Correlation-A round-trip: publish the command to
    ``stores/{id}/commands/{domain}`` via the connection-less ``iot-data``
    data plane and block (bounded timeout) on a **pooled**, SigV4
    MQTT-over-WebSocket subscription to ``stores/{id}/responses/+``, routing
    each reply to its correlation-id waiter. The MQTT connection and the
    ``iot-data`` client are established once and **reused across tool calls**
    (no per-call TLS/WebSocket handshake — see AGENTPERF06). Outbound only —
    the store keeps its single outbound connection; no inbound path is created
    (SP2). ``awsiot``/``awscrt``/``boto3`` are imported lazily (inside the
    transport) so this module imports cleanly in CI without the extension.
  - ``_emit_tool_metric`` — best-effort per-tool CloudWatch telemetry (latency,
    invocations-by-outcome, timeouts, store duration), gated on
    ``TOOL_METRICS_NAMESPACE`` and run off the critical path (AGENTPERF06).

The tool name -> domain routing reuses ``simulator.mcp.registry.DOMAIN_OF_TOOL``
as the single source of truth; this module never hardcodes the mapping.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from simulator.mcp.registry import DOMAIN_OF_TOOL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation limits (R2.4)
# ---------------------------------------------------------------------------

#: Maximum serialized size of a tool's parameter object.
MAX_PARAM_JSON_BYTES = 4096
#: Maximum length of any single string parameter value.
MAX_STRING_LEN = 1024
#: Control characters (except common whitespace) are rejected outright.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Keys whose values are dropped from tool results before returning to the
#: model (defense in depth; store results should never contain these).
_SENSITIVE_KEY = re.compile(
    r"(password|secret|token|credential|api[_-]?key|private[_-]?key|authorization)",
    re.IGNORECASE,
)
#: Maximum length of any string value in a (sanitized) tool result.
MAX_RESULT_STRING_LEN = 8192

#: JSON-schema type name -> accepted Python types, for per-parameter validation
#: (threat model TM-03).
#:
#: Amazon's "Generative AI Tool Use and Agency" guidance requires that "all
#: GenAI output should be validated for expected data types ... for each
#: parameter of a tool prior to invoking the tool". Before this, only strings
#: were checked (length + control characters), so an ``integer`` parameter could
#: receive a string, a float, or a nested object, which was then serialized into
#: the MQTT command and interpreted by the store.
#:
#: ``bool`` is excluded from ``integer``/``number`` even though it is an ``int``
#: subclass in Python -- ``{"minutes": true}`` is not a duration, and letting it
#: through is the same class of bug as ``ConsumptionEvent.order_id`` being typed
#: ``str`` while populated from an ``int``.
_JSON_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


# ---------------------------------------------------------------------------
# Per-tool parameter contracts (allowed keys + required keys)
# ---------------------------------------------------------------------------
#
# Mirrors the durable tools.py signatures.
#
# ``store_id`` is DELIBERATELY NOT an allowed parameter on any tool (threat
# model TM-01). It used to be allowed on every tool and defaulted to the
# backend's configured store, which meant the MODEL could name the store a tool
# call was routed to -- and `execute_tool` publishes to
# ``stores/{store_id}/commands/{domain}``. A prompt-injected model could
# therefore aim a tool call at another tenant's store, and the EC2 role's
# ``iot:Publish`` grant did not stop it.
#
# The store a request belongs to is a property of the SESSION, not of the
# conversation: it comes from server-side configuration, exactly as the
# authenticated IoT topic (not the message body) determines the tenant on the
# store -> cloud path. A parameter the model is never offered and would have
# dropped anyway is one it cannot inject. This empty set is what makes every
# ``_BASE_ALLOWED | {...}`` expression below grant only the tool's real
# arguments.
_BASE_ALLOWED: frozenset[str] = frozenset()

_TOOL_PARAMS: dict[str, dict[str, set[str]]] = {
    # POS
    "get_daily_sales": {"allowed": _BASE_ALLOWED, "required": set()},
    "get_active_orders": {"allowed": _BASE_ALLOWED, "required": set()},
    "get_transaction_history": {"allowed": _BASE_ALLOWED | {"status_filter", "limit"}, "required": set()},
    "void_order": {"allowed": _BASE_ALLOWED | {"order_id", "reason"}, "required": {"order_id", "reason"}},
    # Equipment
    "get_equipment_status": {"allowed": _BASE_ALLOWED, "required": set()},
    "get_temperature_log": {"allowed": _BASE_ALLOWED | {"equipment_id", "limit"}, "required": {"equipment_id"}},
    "set_timer": {
        "allowed": _BASE_ALLOWED | {"equipment_id", "minutes"},
        "required": {"equipment_id", "minutes"},
    },
    "acknowledge_alert": {"allowed": _BASE_ALLOWED | {"equipment_id"}, "required": {"equipment_id"}},
    "acknowledge_proactive_alert": {"allowed": _BASE_ALLOWED | {"alert_id"}, "required": {"alert_id"}},
    "get_active_alerts": {"allowed": _BASE_ALLOWED, "required": set()},
    # Inventory
    "get_stock_levels": {"allowed": _BASE_ALLOWED, "required": set()},
    "log_waste": {
        "allowed": _BASE_ALLOWED | {"item_id", "quantity", "reason"},
        "required": {"item_id", "quantity", "reason"},
    },
    "get_usage_report": {
        "allowed": _BASE_ALLOWED | {"start_time", "end_time"},
        "required": set(),
    },
    "check_reorder_status": {"allowed": _BASE_ALLOWED, "required": set()},
    # Schedule
    "get_current_shifts": {"allowed": _BASE_ALLOWED, "required": set()},
    "get_labor_cost": {"allowed": _BASE_ALLOWED, "required": set()},
    "clock_in": {"allowed": _BASE_ALLOWED | {"employee_id"}, "required": {"employee_id"}},
    "clock_out": {"allowed": _BASE_ALLOWED | {"employee_id"}, "required": {"employee_id"}},
}


# ---------------------------------------------------------------------------
# Inline tool specs (R1.4) — passed to invoke_harness at invoke time
# ---------------------------------------------------------------------------


def _inline(name: str, description: str, properties: dict[str, dict], required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {
        "type": "inline_function",
        "name": name,
        "config": {"inlineFunction": {"description": description, "inputSchema": schema}},
    }


#: Properties every tool schema starts from. Empty, deliberately: this used to
#: advertise ``store_id`` to the model on all 18 tools (threat model TM-01). The
#: session's store is server-side configuration, so offering it as a tool
#: parameter invited a prompt-injected model to route a call at another tenant.
#: Kept as a named constant rather than inlining ``{}`` so the reason travels
#: with every ``_inline(...)`` call site below.
_BASE_PROPS: dict[str, dict] = {}

INLINE_TOOLS: list[dict] = [
    # POS
    _inline(
        "get_daily_sales",
        # semgrep false positive: intentional implicit concatenation: one string split across lines, not a missing comma.
        # nosemgrep: string-concat-in-list
        "Get today's sales summary: total revenue, total completed orders (order_count), average "
        "order value, voids, and hourly breakdown. Use this for revenue, sales, or 'how many / total "
        "orders today' -- these are the dashboard's headline Revenue and Orders figures.",
        dict(_BASE_PROPS),
    ),
    _inline(
        "get_active_orders",
        # nosemgrep: string-concat-in-list
        "Get only the orders currently IN PROGRESS (not yet completed) -- the live active-orders "
        "queue (the dashboard's 'Active' count). For totals, revenue, or how many orders today, use "
        "get_daily_sales instead.",
        dict(_BASE_PROPS),
    ),
    _inline(
        "get_transaction_history",
        "Get recent transaction history (orders).",
        {
            **_BASE_PROPS,
            "status_filter": {"type": "string", "description": "Optional order status to filter by."},
            "limit": {"type": "integer", "description": "Max rows (default 50)."},
        },
    ),
    _inline(
        "void_order",
        "Void an existing order by its numeric order id.",
        {
            **_BASE_PROPS,
            "order_id": {"type": "integer", "description": "Numeric id of the order to void."},
            "reason": {"type": "string", "description": "Reason for voiding the order."},
        },
        required=["order_id", "reason"],
    ),
    # Equipment
    _inline("get_equipment_status", "Get current status of all kitchen equipment.", dict(_BASE_PROPS)),
    _inline(
        "get_temperature_log",
        "Get temperature history for a specific piece of equipment.",
        {
            **_BASE_PROPS,
            "equipment_id": {"type": "string", "description": "Equipment id, e.g. fryer_1."},
            "limit": {"type": "integer", "description": "Max history rows (default 100)."},
        },
        required=["equipment_id"],
    ),
    _inline(
        "set_timer",
        "Set a timer on a piece of equipment.",
        {
            **_BASE_PROPS,
            "equipment_id": {"type": "string", "description": "Equipment id, e.g. fryer_1."},
            "minutes": {"type": "number", "description": "Timer duration in minutes."},
        },
        required=["equipment_id", "minutes"],
    ),
    _inline(
        "acknowledge_alert",
        # nosemgrep: string-concat-in-list
        "Clear the active alert on a piece of equipment, by equipment_id (e.g. fryer_1). This only "
        "flips the equipment's local alert flag. To acknowledge a PROACTIVE alert that was pushed to "
        "the alert inbox (identified by an alert_id like "
        "'equipment-freezer_1-thermostat_malfunction-...'), use acknowledge_proactive_alert instead.",
        {**_BASE_PROPS, "equipment_id": {"type": "string", "description": "Equipment id whose active alert to clear."}},
        required=["equipment_id"],
    ),
    _inline(
        "acknowledge_proactive_alert",
        # nosemgrep: string-concat-in-list
        "Acknowledge a PROACTIVE alert (one the system pushed to the alert inbox / notified the "
        "manager about) by its alert_id, e.g. 'equipment-freezer_1-thermostat_malfunction-1980366'. "
        "Resumes the cloud alerting workflow and marks the inbox entry acknowledged. Use this for "
        "alerts shown in the inbox or that you were asked to acknowledge.",
        {**_BASE_PROPS, "alert_id": {"type": "string", "description": "The proactive alert's id (from the inbox/notification)."}},
        required=["alert_id"],
    ),
    _inline(
        "get_active_alerts",
        # nosemgrep: string-concat-in-list
        "List unacknowledged PROACTIVE alerts in the alert inbox -- cloud-raised alerts the system "
        "pushed / notified the manager about (e.g. shown in the on-display alert banner). Use this to "
        "answer 'are there any alerts?' or 'what needs attention?'. Distinct from get_equipment_status "
        "(per-equipment live temperature/status); acknowledge one with acknowledge_proactive_alert.",
        dict(_BASE_PROPS),
    ),
    # Inventory
    _inline("get_stock_levels", "Get current stock levels for all items.", dict(_BASE_PROPS)),
    _inline(
        "log_waste",
        "Log a waste event for an inventory item (deducts from stock).",
        {
            **_BASE_PROPS,
            "item_id": {"type": "string", "description": "Inventory item id (from get_stock_levels)."},
            "quantity": {"type": "number", "description": "Quantity wasted (> 0)."},
            "reason": {
                "type": "string",
                "enum": ["contamination", "expired", "overproduction", "spillage"],
                "description": "Waste reason.",
            },
        },
        required=["item_id", "quantity", "reason"],
    ),
    _inline(
        "get_usage_report",
        "Get inventory usage/consumption report. Pass start_time/end_time (ISO 8601) for a range.",
        {
            **_BASE_PROPS,
            "start_time": {"type": "string", "description": "ISO 8601 start."},
            "end_time": {"type": "string", "description": "ISO 8601 end."},
        },
    ),
    _inline("check_reorder_status", "Check which inventory items need reordering.", dict(_BASE_PROPS)),
    # Schedule
    _inline("get_current_shifts", "Get the current shift schedule.", dict(_BASE_PROPS)),
    _inline("get_labor_cost", "Get current labor cost summary.", dict(_BASE_PROPS)),
    _inline(
        "clock_in",
        "Clock in a staff member.",
        {**_BASE_PROPS, "employee_id": {"type": "string", "description": "Employee id."}},
        required=["employee_id"],
    ),
    _inline(
        "clock_out",
        "Clock out a staff member.",
        {**_BASE_PROPS, "employee_id": {"type": "string", "description": "Employee id."}},
        required=["employee_id"],
    ),
]

#: Tool names the harness backend will accept (single source: the registry).
KNOWN_TOOLS = frozenset(DOMAIN_OF_TOOL)

#: tool name -> {parameter name -> declared JSON-schema type}, derived from
#: ``INLINE_TOOLS`` so the types enforced at runtime are exactly the types
#: advertised to the model. Deriving rather than restating means the contract
#: cannot drift: if a tool's schema changes, validation follows automatically.
_PARAM_TYPES: dict[str, dict[str, str]] = {
    tool["name"]: {
        prop: spec["type"]
        for prop, spec in (
            tool["config"]["inlineFunction"]["inputSchema"].get("properties") or {}
        ).items()
        if isinstance(spec, dict) and isinstance(spec.get("type"), str)
    }
    for tool in INLINE_TOOLS
}


#: Tools that CHANGE store state. Named explicitly rather than derived, because
#: "mutating" is a property of what the tool does at the store, not of its
#: parameter shape -- `get_temperature_log` takes arguments and mutates nothing,
#: while `clock_in` takes one and does. Single source of truth so the audit log
#: (TM-16) and any future confirmation gate for mutating tools (TM-02) cannot
#: disagree about which tools are dangerous.
#:
#: A tool added to the registry but not classified here is treated as read-only
#: by anything consuming this set, so `test_every_tool_is_classified` asserts the
#: partition is total.
MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "void_order",
        "set_timer",
        "acknowledge_alert",
        "acknowledge_proactive_alert",
        "log_waste",
        "clock_in",
        "clock_out",
    }
)


#: Tools that require an explicit HUMAN confirmation before they execute
#: (spec 031, threat model TM-02). A strict subset of MUTATING_TOOL_NAMES.
#:
#: Deliberately NOT all seven mutating tools. `set_timer`, `clock_in`, `clock_out`
#: and both `acknowledge_*` tools are trivially reversible and FREQUENT -- gating
#: them would mean confirming a fryer timer twenty times a shift, and a prompt
#: that is always approved reflexively is worse than no prompt, because it trains
#: the reflex that makes the `void_order` prompt ineffective too.
#:
#: `void_order` destroys a sale record; `log_waste` writes an awkward-to-reverse
#: inventory adjustment. Both are rare. That is ~95% of the risk at a fraction of
#: the friction. (Note `set_timer` is already protected differently: the store
#: rejects `minutes <= 0`, because semantics belong where the meaning is known.
#: Intent belongs with the human; a bad timer is visible and reversible.)
CONFIRMATION_REQUIRED_TOOLS: frozenset[str] = frozenset({"void_order", "log_waste"})


#: Cacheable reads (spec 026): read-only AND parameterless (only the optional
#: ``store_id``, no required fields) — a single canonical result per
#: (store, tool), so the ``StoreStateCache`` last-known-state fallback is
#: correct. Mutating tools and parameterized reads are excluded by construction
#: (they have required fields and/or extra allowed keys). Single source of truth
#: for BOTH the write-through and the read fallback so they cannot drift.
CACHEABLE_READ_TOOLS = frozenset(
    name
    for name, spec in _TOOL_PARAMS.items()
    if spec["allowed"] == _BASE_ALLOWED and not spec["required"]
)


# ---------------------------------------------------------------------------
# Text tool-catalog filter (spec 027, AGENTPERF06 #2) — OPTIONAL, default OFF.
#
# Offer the harness text path a relevant subset of tools instead of all 18,
# keeping selection accuracy high as the catalog grows. Gated on
# ``TOOL_CATALOG_FILTER_ENABLED`` (default OFF -> returns INLINE_TOOLS
# unchanged). When enabled, a domain-keyword heuristic picks the matched store
# domains' tools plus a small core set; an ambiguous request falls back to the
# FULL catalog so a needed tool is never silently dropped. (The voice path has
# its own VOICE_TOOL_NAMES curation and is unaffected.)
# ---------------------------------------------------------------------------

#: Always offered when the filter is on (cross-cutting "what needs attention").
_CORE_TOOL_NAMES = frozenset({"get_active_alerts", "acknowledge_proactive_alert"})

#: Case-insensitive substring keywords -> store domain. Generous on purpose;
#: tune from real queries. No semantic signal in v1 (see spec 027).
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pos": ("sale", "revenue", "order", "transaction", "void", "sell", "checkout", "money", "ticket"),
    "equipment": (
        "equipment", "temp", "fryer", "freezer", "cooler", "fridge", "oven", "grill",
        "timer", "alarm", "alert", "sensor", "maintenance", "broke", "degree",
    ),
    "inventory": (
        "inventory", "stock", "item", "waste", "reorder", "supply", "ingredient",
        "low on", "running out", "produce",
    ),
    "schedule": (
        "shift", "staff", "employee", "clock", "labor", "labour", "schedule",
        "worker", "hours", "who is working", "who's working",
    ),
}


def _catalog_filter_enabled() -> bool:
    return os.environ.get("TOOL_CATALOG_FILTER_ENABLED", "").strip().lower() in ("1", "true", "yes")


def select_inline_tools(message: str) -> list[dict]:
    """Choose which inline tools to offer the harness for one user turn.

    Default OFF (``TOOL_CATALOG_FILTER_ENABLED`` unset) returns ``INLINE_TOOLS``
    unchanged. When enabled, returns the matched store domains' tools plus the
    core set; an ambiguous request (no domain keyword matched) or a too-small
    result falls back to the full catalog so a needed tool is never dropped
    (spec 027 / AGENTPERF06 #2). Pure + deterministic.
    """
    if not _catalog_filter_enabled():
        return INLINE_TOOLS
    text = (message or "").lower()
    matched = {d for d, kws in _DOMAIN_KEYWORDS.items() if any(k in text for k in kws)}
    if not matched:
        return INLINE_TOOLS  # ambiguous -> full catalog (no capability loss)
    keep = set(_CORE_TOOL_NAMES) | {
        name for name, domain in DOMAIN_OF_TOOL.items() if domain in matched
    }
    selected = [t for t in INLINE_TOOLS if t["name"] in keep]
    # Defensive floor: never offer fewer than the core set; fall back to all.
    if len(selected) < len(_CORE_TOOL_NAMES):
        return INLINE_TOOLS
    return selected


# ---------------------------------------------------------------------------
# Validation + sanitization (R2.4)
# ---------------------------------------------------------------------------


class ToolParamError(ValueError):
    """Raised when tool parameters fail validation."""


def validate_params(tool_name: str, params: Any) -> dict:
    """Validate and sanitize model-supplied tool parameters.

    Returns a cleaned ``dict`` (unknown keys dropped). Raises
    :class:`ToolParamError` on an unknown tool, oversized payload,
    control-char/oversized string, or a missing required field.
    """
    if tool_name not in _TOOL_PARAMS:
        raise ToolParamError(f"unknown tool: {tool_name!r}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolParamError("parameters must be an object")

    serialized = json.dumps(params, default=str)
    if len(serialized.encode("utf-8")) > MAX_PARAM_JSON_BYTES:
        raise ToolParamError("parameters too large")

    spec = _TOOL_PARAMS[tool_name]
    types = _PARAM_TYPES.get(tool_name, {})
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if key not in spec["allowed"]:
            # Drop unknown keys silently (defense against injected params).
            # `store_id` lands here by design -- see TM-01 above.
            continue
        # TM-03: enforce the declared type before the value can reach the store.
        # Rejecting is the right call rather than coercing: a model that sent
        # {"minutes": "sixty"} did not mean 60, and guessing would turn a
        # detectable injection attempt into a silently wrong store action.
        expected = types.get(key)
        if expected is not None:
            accepted = _JSON_TYPE_CHECKS.get(expected)
            if accepted is not None:
                bad_bool = isinstance(value, bool) and expected in ("integer", "number")
                if bad_bool or not isinstance(value, accepted):
                    raise ToolParamError(
                        f"parameter {key!r} must be {expected}, got "
                        f"{type(value).__name__}"
                    )
        if isinstance(value, str):
            if len(value) > MAX_STRING_LEN:
                raise ToolParamError(f"parameter {key!r} too long")
            if _CONTROL_CHARS.search(value):
                raise ToolParamError(f"parameter {key!r} contains control characters")
        cleaned[key] = value

    for req in spec["required"]:
        val = cleaned.get(req)
        if val is None or (isinstance(val, str) and not val.strip()):
            raise ToolParamError(f"missing required parameter {req!r}")

    return cleaned


def sanitize_result(result: Any, _depth: int = 0) -> Any:
    """Recursively drop sensitive-looking keys and bound string sizes."""
    if _depth > 12:
        return "<max-depth>"
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k):
                out[k] = "<redacted>"
            else:
                out[k] = sanitize_result(v, _depth + 1)
        return out
    if isinstance(result, list):
        return [sanitize_result(v, _depth + 1) for v in result]
    if isinstance(result, str) and len(result) > MAX_RESULT_STRING_LEN:
        return result[:MAX_RESULT_STRING_LEN] + "...<truncated>"
    return result


# ---------------------------------------------------------------------------
# Correlation A — outbound IoT round-trip (SP2: outbound only)
#
# The MQTT-over-WebSocket subscribe connection and the ``iot-data`` publish
# client are established once and REUSED across tool calls (connection
# pooling, AGENTPERF06-BP02): a single wildcard subscription to
# ``stores/{id}/responses/+`` is routed to per-correlation-id waiters, so a
# tool call no longer pays a TLS/WebSocket/SigV4 handshake every invocation.
# ``awsiot``/``awscrt``/``boto3`` are imported lazily (inside the transport)
# so this module still imports cleanly in CI without the native extension.
# ---------------------------------------------------------------------------


class _RealMqttTransport:
    """Native transport for the pooled tool round-trip.

    Owns one persistent MQTT-over-WebSocket connection (for the response
    subscription) and one reused ``iot-data`` client (for command publish).
    Kept behind a tiny interface (``start`` / ``publish`` / ``close``) so the
    pooling/routing logic in :class:`_ToolConnection` is testable without the
    native ``awscrt``/``awsiot`` extension.
    """

    def __init__(self, *, store_id: str, iot_endpoint: str, region: str) -> None:
        self.store_id = store_id
        self.iot_endpoint = iot_endpoint
        self.region = region
        self._conn: Any = None
        self._iot: Any = None
        self._on_message = None

    def start(self, resp_filter: str, on_message) -> None:  # noqa: ANN001
        import boto3
        from awscrt import auth, mqtt
        from awsiot import mqtt_connection_builder

        self._on_message = on_message
        conn = mqtt_connection_builder.websockets_with_default_aws_signing(
            endpoint=self.iot_endpoint,
            region=self.region,
            credentials_provider=auth.AwsCredentialsProvider.new_default_chain(),
            client_id=f"chat-harness-pool-{uuid.uuid4().hex[:8]}",
            clean_session=True,
            keep_alive_secs=30,
            on_connection_resumed=self._on_resumed,
        )
        conn.connect().result(timeout=15)
        # One wildcard subscription for the whole store (IAM grant is scoped to
        # topicfilter/stores/*/responses/*), routed by correlation id.
        sub, _ = conn.subscribe(
            topic=resp_filter, qos=mqtt.QoS.AT_LEAST_ONCE, callback=self._dispatch
        )
        sub.result(timeout=10)
        self._conn = conn
        self._iot = boto3.client(
            "iot-data", region_name=self.region, endpoint_url=f"https://{self.iot_endpoint}"
        )

    def _dispatch(self, topic, payload, **_kw):  # noqa: ANN001
        cb = self._on_message
        if cb is not None:
            cb(topic, payload)

    def _on_resumed(self, connection, return_code, session_present, **_kw):  # noqa: ANN001
        # A clean-session reconnect drops server-side subscriptions; restore
        # them so responses keep routing after a transient disconnect.
        if not session_present:
            try:
                connection.resubscribe_existing_topics()
            except Exception:  # noqa: BLE001
                logger.warning("resubscribe after connection resume failed", exc_info=True)

    def publish(self, topic: str, payload: bytes) -> None:
        self._iot.publish(topic=topic, qos=1, payload=payload)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self._iot = None
        if conn is not None:
            try:
                conn.disconnect().result(timeout=5)
            except Exception as exc:  # noqa: BLE001
                # Best-effort: the connection is being discarded either way, and
                # raising would propagate out of a cleanup path. Logged with the
                # exception content so a disconnect that consistently times out
                # (leaking a connection per close) is visible rather than silent.
                logger.debug("iot transport disconnect failed during close: %r", exc)


class _ToolConnection:
    """Pooled, thread-safe Correlation-A round-trip for one store.

    Lazily establishes a transport (persistent subscribe connection + reused
    publish client) and routes each ``stores/{id}/responses/{cid}`` reply to
    the waiter registered for that correlation id. A transport error resets the
    connection so the next call re-establishes it.
    """

    def __init__(
        self,
        *,
        store_id: str,
        iot_endpoint: str,
        region: str,
        transport_factory=None,  # noqa: ANN001
    ) -> None:
        self.store_id = store_id
        self.iot_endpoint = iot_endpoint
        self.region = region
        self._transport_factory = transport_factory or _RealMqttTransport
        self._resp_filter = f"stores/{store_id}/responses/+"
        self._waiters_lock = threading.Lock()
        self._waiters: dict[str, "queue.Queue[dict]"] = {}
        self._conn_lock = threading.Lock()
        self._transport: Any = None

    def _on_message(self, topic, payload) -> None:  # noqa: ANN001
        cid = topic.rsplit("/", 1)[-1]
        with self._waiters_lock:
            waiter = self._waiters.get(cid)
        if waiter is None:
            return  # late/duplicate response for a completed call
        try:
            env = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            env = {"status": "error", "error": {"code": "unparseable", "message": "bad response"}}
        try:
            waiter.put_nowait(env)
        except queue.Full:
            # The waiter queue has maxsize=1, so Full means a SECOND response
            # arrived for this correlation id -- normal under MQTT QoS 1
            # redelivery, and correctly ignored because the first response
            # already resolved the call. Logged at debug because the rate is
            # the useful signal: a steady stream of these means the store is
            # answering twice, not that this drop is a problem.
            logger.debug("dropping duplicate response for correlation id %s", cid)

    def _ensure_started(self) -> Any:
        with self._conn_lock:
            if self._transport is None:
                transport = self._transport_factory(
                    store_id=self.store_id, iot_endpoint=self.iot_endpoint, region=self.region
                )
                transport.start(self._resp_filter, self._on_message)
                self._transport = transport
            return self._transport

    def _reset(self) -> None:
        with self._conn_lock:
            transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:  # noqa: BLE001
                # The transport is already detached from self, so the next call
                # re-establishes it regardless; failing loudly here would turn a
                # recoverable transport reset into a failed tool call. Logged
                # with content so a close that always fails is not invisible.
                logger.debug("iot transport close failed during reset: %r", exc)

    def round_trip(self, *, tool_name: str, domain: str, parameters: dict, timeout_s: float) -> dict:
        cid = str(uuid.uuid4())
        cmd_topic = f"stores/{self.store_id}/commands/{domain}"
        waiter: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        # Register the waiter BEFORE publishing so a fast response can never
        # arrive before there is a queue to route it to.
        with self._waiters_lock:
            self._waiters[cid] = waiter
        try:
            try:
                transport = self._ensure_started()
                transport.publish(
                    cmd_topic,
                    json.dumps(
                        {
                            "correlation_id": cid,
                            "tool_name": tool_name,
                            "parameters": parameters,
                            "timeout_ms": int(timeout_s * 1000),
                            "priority": "normal",
                        }
                    ).encode("utf-8"),
                )
            except Exception as exc:  # noqa: BLE001 — connect/publish failure
                logger.warning("tool round-trip transport error (resetting pool): %s", exc)
                self._reset()
                return {
                    "status": "error",
                    "error": {"code": "transport_error", "message": "Could not reach the store."},
                }
            try:
                return waiter.get(timeout=timeout_s)
            except queue.Empty:
                return {
                    "status": "error",
                    "error": {"code": "timeout", "message": f"The {tool_name} tool did not respond in time."},
                }
        finally:
            with self._waiters_lock:
                self._waiters.pop(cid, None)


# Module-level pool: one persistent connection per (store, endpoint, region).
_POOL_LOCK = threading.Lock()
_POOL: dict[tuple[str, str, str], _ToolConnection] = {}


def _get_pooled_connection(store_id: str, iot_endpoint: str, region: str) -> _ToolConnection:
    key = (store_id, iot_endpoint, region)
    with _POOL_LOCK:
        conn = _POOL.get(key)
        if conn is None:
            conn = _ToolConnection(store_id=store_id, iot_endpoint=iot_endpoint, region=region)
            _POOL[key] = conn
        return conn


def _reset_pool() -> None:
    """Close and drop all pooled connections (shutdown / test isolation)."""
    with _POOL_LOCK:
        conns = list(_POOL.values())
        _POOL.clear()
    for conn in conns:
        conn._reset()


# ---------------------------------------------------------------------------
# Per-tool telemetry (AGENTPERF06: instrument tool-layer latency/error/timeout)
#
# Best-effort and OFF the critical path: emission is gated on the
# ``TOOL_METRICS_NAMESPACE`` env var (unset -> no-op) and the PutMetricData
# call runs on a daemon thread, so instrumenting the tool layer never adds
# latency to it. Failures are swallowed (debug-logged).
# ---------------------------------------------------------------------------

#: Recommended namespace; the EC2 role grants PutMetricData on ``RestaurantSim/*``.
DEFAULT_TOOL_METRICS_NAMESPACE = "RestaurantSim/AgentTools"

_CW_LOCK = threading.Lock()
_CW_CLIENTS: dict[str, Any] = {}


def _get_cw_client(region: str) -> Any:
    with _CW_LOCK:
        client = _CW_CLIENTS.get(region)
        if client is None:
            import boto3

            client = boto3.client("cloudwatch", region_name=region)
            _CW_CLIENTS[region] = client
        return client


def _run_async(fn) -> None:  # noqa: ANN001
    """Run a best-effort side effect off the critical path (overridable in tests)."""
    threading.Thread(target=fn, name="tool-metric", daemon=True).start()


def _emit_tool_metric(
    *,
    tool_name: str,
    outcome: str,
    latency_ms: float,
    region: str,
    store_duration_ms: float | None = None,
) -> None:
    """Emit per-tool CloudWatch metrics (best-effort, off the critical path).

    No-op unless ``TOOL_METRICS_NAMESPACE`` is set. Emits ``ToolInvocations``
    (by Tool + Outcome), ``ToolLatencyMs`` (round-trip, by Tool),
    ``ToolTimeouts`` (by Tool, on a timeout), and ``ToolStoreDurationMs`` (the
    store-reported execution time, by Tool — so the IoT-transit overhead,
    ``latency - store_duration``, is visible). Never raises.
    """
    namespace = os.environ.get("TOOL_METRICS_NAMESPACE", "").strip()
    if not namespace:
        return
    dims = [{"Name": "Tool", "Value": tool_name}]
    metric_data: list[dict] = [
        {
            "MetricName": "ToolInvocations",
            "Dimensions": dims + [{"Name": "Outcome", "Value": outcome}],
            "Value": 1.0,
            "Unit": "Count",
        },
        {"MetricName": "ToolLatencyMs", "Dimensions": dims, "Value": float(latency_ms), "Unit": "Milliseconds"},
    ]
    if outcome == "timeout":
        metric_data.append({"MetricName": "ToolTimeouts", "Dimensions": dims, "Value": 1.0, "Unit": "Count"})
        # Dimensionless aggregate so a valid (non-SEARCH) alarm can sum timeouts
        # across all tools into one series -- CloudWatch Metric Alarms cannot alarm
        # on a SEARCH() expression (AGENTPERF06 #4 / spec-028 deploy follow-up).
        metric_data.append({"MetricName": "ToolTimeoutsTotal", "Dimensions": [], "Value": 1.0, "Unit": "Count"})
    if store_duration_ms is not None:
        try:
            metric_data.append(
                {
                    "MetricName": "ToolStoreDurationMs",
                    "Dimensions": dims,
                    "Value": float(store_duration_ms),
                    "Unit": "Milliseconds",
                }
            )
        except (TypeError, ValueError):
            # store_duration_ms came from a tool response, so a non-numeric
            # value is bad input rather than a bug here -- the other metrics in
            # this batch should still be emitted. Logged with the offending
            # value, because "duration metrics are missing" is otherwise
            # unexplainable from the logs alone.
            logger.debug(
                "skipping ToolStoreDurationMs, non-numeric duration: %r",
                store_duration_ms,
            )

    def _put() -> None:
        try:
            _get_cw_client(region).put_metric_data(Namespace=namespace, MetricData=metric_data)
        except Exception:  # noqa: BLE001
            logger.debug("tool metric emit failed (best-effort)", exc_info=True)

    _run_async(_put)


# ---------------------------------------------------------------------------
# Last-known-state read fallback (spec 026, AGENTPERF06 #3)
#
# On a SUCCESSFUL cacheable read, write-through the sanitized result to the
# spec-006 ``StoreStateCache`` table under a tool-qualified key. On a read that
# times out / fails transport, fall back to that last-known result with an
# explicit staleness marker instead of an error. Gated on
# ``TOOL_READ_CACHE_ENABLED`` (default OFF) + ``STORE_STATE_CACHE_TABLE``;
# best-effort and OFF the critical path. Only the parameterless reads in
# ``CACHEABLE_READ_TOOLS`` are eligible — mutating + parameterized tools never.
# ---------------------------------------------------------------------------

#: Default max staleness (seconds) beyond which a cached entry is NOT served.
_DEFAULT_CACHE_MAX_STALENESS_S = 900  # 15 minutes (spec 026 D4)

_DDB_LOCK = threading.Lock()
_DDB_TABLES: dict[str, Any] = {}


def _cache_enabled() -> bool:
    return os.environ.get("TOOL_READ_CACHE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ) and bool(os.environ.get("STORE_STATE_CACHE_TABLE", "").strip())


def _max_staleness_s() -> int:
    raw = os.environ.get("TOOL_READ_CACHE_MAX_STALENESS_S", "").strip()
    if not raw:
        return _DEFAULT_CACHE_MAX_STALENESS_S
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_CACHE_MAX_STALENESS_S


def _get_ddb_table(region: str) -> Any:
    """Return a cached DynamoDB Table resource for the StoreStateCache table."""
    table_name = os.environ.get("STORE_STATE_CACHE_TABLE", "").strip()
    key = f"{region}/{table_name}"
    with _DDB_LOCK:
        table = _DDB_TABLES.get(key)
        if table is None:
            import boto3

            table = boto3.resource("dynamodb", region_name=region).Table(table_name)
            _DDB_TABLES[key] = table
        return table


def _cache_write(store_id: str, tool_name: str, region: str, result: Any) -> None:
    """Write-through a successful cacheable read (best-effort; never raises).

    The result is stored as a JSON string to avoid float->Decimal coercion.
    """
    try:
        _get_ddb_table(region).put_item(
            Item={
                "store_domain_key": f"{store_id}#{tool_name}",
                "result_json": json.dumps(result),
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "tool_name": tool_name,
                "store_id": store_id,
            }
        )
    except Exception:  # noqa: BLE001
        logger.debug("StoreStateCache write-through failed (best-effort)", exc_info=True)


def _cache_read(store_id: str, tool_name: str, region: str) -> dict | None:
    """Return ``{result, as_of, age_seconds}`` from the cache, or ``None`` if
    absent, too stale, or on any error (best-effort; never raises)."""
    try:
        resp = _get_ddb_table(region).get_item(
            Key={"store_domain_key": f"{store_id}#{tool_name}"}
        )
    except Exception:  # noqa: BLE001
        logger.debug("StoreStateCache read failed (best-effort)", exc_info=True)
        return None
    item = resp.get("Item")
    if not item:
        return None
    cached_at = item.get("cached_at")
    raw = item.get("result_json")
    if not cached_at or raw is None:
        return None
    try:
        as_of_dt = datetime.fromisoformat(cached_at)
        age_seconds = int((datetime.now(timezone.utc) - as_of_dt).total_seconds())
        result = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if age_seconds < 0 or age_seconds > _max_staleness_s():
        return None
    return {"result": result, "as_of": cached_at, "age_seconds": age_seconds}


def execute_tool(
    *,
    tool_name: str,
    parameters: dict,
    store_id: str,
    iot_endpoint: str,
    region: str,
    timeout_s: float = 30.0,
) -> dict:
    """Run one store tool via the pooled Correlation-A round-trip.

    Returns either ``{"status": "success", "result": {...}}`` or
    ``{"status": "error", "error": {"code", "message"}}`` (sanitized). Never
    raises for store/timeout/transport failures — those become structured
    error envelopes the model can reason about. Per-tool latency/outcome is
    emitted to CloudWatch best-effort (off the critical path) via
    ``_emit_tool_metric``.

    The MQTT subscribe connection and ``iot-data`` client are pooled per
    (store, endpoint, region) and reused across calls (AGENTPERF06-BP02); the
    native ``awsiot``/``awscrt``/``boto3`` imports are deferred to the
    transport so this module loads without the extension (CI / mocked tests).
    """
    domain = DOMAIN_OF_TOOL.get(tool_name)
    if domain is None:
        return {"status": "error", "error": {"code": "unknown_tool", "message": f"Unknown tool {tool_name!r}"}}
    if not iot_endpoint:
        return {"status": "error", "error": {"code": "not_configured", "message": "IOT_ENDPOINT is not set."}}

    conn = _get_pooled_connection(store_id, iot_endpoint, region)
    started = time.monotonic()
    env = conn.round_trip(
        tool_name=tool_name, domain=domain, parameters=parameters, timeout_s=timeout_s
    )
    latency_ms = (time.monotonic() - started) * 1000.0
    cacheable = _cache_enabled() and tool_name in CACHEABLE_READ_TOOLS

    status = env.get("status", "unknown")
    if status == "success":
        result = sanitize_result(env.get("result", {}))
        if cacheable:
            # Write-through, off the critical path (spec 026 R2).
            _run_async(lambda: _cache_write(store_id, tool_name, region, result))
        _emit_tool_metric(
            tool_name=tool_name,
            outcome="success",
            latency_ms=latency_ms,
            region=region,
            store_duration_ms=env.get("duration_ms"),
        )
        return {"status": "success", "result": result}

    err = env.get("error") or {}
    code = str(err.get("code", "tool_error"))
    # Last-known-state fallback (spec 026 R3): only for a cacheable read that
    # failed to reach the store; a live success is never overridden.
    if cacheable and code in ("timeout", "transport_error"):
        cached = _cache_read(store_id, tool_name, region)
        if cached is not None:
            _emit_tool_metric(
                tool_name=tool_name, outcome="cache_hit", latency_ms=latency_ms, region=region
            )
            return {
                "status": "success",
                "result": cached["result"],
                "stale": True,
                "as_of": cached["as_of"],
                "age_seconds": cached["age_seconds"],
                "source": "cache",
            }
    _emit_tool_metric(tool_name=tool_name, outcome=code, latency_ms=latency_ms, region=region)
    return {
        "status": "error",
        "error": {"code": code, "message": str(err.get("message", "tool returned an error"))},
    }


# ---------------------------------------------------------------------------
# Stream parsing (shared by the backend)
# ---------------------------------------------------------------------------


def stream_collect(resp: dict) -> tuple[str, dict, str | None]:
    """Collect (text, tool_use{id,name,input}, stop_reason) from an
    ``invoke_harness`` response stream.
    """
    text = ""
    tu: dict[str, Any] = {"id": None, "name": None, "input": ""}
    stop: str | None = None
    for ev in resp["stream"]:
        if "contentBlockStart" in ev:
            start = ev["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tu["id"] = start["toolUse"].get("toolUseId")
                tu["name"] = start["toolUse"].get("name")
        elif "contentBlockDelta" in ev:
            d = ev["contentBlockDelta"].get("delta", {})
            if "text" in d:
                text += d["text"]
            if "toolUse" in d:
                tu["input"] += d["toolUse"].get("input", "")
        elif "messageStop" in ev:
            stop = ev["messageStop"].get("stopReason")
        elif "runtimeClientError" in ev:
            logger.warning("harness runtimeClientError: %s", ev["runtimeClientError"])
            raise RuntimeError(f"harness runtime error: {ev['runtimeClientError']}")
    return text, tu, stop
