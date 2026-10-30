# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram 1 — secure remote tool invocation, reference architecture (overview).

Renders ``diagram1-architecture-overview.png`` with the mingrammer ``diagrams``
library (AWS icon sets, Graphviz-rendered), styled per the AWS Architecture
Icons usage guidelines (see ``_style.py``). Run from the repo root:

    .venv/bin/python docs/solution-guidance/diagrams/diagram1_architecture_overview.py

Shows the CURRENT production path (the in-process round-trip): the chat backend
runs the AgentCore harness on the inline-tool path and executes each tool call
itself over an outbound IoT round-trip (``iot-data:Publish`` for the command +
a pooled SigV4 MQTT-over-WebSocket subscription for the response). The
alternative event-driven relay path is documented in the reference
architecture prose. This Python file is the editable source of truth.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2
from diagrams.aws.general import TraditionalServer, Users
from diagrams.aws.iot import IotCore
from diagrams.aws.ml import Bedrock

from _style import CLOUD, EDGE_ATTR, GRAPH_ATTR, NODE_ATTR, ONPREM, OUTBOUND

with Diagram(
    "Secure remote tool invocation — reference architecture",
    filename="docs/solution-guidance/diagrams/diagram1-architecture-overview",
    show=False,
    direction="LR",
    outformat="png",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    mgr = Users("Restaurant\ngeneral manager")

    with Cluster("AWS Cloud (us-west-2)", graph_attr=CLOUD):
        chat = EC2("Chat backend (FastAPI)\ninline-tool executor")
        harness = Bedrock("AgentCore Managed Harness\n(Amazon Bedrock)")
        iot = IotCore("AWS IoT Core\nMQTT broker · mTLS (X.509)")

        mgr >> Edge(label="HTTPS chat") >> chat
        chat >> Edge(label="invoke_harness · tool_use / result", dir="both") >> harness
        chat >> Edge(
            label="publish command (iot-data:Publish)  +\nSigV4 MQTT/WSS response subscribe",
            dir="both",
        ) >> iot

    with Cluster(
        "On-premises store — behind the firewall · outbound-only · NO inbound connections",
        graph_attr=ONPREM,
    ):
        gw = TraditionalServer("Store gateway\noutbound MQTT bridge")
        mcp = TraditionalServer(
            "MCP servers\npos · equipment · inventory · schedule\n(18 tools)"
        )
        systems = TraditionalServer("Store systems\nPOS · kitchen · inventory · staff")

        gw >> Edge(label="dispatch (MCP)") >> mcp >> Edge(label="read / act") >> systems

    # The store DIALS OUT to IoT Core; the command (down) and the response (up)
    # both ride this one store-initiated mTLS connection. No inbound port is
    # ever opened at the store.
    gw >> Edge(
        label="outbound mTLS · port 443 / ALPN\nx-amzn-mqtt-ca  (store-initiated)",
        color=OUTBOUND,
        penwidth="2.5",
        dir="both",
    ) >> iot
