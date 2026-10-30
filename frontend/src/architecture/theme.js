// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Palette + connector treatments for the architecture visualization.
 *
 * SOURCE OF TRUTH: `docs/solution-guidance/diagrams/_style.py`.
 *
 * These constants are a deliberate mirror of that module (spec 030 AC 3.1, 3.5)
 * so this page and the published PNG figures in `docs/solution-guidance/diagrams/`
 * read as the same architecture. If `_style.py` changes, change this too --
 * nothing enforces the link at build time.
 */

// --- Base palette (verbatim from _style.py) --------------------------------

/** AWS label / dark slate. `_style.py: SLATE` */
export const SLATE = '#232F3E'
/** On-premises group border. `_style.py: GRAY` */
export const GRAY = '#7D8998'
/** Default connector. `_style.py: LINE` */
export const LINE = '#5A6B7B'
/** The store's single store-initiated outbound mTLS link. `_style.py: OUTBOUND` */
export const OUTBOUND = '#1B660F'
/**
 * Return-path connector. Defined per-figure in the diagram scripts rather than
 * in `_style.py` (see `diagram3_tool_invocation_flow.py: BLUE`).
 */
export const BLUE = '#245A8D'

// --- Group (container) fills and borders -----------------------------------

/** AWS Cloud cluster: white fill, solid 2px slate border. `_style.py: CLOUD` */
export const CLOUD_FILL = '#FFFFFF'
/** On-premises cluster: light fill, dashed 2px gray border. `_style.py: ONPREM` */
export const ONPREM_FILL = '#F6F7F8'
/** Neutral logical sub-group. `_style.py: SUBGROUP` */
export const SUBGROUP_STROKE = '#AAB7B8'
export const SUBGROUP_FILL = '#FBFBFB'

/**
 * Border treatment per group kind. `cloud` is solid (an AWS spec group);
 * `onprem` is dashed (AC 3.2).
 */
export const GROUP_STYLES = {
  cloud: { stroke: SLATE, strokeWidth: 2, dash: null, fill: CLOUD_FILL },
  onprem: { stroke: GRAY, strokeWidth: 2, dash: '8 5', fill: ONPREM_FILL },
  subgroup: { stroke: SUBGROUP_STROKE, strokeWidth: 1, dash: '4 4', fill: SUBGROUP_FILL },
}

/**
 * Connector treatment per step kind (spec 030 design § Data model).
 *
 *   request  — forward path, neutral connector
 *   response — return path, blue
 *   outbound — the store's outbound mTLS link: green and heavier, because
 *              "outbound-only" is the load-bearing property of the pattern (AC 3.3)
 *   ack      — dashed blue, an out-of-band resume (`SendTaskSuccess`)
 */
export const STEP_STYLES = {
  request: { stroke: LINE, strokeWidth: 2, dash: null },
  response: { stroke: BLUE, strokeWidth: 2, dash: null },
  outbound: { stroke: OUTBOUND, strokeWidth: 3.5, dash: null },
  ack: { stroke: BLUE, strokeWidth: 2, dash: '7 5' },
}

/** Valid `step.kind` values -- used by the flow schema test. */
export const STEP_KINDS = Object.keys(STEP_STYLES)
/** Valid `group.kind` values. */
export const GROUP_KINDS = Object.keys(GROUP_STYLES)

/**
 * SVG `<marker>` id for a step kind's arrowhead. Lives here (not beside the
 * component) so the component modules export only components, which is what
 * react-refresh requires. Must match the `<defs>` FlowCanvas renders.
 */
export const arrowMarkerId = (kind) => `arrow-${kind}`

/** Node fill by the kind of group the node sits in (actors sit in no group). */
export const NODE_FILL = {
  cloud: '#FFFFFF',
  onprem: '#FFFFFF',
  actor: '#EEF1F4',
}

/** Opacity applied to connectors that are not the active step. */
export const INACTIVE_EDGE_OPACITY = 0.28
