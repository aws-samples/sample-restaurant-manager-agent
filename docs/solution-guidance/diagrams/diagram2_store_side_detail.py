# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram 2 — store-side detail (the on-premises gateway internals).

Renders ``diagram2-store-side-detail.png``, styled per the AWS Architecture
Icons usage guidelines (see ``_style.py``). Run from the repo root:

    .venv/bin/python docs/solution-guidance/diagrams/diagram2_store_side_detail.py

This Python file is the editable source of truth for the figure.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.general import GenericDatabase, TraditionalServer
from diagrams.aws.iot import IotCore, IotMqtt

from _style import CLOUD, EDGE_ATTR, GRAPH_ATTR, NODE_ATTR, ONPREM, OUTBOUND, SUBGROUP

BLUE = "#245A8D"
GRAYLINE = "#5A6B7B"

with Diagram(
    "Store-side detail — outbound MQTT bridge, MCP tools, and the simulation engine",
    filename="docs/solution-guidance/diagrams/diagram2-store-side-detail",
    show=False,
    direction="TB",
    outformat="png",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    with Cluster("AWS Cloud", graph_attr=CLOUD):
        iot = IotCore("AWS IoT Core")

    with Cluster(
        "On-premises store — behind the firewall · outbound-only", graph_attr=ONPREM
    ):
        with Cluster(
            "Store gateway — outbound MQTT bridge (no inbound port)", graph_attr=SUBGROUP
        ):
            mqtt = IotMqtt("MQTT client\naiomqtt · supervised reconnect")
            disp = TraditionalServer("Dispatcher\nparse · validate · bounded concurrency")
            dedup = GenericDatabase(
                "Correlation-ID cache\nLRU 1024 · 30-min TTL\n(at-most-once)"
            )
            pub = TraditionalServer("Publisher\nresponse + disconnect buffer")

        with Cluster("MCP tool layer (18 tools)", graph_attr=SUBGROUP):
            reg = TraditionalServer("Tool registry\nname → callable")
            pos = TraditionalServer("pos-mcp")
            equip = TraditionalServer("equipment-mcp")
            inv = TraditionalServer("inventory-mcp")
            sched = TraditionalServer("schedule-mcp")

        with Cluster("Simulation & state", graph_attr=SUBGROUP):
            eng = TraditionalServer(
                "Simulation engine\nclock · tick loop\nPOS · equipment ·\ninventory · schedule"
            )
            state = GenericDatabase("State store\nin-memory + event emission")
            api = TraditionalServer("REST API + WebSocket\nlive dashboard")

    # One store-initiated outbound mTLS link carries commands (down) and
    # responses (up). Internal edges show how each is handled.
    mqtt >> Edge(
        label="outbound mTLS · 443/ALPN\ncommands + responses (store-initiated)",
        color=OUTBOUND,
        penwidth="2.5",
        dir="both",
    ) >> iot

    # Command path (inbound over the connection).
    mqtt >> Edge(label="deliver command\nstores/{id}/commands/{domain}") >> disp
    disp >> Edge(label="dedup check", style="dotted") >> dedup
    disp >> Edge(label="invoke tool") >> reg
    reg >> [pos, equip, inv, sched]

    # Tools read/mutate the shared state.
    for tool in (pos, equip, inv, sched):
        tool >> Edge(color=GRAYLINE) >> state

    # Response path (outbound over the same connection).
    disp >> Edge(label="result") >> pub
    pub >> Edge(label="publish response\nstores/{id}/responses/{cid}", color=BLUE) >> mqtt

    # The engine advances state; the dashboard streams it live.
    eng >> Edge(label="tick updates") >> state
    state >> Edge(label="broadcast") >> api
