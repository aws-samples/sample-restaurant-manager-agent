# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram 3 — a single tool invocation, end to end (numbered request/response).

Renders ``diagram3-tool-invocation-flow.png``, styled per the AWS Architecture
Icons usage guidelines (see ``_style.py``). Run from the repo root:

    .venv/bin/python docs/solution-guidance/diagrams/diagram3_tool_invocation_flow.py

Shows the CURRENT production path (the in-process round-trip). ``diagrams``
renders node graphs, not UML sequence diagrams, so the ordering is carried by
the numbered edge labels (1 -> 10). This Python file is the editable source.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2
from diagrams.aws.general import TraditionalServer, Users
from diagrams.aws.iot import IotCore
from diagrams.aws.ml import Bedrock

from _style import CLOUD, EDGE_ATTR, GRAPH_ATTR, NODE_ATTR, ONPREM, OUTBOUND

BLUE = "#245A8D"

with Diagram(
    "One tool invocation, end to end (numbered request → response)",
    filename="docs/solution-guidance/diagrams/diagram3-tool-invocation-flow",
    show=False,
    direction="LR",
    outformat="png",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    mgr = Users("Restaurant\ngeneral manager")

    with Cluster("AWS Cloud", graph_attr=CLOUD):
        chat = EC2("Chat backend\n(inline-tool executor)")
        harness = Bedrock("AgentCore\nManaged Harness")
        iot = IotCore("AWS IoT Core")

    with Cluster("Store — behind the firewall · outbound-only", graph_attr=ONPREM):
        gw = TraditionalServer("Store gateway")
        tool = TraditionalServer("MCP tool\n+ store system")

    mgr >> Edge(label="1. ask (chat)") >> chat
    chat >> Edge(label="2. invoke_harness") >> harness
    harness >> Edge(label="3. tool_use\n(get_equipment_status)") >> chat
    chat >> Edge(label="4. iot-data:Publish command\n+ SigV4 subscribe response") >> iot
    iot >> Edge(
        label="5. deliver over the store's\noutbound mTLS connection",
        color=OUTBOUND,
        penwidth="2.5",
    ) >> gw
    gw >> Edge(label="6. dispatch + read/act") >> tool
    gw >> Edge(label="7. publish response", color=BLUE) >> iot
    iot >> Edge(label="8. response\n(bounded 30s wait)", color=BLUE) >> chat
    chat >> Edge(label="9. re-invoke (toolResult)\n→ final answer", dir="both") >> harness
    chat >> Edge(label="10. answer") >> mgr
