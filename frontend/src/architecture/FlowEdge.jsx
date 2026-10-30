// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * One connector (step) in the architecture diagram (spec 030).
 *
 * Stroke, weight and dash come from the step's `kind`, so the store's outbound
 * mTLS link reads as emphasized (green, heavier) and return paths read as
 * distinct from forward paths -- AC 3.3 / 3.4.
 *
 * When the step is active the connector "draws on" proportionally to
 * `progress` and a packet marker rides along it, positioned by arc-length
 * interpolation (so it moves at constant speed across a multi-segment path).
 */
import { edgePath, midpoint, pathLength, pointAtProgress, toPointsAttr } from './geometry.js'
import { arrowMarkerId, INACTIVE_EDGE_OPACITY, SLATE, STEP_STYLES } from './theme.js'

/**
 * Opacity of the base line under the bright draw-on overlay.
 *
 * Deliberately well above `INACTIVE_EDGE_OPACITY`: at progress 0 the overlay has
 * zero length, so this value alone distinguishes the active connector. Set too
 * close to the inactive opacity, a paused step -- which is exactly what a
 * presenter looks at while explaining a hop -- shows no visible active line.
 */
const BASE_UNDER_ACTIVE_OPACITY = 0.55

export default function FlowEdge({ step, fromNode, toNode, active = false, progress = 0 }) {
  const style = STEP_STYLES[step.kind] ?? STEP_STYLES.request
  const points = edgePath(fromNode, toNode)
  const badge = midpoint(points)
  const length = pathLength(points)
  const packet = active ? pointAtProgress(points, progress) : null

  // A dashed kind (`ack`) already uses strokeDasharray for its pattern, so the
  // dash-offset draw-on would fight it. Those edges keep their pattern and rely
  // on the packet alone to show progress.
  const drawOn = active && !style.dash && length > 0

  return (
    <g
      data-testid={`edge-${step.n}`}
      data-kind={step.kind}
      data-active={active ? 'true' : 'false'}
      opacity={active ? 1 : INACTIVE_EDGE_OPACITY}
    >
      <polyline
        points={toPointsAttr(points)}
        fill="none"
        stroke={style.stroke}
        strokeWidth={style.strokeWidth}
        strokeDasharray={style.dash ?? undefined}
        strokeLinejoin="round"
        strokeLinecap="round"
        markerEnd={`url(#${arrowMarkerId(step.kind)})`}
        opacity={drawOn ? BASE_UNDER_ACTIVE_OPACITY : 1}
      />

      {/* Progress overlay: the full-strength line revealed from the source end. */}
      {drawOn && (
        <polyline
          data-testid={`edge-${step.n}-progress`}
          points={toPointsAttr(points)}
          fill="none"
          stroke={style.stroke}
          strokeWidth={style.strokeWidth + 1}
          strokeLinejoin="round"
          strokeLinecap="round"
          strokeDasharray={length}
          strokeDashoffset={length * (1 - Math.min(1, Math.max(0, progress)))}
        />
      )}

      {/* The moving packet. */}
      {packet && (
        <circle
          data-testid={`edge-${step.n}-packet`}
          cx={packet.x}
          cy={packet.y}
          r={6}
          fill={style.stroke}
          stroke="#FFFFFF"
          strokeWidth={2}
        />
      )}

      {/* Numbered badge for the ACTIVE step only. Many steps connect the same
          two boxes (a request and its response), so drawing every badge stacks
          numbers on one point and makes them unreadable; showing just the active
          one keeps the figure clean and matches how the caption reads. */}
      {badge && active && (
        <g aria-hidden="true">
          <circle
            cx={badge.x}
            cy={badge.y}
            r={12}
            fill="#FFFFFF"
            stroke={style.stroke}
            strokeWidth={2}
          />
          <text
            x={badge.x}
            y={badge.y + 4}
            textAnchor="middle"
            fontFamily="Arial, Helvetica, sans-serif"
            fontSize={12}
            fontWeight="bold"
            fill={SLATE}
          >
            {step.n}
          </text>
        </g>
      )}
    </g>
  )
}
