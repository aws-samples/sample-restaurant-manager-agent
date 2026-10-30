# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Standalone MCP stdio runner for one domain at a time.

Usage:
    python -m simulator.mcp.run_server <domain>

Where ``<domain>`` is one of: ``pos``, ``equipment``, ``inventory``, ``schedule``.

The runner constructs a minimal :class:`StateStore` (loading config files from
``config/``) and starts the selected server over stdio. Useful for testing
tools with ``mcp-cli``, Claude Code, or Anthropic Workbench without starting
the full simulator stack.
"""

from __future__ import annotations

import sys
from typing import Callable

from mcp.server.fastmcp import FastMCP

from simulator.mcp.equipment_server import build_equipment_server
from simulator.mcp.inventory_server import build_inventory_server
from simulator.mcp.pos_server import build_pos_server
from simulator.mcp.schedule_server import build_schedule_server
from simulator.state.store import StateStore, load_all_configs

_BUILDERS: dict[str, Callable[[StateStore], FastMCP]] = {
    "pos": build_pos_server,
    "equipment": build_equipment_server,
    "inventory": build_inventory_server,
    "schedule": build_schedule_server,
}


def _usage() -> str:
    return (
        "Usage: python -m simulator.mcp.run_server <domain>\n"
        f"  domain: {' | '.join(_BUILDERS)}"
    )


def main() -> None:
    if len(sys.argv) != 2:
        print(_usage(), file=sys.stderr)
        sys.exit(2)

    domain = sys.argv[1].strip().lower()
    if domain not in _BUILDERS:
        print(f"Unknown domain: {domain!r}\n{_usage()}", file=sys.stderr)
        sys.exit(2)

    configs = load_all_configs()
    store = StateStore.from_configs(configs)
    server = _BUILDERS[domain](store)
    server.run()


if __name__ == "__main__":
    main()
