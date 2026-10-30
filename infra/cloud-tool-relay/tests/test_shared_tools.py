# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``lambdas.shared.tools`` — the static tool map and
read/write classification.

Requirements covered:
    R4.3   — exact tool names per domain (16 tools across 4 domains).
    R5.3   — read-tool classification drives ``StoreStateCache``
             write-through.
    R8.3   — per-tool timeout classification.
    R15.5  — static tool map matches the 16 tools listed in R4.3.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import pytest

# Ensure the lambdas package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lambdas.shared.tools import (
    ALL_TOOLS,
    COMPLEX_WRITE_TIMEOUT_S,
    COMPLEX_WRITE_TOOLS,
    DOMAIN_TOOLS,
    READ_TOOLS,
    SIMPLE_READ_TIMEOUT_S,
    SIMPLE_READ_TOOLS,
    TOOL_TIMEOUT_S,
    TOOL_TO_DOMAIN,
    WRITE_TOOLS,
    domain_for_tool,
    is_read_tool,
    timeout_for_tool,
    tools_for_domain,
)

# -----------------------------------------------------------------------
# Expected values from requirements R4.3
# -----------------------------------------------------------------------

#: The exact 16 tools from R4.3, grouped by domain.
EXPECTED_DOMAIN_TOOLS: Final[dict[str, set[str]]] = {
    "pos": {
        "get_daily_sales",
        "get_active_orders",
        "get_transaction_history",
        "void_order",
    },
    "equipment": {
        "get_equipment_status",
        "set_timer",
        "get_temperature_log",
        "acknowledge_alert",
    },
    "inventory": {
        "get_stock_levels",
        "log_waste",
        "get_usage_report",
        "check_reorder_status",
    },
    "schedule": {
        "get_current_shifts",
        "clock_in",
        "clock_out",
        "get_labor_cost",
    },
}

#: All 16 tool names flattened.
EXPECTED_ALL_TOOLS: Final[set[str]] = set().union(*EXPECTED_DOMAIN_TOOLS.values())

#: Read tools from design.md response-handler section.
EXPECTED_READ_TOOLS: Final[set[str]] = {
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

#: Write / action tools — complement of read tools.
EXPECTED_WRITE_TOOLS: Final[set[str]] = EXPECTED_ALL_TOOLS - EXPECTED_READ_TOOLS

#: Simple-read tools (10 s timeout) from R8.3.
EXPECTED_SIMPLE_READ_TOOLS: Final[set[str]] = {
    "get_equipment_status",
    "get_stock_levels",
    "get_current_shifts",
    "get_active_orders",
    "check_reorder_status",
    "get_temperature_log",
    "get_daily_sales",
    "get_labor_cost",
}

#: Complex / write tools (30 s timeout) from R8.3.
EXPECTED_COMPLEX_WRITE_TOOLS: Final[set[str]] = {
    "get_usage_report",
    "get_transaction_history",
    "void_order",
    "set_timer",
    "acknowledge_alert",
    "log_waste",
    "clock_in",
    "clock_out",
}


# -----------------------------------------------------------------------
# R4.3 — Domain → tool mapping
# -----------------------------------------------------------------------


class TestDomainToolMapping:
    """Verify the domain-to-tool mapping matches R4.3 exactly."""

    def test_exactly_four_domains(self) -> None:
        assert set(DOMAIN_TOOLS.keys()) == {"pos", "equipment", "inventory", "schedule"}

    def test_pos_tools(self) -> None:
        assert set(DOMAIN_TOOLS["pos"]) == EXPECTED_DOMAIN_TOOLS["pos"]

    def test_equipment_tools(self) -> None:
        assert set(DOMAIN_TOOLS["equipment"]) == EXPECTED_DOMAIN_TOOLS["equipment"]

    def test_inventory_tools(self) -> None:
        assert set(DOMAIN_TOOLS["inventory"]) == EXPECTED_DOMAIN_TOOLS["inventory"]

    def test_schedule_tools(self) -> None:
        assert set(DOMAIN_TOOLS["schedule"]) == EXPECTED_DOMAIN_TOOLS["schedule"]

    def test_exactly_16_tools_total(self) -> None:
        assert len(ALL_TOOLS) == 16

    def test_all_tools_matches_expected(self) -> None:
        assert set(ALL_TOOLS) == EXPECTED_ALL_TOOLS

    def test_no_tool_appears_in_multiple_domains(self) -> None:
        """Each tool belongs to exactly one domain."""
        seen: dict[str, str] = {}
        for domain, tools in DOMAIN_TOOLS.items():
            for tool in tools:
                assert tool not in seen, (
                    f"Tool {tool!r} appears in both {seen[tool]!r} and {domain!r}"
                )
                seen[tool] = domain

    def test_four_tools_per_domain(self) -> None:
        for domain, tools in DOMAIN_TOOLS.items():
            assert len(tools) == 4, (
                f"Domain {domain!r} has {len(tools)} tools, expected 4"
            )


# -----------------------------------------------------------------------
# Reverse lookup
# -----------------------------------------------------------------------


class TestToolToDomain:
    """Verify the reverse lookup map."""

    def test_every_tool_has_a_domain(self) -> None:
        assert set(TOOL_TO_DOMAIN.keys()) == EXPECTED_ALL_TOOLS

    @pytest.mark.parametrize(
        "tool,expected_domain",
        [
            ("get_daily_sales", "pos"),
            ("void_order", "pos"),
            ("get_equipment_status", "equipment"),
            ("set_timer", "equipment"),
            ("get_stock_levels", "inventory"),
            ("log_waste", "inventory"),
            ("get_current_shifts", "schedule"),
            ("clock_in", "schedule"),
        ],
    )
    def test_specific_tool_domain_mapping(self, tool: str, expected_domain: str) -> None:
        assert TOOL_TO_DOMAIN[tool] == expected_domain


# -----------------------------------------------------------------------
# R5.3 — Read-tool classification
# -----------------------------------------------------------------------


class TestReadToolClassification:
    """Verify the read/write classification matches design.md."""

    def test_read_tools_match_expected(self) -> None:
        assert set(READ_TOOLS) == EXPECTED_READ_TOOLS

    def test_write_tools_match_expected(self) -> None:
        assert set(WRITE_TOOLS) == EXPECTED_WRITE_TOOLS

    def test_read_and_write_are_disjoint(self) -> None:
        assert READ_TOOLS & WRITE_TOOLS == frozenset()

    def test_read_and_write_cover_all_tools(self) -> None:
        assert READ_TOOLS | WRITE_TOOLS == ALL_TOOLS

    def test_ten_read_tools(self) -> None:
        assert len(READ_TOOLS) == 10

    def test_six_write_tools(self) -> None:
        assert len(WRITE_TOOLS) == 6

    @pytest.mark.parametrize("tool", sorted(EXPECTED_READ_TOOLS))
    def test_is_read_tool_returns_true(self, tool: str) -> None:
        assert is_read_tool(tool) is True

    @pytest.mark.parametrize("tool", sorted(EXPECTED_WRITE_TOOLS))
    def test_is_read_tool_returns_false_for_writes(self, tool: str) -> None:
        assert is_read_tool(tool) is False


# -----------------------------------------------------------------------
# R8.3 — Per-tool timeout classification
# -----------------------------------------------------------------------


class TestTimeoutClassification:
    """Verify per-tool timeout values match R8.3."""

    def test_simple_read_timeout_is_10s(self) -> None:
        assert SIMPLE_READ_TIMEOUT_S == 10

    def test_complex_write_timeout_is_30s(self) -> None:
        assert COMPLEX_WRITE_TIMEOUT_S == 30

    def test_simple_read_tools_match_expected(self) -> None:
        assert set(SIMPLE_READ_TOOLS) == EXPECTED_SIMPLE_READ_TOOLS

    def test_complex_write_tools_match_expected(self) -> None:
        assert set(COMPLEX_WRITE_TOOLS) == EXPECTED_COMPLEX_WRITE_TOOLS

    def test_timeout_buckets_are_disjoint(self) -> None:
        assert SIMPLE_READ_TOOLS & COMPLEX_WRITE_TOOLS == frozenset()

    def test_timeout_buckets_cover_all_tools(self) -> None:
        assert SIMPLE_READ_TOOLS | COMPLEX_WRITE_TOOLS == ALL_TOOLS

    def test_every_tool_has_a_timeout(self) -> None:
        assert set(TOOL_TIMEOUT_S.keys()) == set(ALL_TOOLS)

    @pytest.mark.parametrize("tool", sorted(EXPECTED_SIMPLE_READ_TOOLS))
    def test_simple_read_tool_timeout_value(self, tool: str) -> None:
        assert TOOL_TIMEOUT_S[tool] == 10

    @pytest.mark.parametrize("tool", sorted(EXPECTED_COMPLEX_WRITE_TOOLS))
    def test_complex_write_tool_timeout_value(self, tool: str) -> None:
        assert TOOL_TIMEOUT_S[tool] == 30


# -----------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------


class TestHelperFunctions:
    """Verify the convenience functions."""

    def test_tools_for_domain_returns_correct_set(self) -> None:
        assert set(tools_for_domain("equipment")) == EXPECTED_DOMAIN_TOOLS["equipment"]

    def test_tools_for_domain_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            tools_for_domain("unknown_domain")

    def test_domain_for_tool_returns_correct_domain(self) -> None:
        assert domain_for_tool("get_equipment_status") == "equipment"

    def test_domain_for_tool_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            domain_for_tool("nonexistent_tool")

    def test_timeout_for_tool_returns_correct_value(self) -> None:
        assert timeout_for_tool("get_equipment_status") == 10
        assert timeout_for_tool("void_order") == 30

    def test_timeout_for_tool_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            timeout_for_tool("nonexistent_tool")
