# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared styling for the architecture diagrams, following the AWS Architecture
Icons usage guidelines.

The AWS "official standard" is the Architecture Icons toolkit plus the usage
rules baked into its decks. The parts we can honor from mingrammer ``diagrams``
(Graphviz) are centralized here so all four figures stay consistent:

- **Labels** — every icon is labeled; label text is Arial, dark slate
  (``#232F3E``) on a light background, ~12 pt.
- **Groups** — real border treatments: **AWS Cloud** = solid dark-slate border;
  **on-premises / corporate data center** = gray, dashed. Neutral logical
  sub-groupings use a lighter dashed box.
- **Connections** — clean **orthogonal** connectors (``splines=ortho``), arrows
  showing data/request flow — not tangled diagonals.
- **Whitespace** — generous node/rank separation and group padding.
- **Icons** — the official AWS icon art is used as-is (mingrammer ships the
  toolkit icons); never recolored, stretched, or rotated. Non-AWS components
  use the neutral AWS *general* icons (``TraditionalServer`` / ``GenericDatabase``),
  not a borrowed service icon.
"""

SLATE = "#232F3E"  # AWS label / dark-slate
GRAY = "#7D8998"  # on-premises border
LINE = "#5A6B7B"  # connector

# Diagram-wide attributes.
GRAPH_ATTR = {
    "fontname": "Arial",
    "fontsize": "24",
    "fontcolor": SLATE,
    "labelloc": "t",
    "bgcolor": "white",
    "pad": "0.75",
    "nodesep": "0.75",
    "ranksep": "1.5",
    # Direct, straight-segment connectors (AWS guideline: "straight or clean
    # orthogonal … not tangled diagonals"). ``ortho`` right-angles collide with
    # cross-cluster edge labels here, so we use straight polylines.
    "splines": "polyline",
}
NODE_ATTR = {
    "fontname": "Arial",
    "fontcolor": SLATE,
    "fontsize": "12",
}
EDGE_ATTR = {
    "fontname": "Arial",
    "fontcolor": SLATE,
    "fontsize": "11",
    "color": LINE,
}

# --- Group (container) border treatments -----------------------------------

# AWS Cloud — solid border, labeled.
CLOUD = {
    "style": "solid",
    "pencolor": SLATE,
    "penwidth": "2",
    "bgcolor": "white",
    "fontname": "Arial",
    "fontcolor": SLATE,
    "fontsize": "14",
    "margin": "20",
}
# On-premises / corporate data center — gray, dashed.
ONPREM = {
    "style": "dashed",
    "pencolor": GRAY,
    "penwidth": "2",
    "bgcolor": "#F6F7F8",
    "fontname": "Arial",
    "fontcolor": SLATE,
    "fontsize": "14",
    "margin": "20",
}
# Neutral logical sub-group (not an AWS spec group) — light dashed box.
SUBGROUP = {
    "style": "dashed",
    "pencolor": "#AAB7B8",
    "penwidth": "1",
    "bgcolor": "#FBFBFB",
    "fontname": "Arial",
    "fontcolor": SLATE,
    "fontsize": "12",
    "margin": "16",
}

# Emphasis connector for the store's single, store-initiated outbound mTLS link.
OUTBOUND = "#1B660F"  # dark green
