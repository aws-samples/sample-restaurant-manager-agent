# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Store-side MCP (Model Context Protocol) servers.

This package is the "universal tool interface" for the restaurant simulator.
Four MCP servers — POS, Equipment, Inventory, Schedule — expose the simulator's
state and actions through a uniform tool schema that cloud-hosted agents invoke
either over stdio (local development) or via the MQTT bridge (spec 002).

The MCP layer decouples cloud agents from store-side implementation details:
a store can swap POS vendors or replace a kitchen controller without any change
to the cloud agent layer — only the tool implementations here change.
"""
