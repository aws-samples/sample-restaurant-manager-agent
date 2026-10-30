# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram 4 — agent-initiated proactive alerting (reference-architecture Fig 4).

Renders ``diagram4-proactive-alerting.png``, styled per the AWS Architecture
Icons usage guidelines (see ``_style.py``). Run from the repo root:

    .venv/bin/python docs/solution-guidance/diagrams/diagram4_proactive_alerting.py

This Python file is the editable source of truth for the figure.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.general import TraditionalServer
from diagrams.aws.integration import Eventbridge, StepFunctions
from diagrams.aws.iot import IotCore

from _style import CLOUD, EDGE_ATTR, GRAPH_ATTR, NODE_ATTR, ONPREM, OUTBOUND

BLUE = "#245A8D"

with Diagram(
    "Agent-initiated proactive alerting",
    filename="docs/solution-guidance/diagrams/diagram4-proactive-alerting",
    show=False,
    direction="LR",
    outformat="png",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    with Cluster("On-premises store — always-on display", graph_attr=ONPREM):
        src = TraditionalServer("Store engine\nequipment · inventory · …")
        display = TraditionalServer(
            "Always-on display\nin-app inbox · WebSocket · voice"
        )

    with Cluster("AWS Cloud (us-west-2)", graph_attr=CLOUD):
        iot = IotCore("AWS IoT Core")
        ingest = Lambda("ingest\nvalidate · window-dedup · persist")
        alerts = Dynamodb("Alerts")
        eb = Eventbridge("EventBridge\nAlertRaised")
        sfn = StepFunctions(
            "Alert policy (Step Functions)\nseverity · quiet hours · dedup\nack SLA · bounded re-surface"
        )

    src >> Edge(label="alert event\nstores/{id}/events/{domain}", color=OUTBOUND, penwidth="2.5") >> iot
    iot >> Edge(label="IoT Topic Rule") >> ingest
    ingest >> Edge(label="dedup + persist") >> alerts
    ingest >> Edge(label="AlertRaised") >> eb >> sfn
    sfn >> Edge(label="notification\nstores/{id}/notifications/{id}") >> iot
    iot >> Edge(label="deliver (outbound mTLS)", color=OUTBOUND, penwidth="2.5") >> display
    display >> Edge(
        label="acknowledge → resume\n(SendTaskSuccess)", style="dashed", color=BLUE
    ) >> sfn
