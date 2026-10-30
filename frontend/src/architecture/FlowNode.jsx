// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * One node box in the architecture diagram (spec 030).
 *
 * A labeled rounded rect, category-colored by the kind of group it sits in.
 * Deliberately not AWS icon art -- no usable icon assets exist in the repo, and
 * dropping real Architecture Icon SVGs in later is additive (design decision #6).
 */
import { wrapLabel } from './geometry.js'
import { BLUE, NODE_FILL, SLATE } from './theme.js'

const LABEL_SIZE = 13
const SUBLABEL_SIZE = 11
const LINE_HEIGHT = 15

export default function FlowNode({ node, groupKind = 'actor', active = false }) {
  const cx = node.x + node.w / 2
  const fill = NODE_FILL[groupKind] ?? NODE_FILL.actor
  const lines = wrapLabel(node.label, node.w)

  // Center the label block, then hang the sublabel below it.
  const blockHeight = lines.length * LINE_HEIGHT + (node.sublabel ? LINE_HEIGHT : 0)
  const firstBaseline = node.y + node.h / 2 - blockHeight / 2 + LINE_HEIGHT * 0.8

  return (
    <g data-testid={`node-${node.id}`} data-active={active ? 'true' : 'false'}>
      {/* Highlight ring when this node is the active step's source or target. */}
      {active && (
        <rect
          x={node.x - 5}
          y={node.y - 5}
          width={node.w + 10}
          height={node.h + 10}
          rx={12}
          fill="none"
          stroke={BLUE}
          strokeWidth={2.5}
          opacity={0.9}
        />
      )}
      <rect
        x={node.x}
        y={node.y}
        width={node.w}
        height={node.h}
        rx={8}
        fill={fill}
        stroke={active ? BLUE : SLATE}
        strokeWidth={active ? 2 : 1.5}
      />
      <text
        x={cx}
        textAnchor="middle"
        fontFamily="Arial, Helvetica, sans-serif"
        fill={SLATE}
      >
        {lines.map((line, i) => (
          <tspan key={line} x={cx} y={firstBaseline + i * LINE_HEIGHT} fontSize={LABEL_SIZE}>
            {line}
          </tspan>
        ))}
        {node.sublabel && (
          <tspan
            x={cx}
            y={firstBaseline + lines.length * LINE_HEIGHT}
            fontSize={SUBLABEL_SIZE}
            opacity={0.72}
          >
            {node.sublabel}
          </tspan>
        )}
      </text>
    </g>
  )
}
