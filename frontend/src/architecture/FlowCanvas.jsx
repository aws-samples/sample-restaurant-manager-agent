// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The diagram surface (spec 030).
 *
 * Renders, in order: group containers, then all edges, then nodes -- so node
 * boxes overlay connectors rather than being crossed by them. The SVG scales via
 * `viewBox` + `preserveAspectRatio` with no JS measurement.
 *
 * This is a deliberately LIGHT panel inside the dashboard's dark chrome: the
 * published figures are light-on-white, and recoloring them here would lose the
 * fidelity that is the whole point (design decision #5).
 */
import FlowEdge from './FlowEdge.jsx'
import FlowNode from './FlowNode.jsx'
import { arrowMarkerId, GROUP_STYLES, SLATE, STEP_STYLES } from './theme.js'

const GROUP_LABEL_SIZE = 14

/** One arrowhead marker per step kind, so each inherits its connector color. */
function ArrowDefs() {
  return (
    <defs>
      {Object.entries(STEP_STYLES).map(([kind, style]) => (
        <marker
          key={kind}
          id={arrowMarkerId(kind)}
          viewBox="0 0 10 10"
          refX={9}
          refY={5}
          markerWidth={6}
          markerHeight={6}
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill={style.stroke} />
        </marker>
      ))}
    </defs>
  )
}

function GroupContainer({ group }) {
  const style = GROUP_STYLES[group.kind] ?? GROUP_STYLES.subgroup
  return (
    <g data-testid={`group-${group.id}`} data-kind={group.kind}>
      <rect
        x={group.x}
        y={group.y}
        width={group.w}
        height={group.h}
        rx={10}
        fill={style.fill}
        stroke={style.stroke}
        strokeWidth={style.strokeWidth}
        strokeDasharray={style.dash ?? undefined}
      />
      <text
        x={group.x + 16}
        y={group.y + 24}
        fontFamily="Arial, Helvetica, sans-serif"
        fontSize={GROUP_LABEL_SIZE}
        fill={SLATE}
        opacity={0.85}
      >
        {group.label}
      </text>
    </g>
  )
}

export default function FlowCanvas({ flow, activeStepN = null, progress = 0 }) {
  const nodeById = new Map(flow.nodes.map((n) => [n.id, n]))
  const groupById = new Map(flow.groups.map((g) => [g.id, g]))
  const activeStep = flow.steps.find((s) => s.n === activeStepN) ?? null

  const titleId = `flow-title-${flow.id}`
  const descId = `flow-desc-${flow.id}`

  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      aria-describedby={descId}
      viewBox={`0 0 ${flow.viewBox.w} ${flow.viewBox.h}`}
      preserveAspectRatio="xMidYMid meet"
      className="w-full h-auto rounded-lg bg-white"
      data-testid="flow-canvas"
    >
      <title id={titleId}>{`${flow.label} — architecture flow`}</title>
      <desc id={descId}>
        {activeStep
          ? `${flow.subtitle}. Step ${activeStep.n} of ${flow.steps.length}: ${activeStep.label}.`
          : flow.subtitle}
      </desc>

      <ArrowDefs />

      {flow.groups.map((group) => (
        <GroupContainer key={group.id} group={group} />
      ))}

      {flow.steps.map((step) => (
        <FlowEdge
          key={step.n}
          step={step}
          fromNode={nodeById.get(step.from)}
          toNode={nodeById.get(step.to)}
          active={step.n === activeStepN}
          progress={step.n === activeStepN ? progress : 0}
        />
      ))}

      {flow.nodes.map((node) => (
        <FlowNode
          key={node.id}
          node={node}
          groupKind={node.group ? (groupById.get(node.group)?.kind ?? 'actor') : 'actor'}
          active={
            activeStep ? node.id === activeStep.from || node.id === activeStep.to : false
          }
        />
      ))}
    </svg>
  )
}
