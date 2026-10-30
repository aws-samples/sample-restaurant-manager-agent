# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static tool-name-to-domain map and read/write classification.

This module is the single source of truth for the 16 MCP tools exposed by
the four gateway Lambdas (spec 006, R4.3). Both the gateway Lambdas and the
Fargate response handler import from here so the tool roster and the
read-tool classification stay in sync — preventing drift between the
"publish" side and the "write-through cache" side.

Requirements covered:
    R4.3  — exact tool names per domain.
    R5.3  — read-tool classification drives ``StoreStateCache`` write-through.
    R8.3  — per-tool timeout classification (simple-read vs complex/write).
"""
from __future__ import annotations

from typing import Final

# -----------------------------------------------------------------------
# Domain → tool mapping (R4.3)
# -----------------------------------------------------------------------
#
# Each key is a domain name that doubles as the MQTT topic segment
# (``stores/{store_id}/commands/{domain}``). Each value is the frozen
# set of tool names that domain's gateway Lambda exposes via MCP.

DOMAIN_TOOLS: Final[dict[str, frozenset[str]]] = {
    "pos": frozenset(
        {
            "get_daily_sales",
            "get_active_orders",
            "get_transaction_history",
            "void_order",
        }
    ),
    "equipment": frozenset(
        {
            "get_equipment_status",
            "set_timer",
            "get_temperature_log",
            "acknowledge_alert",
        }
    ),
    "inventory": frozenset(
        {
            "get_stock_levels",
            "log_waste",
            "get_usage_report",
            "check_reorder_status",
        }
    ),
    "schedule": frozenset(
        {
            "get_current_shifts",
            "clock_in",
            "clock_out",
            "get_labor_cost",
        }
    ),
}

# Flat set of every tool name across all domains — handy for validation.
ALL_TOOLS: Final[frozenset[str]] = frozenset().union(*DOMAIN_TOOLS.values())

# Reverse lookup: tool name → domain.
TOOL_TO_DOMAIN: Final[dict[str, str]] = {
    tool: domain for domain, tools in DOMAIN_TOOLS.items() for tool in tools
}

# -----------------------------------------------------------------------
# Read-tool classification (R5.3, design.md response-handler section)
# -----------------------------------------------------------------------
#
# Tools classified as "read" trigger a write-through update to
# ``StoreStateCache`` when the response handler processes a successful
# response. The classification comes from design.md's response-handler
# component description:
#
#   "If the tool is in the shared read-tool list (get_daily_sales,
#    get_equipment_status, get_temperature_log, get_stock_levels,
#    check_reorder_status, get_usage_report, get_current_shifts,
#    get_labor_cost, and get_active_orders / get_transaction_history
#    as read-oriented reports) …"
#
# That list covers 10 of the 16 tools. The remaining 6 are writes or
# actions (void_order, set_timer, acknowledge_alert, log_waste,
# clock_in, clock_out) and do NOT update the cache.

READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_daily_sales",
        "get_active_orders",
        "get_transaction_history",
        "get_equipment_status",
        "get_temperature_log",
        "get_stock_levels",
        "check_reorder_status",
        "get_usage_report",
        "get_current_shifts",
        "get_labor_cost",
    }
)

# Write / action tools — the complement of READ_TOOLS within ALL_TOOLS.
WRITE_TOOLS: Final[frozenset[str]] = ALL_TOOLS - READ_TOOLS

# -----------------------------------------------------------------------
# Per-tool timeout classification (R8.3)
# -----------------------------------------------------------------------
#
# Two buckets taken directly from the blog / requirements:
#   - 10 s for simple reads.
#   - 30 s for complex queries or writes.
#
# Domain agents use these defaults when polling ``CommandResults``.

SIMPLE_READ_TIMEOUT_S: Final[int] = 10
COMPLEX_WRITE_TIMEOUT_S: Final[int] = 30

#: Tools with a 10-second timeout (simple reads).
SIMPLE_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_equipment_status",
        "get_stock_levels",
        "get_current_shifts",
        "get_active_orders",
        "check_reorder_status",
        "get_temperature_log",
        "get_daily_sales",
        "get_labor_cost",
    }
)

#: Tools with a 30-second timeout (complex queries or writes).
COMPLEX_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_usage_report",
        "get_transaction_history",
        "void_order",
        "set_timer",
        "acknowledge_alert",
        "log_waste",
        "clock_in",
        "clock_out",
    }
)

#: Per-tool timeout map. Every tool in ``ALL_TOOLS`` has an entry.
TOOL_TIMEOUT_S: Final[dict[str, int]] = {
    **{tool: SIMPLE_READ_TIMEOUT_S for tool in SIMPLE_READ_TOOLS},
    **{tool: COMPLEX_WRITE_TIMEOUT_S for tool in COMPLEX_WRITE_TOOLS},
}


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def is_read_tool(tool_name: str) -> bool:
    """Return ``True`` if *tool_name* is classified as a read tool.

    Used by the response handler to decide whether to write-through to
    ``StoreStateCache`` (R5.3).
    """
    return tool_name in READ_TOOLS


def tools_for_domain(domain: str) -> frozenset[str]:
    """Return the set of tool names for *domain*.

    Raises ``KeyError`` if *domain* is not one of the four known domains.
    """
    return DOMAIN_TOOLS[domain]


def domain_for_tool(tool_name: str) -> str:
    """Return the domain that owns *tool_name*.

    Raises ``KeyError`` if *tool_name* is not one of the 16 known tools.
    """
    return TOOL_TO_DOMAIN[tool_name]


def timeout_for_tool(tool_name: str) -> int:
    """Return the per-tool timeout in seconds for *tool_name*.

    Raises ``KeyError`` if *tool_name* is not one of the 16 known tools.
    """
    return TOOL_TIMEOUT_S[tool_name]
