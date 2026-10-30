// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Geometry unit tests (spec 030 Task 4).
 *
 * `pointAtProgress` is the function the packet animation rides on, so the cases
 * that matter are the endpoints, a straight-segment midpoint, a multi-segment
 * path (proving interpolation is by ARC LENGTH, not per-segment), and degenerate
 * input.
 */
import { describe, it, expect } from 'vitest'

import {
  edgePath,
  pathLength,
  pointAtProgress,
  toPointsAttr,
  midpoint,
} from '../geometry.js'
import { flowById } from '../flows.js'

/** Helper: build a node box. */
const box = (x, y, w = 100, h = 50) => ({ x, y, w, h })

describe('edgePath', () => {
  it('draws a straight horizontal run between row-aligned boxes, anchored on their edges', () => {
    const from = box(0, 100)
    const to = box(300, 100)
    expect(edgePath(from, to)).toEqual([
      { x: 100, y: 125 }, // right edge of `from`
      { x: 300, y: 125 }, // left edge of `to`
    ])
  })

  it('draws right-to-left when the target is to the left', () => {
    const from = box(300, 100)
    const to = box(0, 100)
    expect(edgePath(from, to)).toEqual([
      { x: 300, y: 125 }, // left edge of `from`
      { x: 100, y: 125 }, // right edge of `to`
    ])
  })

  it('draws a straight vertical run between column-aligned boxes', () => {
    const from = box(0, 0)
    const to = box(0, 200)
    expect(edgePath(from, to)).toEqual([
      { x: 50, y: 50 }, // bottom edge of `from`
      { x: 50, y: 200 }, // top edge of `to`
    ])
  })

  it('draws bottom-to-top when the target is above', () => {
    const from = box(0, 200)
    const to = box(0, 0)
    expect(edgePath(from, to)).toEqual([
      { x: 50, y: 200 }, // top edge of `from`
      { x: 50, y: 50 }, // bottom edge of `to`
    ])
  })

  it('inserts an orthogonal mid-jog when boxes are neither row- nor column-aligned', () => {
    const from = box(0, 0)
    const to = box(300, 200)
    const path = edgePath(from, to)
    expect(path).toHaveLength(4)
    expect(path[0]).toEqual({ x: 100, y: 25 }) // out of `from`'s right edge
    expect(path[3]).toEqual({ x: 300, y: 225 }) // into `to`'s left edge
    // The jog is orthogonal: across at a shared x, then down.
    expect(path[1].x).toBe(path[2].x)
    expect(path[1].y).toBe(path[0].y)
    expect(path[2].y).toBe(path[3].y)
  })

  it('is a pure function of the two boxes (repeatable)', () => {
    const a = box(10, 10)
    const b = box(400, 300)
    expect(edgePath(a, b)).toEqual(edgePath(a, b))
  })
})

describe('edgePath against real flow data', () => {
  const flow = flowById('tool-round-trip')
  const node = (id) => flow.nodes.find((n) => n.id === id)

  it('routes the emphasized outbound hop (IoT Core -> store gateway) as one straight horizontal', () => {
    const path = edgePath(node('iot'), node('gateway'))
    expect(path).toHaveLength(2)
    expect(path[0].y).toBe(path[1].y)
    expect(path[0].x).toBe(756) // iot right edge (548 + 208)
    expect(path[1].x).toBe(864) // gateway left edge
  })

  it('routes gateway -> MCP tool as one straight vertical', () => {
    const path = edgePath(node('gateway'), node('tool'))
    expect(path).toHaveLength(2)
    expect(path[0].x).toBe(path[1].x)
    expect(path[0].y).toBe(384) // gateway top edge
    expect(path[1].y).toBe(276) // tool bottom edge (180 + 96)
  })

  it('routes chat backend -> harness with a mid-jog', () => {
    expect(edgePath(node('chat'), node('harness'))).toHaveLength(4)
  })
})

describe('pathLength', () => {
  it('sums segment lengths', () => {
    expect(pathLength([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 20 }])).toBe(30)
  })

  it('is 0 for empty, single-point, and zero-length paths', () => {
    expect(pathLength([])).toBe(0)
    expect(pathLength([{ x: 5, y: 5 }])).toBe(0)
    expect(pathLength([{ x: 5, y: 5 }, { x: 5, y: 5 }])).toBe(0)
    expect(pathLength(undefined)).toBe(0)
  })
})

describe('pointAtProgress', () => {
  const straight = [{ x: 0, y: 0 }, { x: 10, y: 0 }]
  // An L: 10 units right, then 20 units down. Total arc length 30.
  const elbow = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 20 }]

  it('returns the first point at t = 0', () => {
    expect(pointAtProgress(straight, 0)).toEqual({ x: 0, y: 0, angle: 0 })
  })

  it('returns the last point at t = 1', () => {
    expect(pointAtProgress(straight, 1)).toEqual({ x: 10, y: 0, angle: 0 })
  })

  it('returns the midpoint of a straight segment at t = 0.5', () => {
    expect(pointAtProgress(straight, 0.5)).toEqual({ x: 5, y: 0, angle: 0 })
  })

  it('interpolates a multi-segment path by arc length, not per segment', () => {
    // t=0.5 of 30 units => 15 units in => 5 units down the second segment.
    // A per-segment implementation would wrongly land at the (10,0) vertex.
    const p = pointAtProgress(elbow, 0.5)
    expect(p.x).toBe(10)
    expect(p.y).toBe(5)
    expect(p.angle).toBe(90) // heading straight down
  })

  it('reaches the far end of a multi-segment path at t = 1', () => {
    const p = pointAtProgress(elbow, 1)
    expect(p.x).toBe(10)
    expect(p.y).toBe(20)
  })

  it('reports the heading in degrees for the segment it lands on', () => {
    expect(pointAtProgress(elbow, 0.1).angle).toBe(0) // still on the horizontal leg
    expect(pointAtProgress([{ x: 0, y: 0 }, { x: -10, y: 0 }], 0.5).angle).toBe(180)
  })

  it('clamps t outside [0, 1] and treats non-finite t as 0', () => {
    expect(pointAtProgress(straight, -5)).toEqual({ x: 0, y: 0, angle: 0 })
    expect(pointAtProgress(straight, 42)).toEqual({ x: 10, y: 0, angle: 0 })
    expect(pointAtProgress(straight, Number.NaN)).toEqual({ x: 0, y: 0, angle: 0 })
  })

  it('handles degenerate input: empty, single point, and zero length', () => {
    expect(pointAtProgress([], 0.5)).toBeNull()
    expect(pointAtProgress(undefined, 0.5)).toBeNull()
    expect(pointAtProgress([{ x: 3, y: 4 }], 0.5)).toEqual({ x: 3, y: 4, angle: 0 })
    // Coincident points: a position exists, but no direction to report.
    expect(pointAtProgress([{ x: 5, y: 5 }, { x: 5, y: 5 }], 0.5)).toEqual({
      x: 5,
      y: 5,
      angle: 0,
    })
  })

  it('skips zero-length segments inside an otherwise real path', () => {
    const withDupe = [{ x: 0, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 }]
    expect(pointAtProgress(withDupe, 0.5)).toEqual({ x: 5, y: 0, angle: 0 })
  })
})

describe('midpoint', () => {
  it('is the arc-length midpoint', () => {
    expect(midpoint([{ x: 0, y: 0 }, { x: 10, y: 0 }])).toEqual({ x: 5, y: 0, angle: 0 })
  })
})

describe('toPointsAttr', () => {
  it('formats points for an SVG points attribute', () => {
    expect(toPointsAttr([{ x: 0, y: 0 }, { x: 10, y: 20 }])).toBe('0,0 10,20')
  })

  it('returns an empty string for empty input', () => {
    expect(toPointsAttr([])).toBe('')
    expect(toPointsAttr(undefined)).toBe('')
  })
})
