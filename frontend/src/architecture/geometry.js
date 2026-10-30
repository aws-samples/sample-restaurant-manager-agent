// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Edge geometry for the architecture visualization -- pure functions, no React,
 * no DOM.
 *
 * Because node boxes carry their own coordinates in `flows.js`, connector paths
 * and packet positions are plain arithmetic: no `getBoundingClientRect`, no
 * `getPointAtLength`, no measurement pass, no layout thrash. That also makes all
 * of this unit-testable in isolation (spec 030 design § "layout coordinates live
 * in the flow data").
 *
 * A "path" here is an array of `{x, y}` points describing a polyline.
 */

/**
 * Shared span (in px) two boxes need along an axis before we treat them as
 * row- or column-aligned and draw a straight connector. Below this we fall back
 * to an orthogonal mid-jog, which reads better than a shallow diagonal.
 */
const MIN_ALIGN_OVERLAP = 8

/** Convert a node box `{x, y, w, h}` to edges `{x1, y1, x2, y2}`. */
function bounds(node) {
  return { x1: node.x, y1: node.y, x2: node.x + node.w, y2: node.y + node.h }
}

/** Length of the overlap between spans [a1,a2] and [b1,b2]; negative if disjoint. */
function overlapLength(a1, a2, b1, b2) {
  return Math.min(a2, b2) - Math.max(a1, b1)
}

/** Midpoint of the overlap between spans [a1,a2] and [b1,b2]. */
function overlapMidpoint(a1, a2, b1, b2) {
  return (Math.max(a1, b1) + Math.min(a2, b2)) / 2
}

/**
 * Build the connector polyline between two node boxes.
 *
 * Anchors sit on the box EDGES (not centers), so connectors touch the borders
 * and stay clear of the label text. Routing:
 *
 *   - boxes share a horizontal band  -> one straight horizontal segment
 *   - boxes share a vertical band    -> one straight vertical segment
 *   - otherwise                      -> orthogonal mid-jog (out horizontally,
 *                                       across at the midpoint, then in)
 *
 * The straight cases return 2 points; the mid-jog returns 4.
 *
 * @param {{x:number,y:number,w:number,h:number}} from
 * @param {{x:number,y:number,w:number,h:number}} to
 * @returns {{x:number,y:number}[]} polyline points, first = source anchor
 */
export function edgePath(from, to) {
  const f = bounds(from)
  const t = bounds(to)

  // Row-aligned: a straight horizontal run, provided one box is clearly to the
  // side of the other (not merely overlapping it).
  if (overlapLength(f.y1, f.y2, t.y1, t.y2) >= MIN_ALIGN_OVERLAP) {
    const y = overlapMidpoint(f.y1, f.y2, t.y1, t.y2)
    if (t.x1 >= f.x2) return [{ x: f.x2, y }, { x: t.x1, y }] // to the right
    if (t.x2 <= f.x1) return [{ x: f.x1, y }, { x: t.x2, y }] // to the left
  }

  // Column-aligned: a straight vertical run.
  if (overlapLength(f.x1, f.x2, t.x1, t.x2) >= MIN_ALIGN_OVERLAP) {
    const x = overlapMidpoint(f.x1, f.x2, t.x1, t.x2)
    if (t.y1 >= f.y2) return [{ x, y: f.y2 }, { x, y: t.y1 }] // below
    if (t.y2 <= f.y1) return [{ x, y: f.y1 }, { x, y: t.y2 }] // above
  }

  // Neither: leave horizontally (the layouts read left-to-right), cross at the
  // horizontal midpoint, then come back in horizontally.
  //
  // Antiparallel steps (a request and its response between the same two boxes)
  // deliberately SHARE this path. An earlier revision separated them into
  // parallel lanes to stop their numbered badges colliding, but that made the
  // figure look busy; showing only the active step's badge solves the collision
  // without extra lines. See commit da7a9aa if lanes are ever wanted back.
  const fromCenterX = (f.x1 + f.x2) / 2
  const toCenterX = (t.x1 + t.x2) / 2
  const goRight = toCenterX >= fromCenterX

  const ax = goRight ? f.x2 : f.x1
  const bx = goRight ? t.x1 : t.x2
  const ay = (f.y1 + f.y2) / 2
  const by = (t.y1 + t.y2) / 2
  const mx = (ax + bx) / 2

  return [
    { x: ax, y: ay },
    { x: mx, y: ay },
    { x: mx, y: by },
    { x: bx, y: by },
  ]
}

/** Euclidean distance between two points. */
function distance(a, b) {
  return Math.hypot(b.x - a.x, b.y - a.y)
}

/**
 * Total arc length of a polyline. Needed for the dash-offset "draw-on" effect,
 * which sets `strokeDasharray` to the full length and animates `strokeDashoffset`.
 *
 * @param {{x:number,y:number}[]} points
 * @returns {number} 0 for an empty, single-point, or zero-length path
 */
export function pathLength(points) {
  if (!points || points.length < 2) return 0
  let total = 0
  for (let i = 1; i < points.length; i += 1) total += distance(points[i - 1], points[i])
  return total
}

/**
 * Position along a polyline at fractional progress `t`, by ARC LENGTH -- so a
 * packet moves at constant speed regardless of how the polyline is segmented
 * (interpolating per-segment instead would make it jump at each vertex).
 *
 * `angle` is in degrees, suitable for an SVG `rotate(...)` transform, and is
 * taken from the segment the point falls on.
 *
 * @param {{x:number,y:number}[]} points
 * @param {number} t clamped to [0, 1]; non-finite is treated as 0
 * @returns {{x:number,y:number,angle:number}|null} null only for an empty path
 */
export function pointAtProgress(points, t) {
  if (!points || points.length === 0) return null

  const first = points[0]
  if (points.length === 1) return { x: first.x, y: first.y, angle: 0 }

  const total = pathLength(points)
  // Degenerate: every point coincides, so there is no direction to report.
  if (total === 0) return { x: first.x, y: first.y, angle: 0 }

  const clamped = Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0
  const target = clamped * total

  let travelled = 0
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1]
    const b = points[i]
    const segment = distance(a, b)
    if (segment === 0) continue

    // The final segment takes the `t === 1` case, so we never fall through.
    if (travelled + segment >= target || i === points.length - 1) {
      const local = Math.min(1, (target - travelled) / segment)
      return {
        x: a.x + (b.x - a.x) * local,
        y: a.y + (b.y - a.y) * local,
        angle: (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI,
      }
    }
    travelled += segment
  }

  // Unreachable given the guard above; kept so the contract is total.
  const last = points[points.length - 1]
  return { x: last.x, y: last.y, angle: 0 }
}

/**
 * Format a polyline for an SVG `points` attribute.
 *
 * @param {{x:number,y:number}[]} points
 * @returns {string} e.g. "0,0 10,0 10,10"
 */
export function toPointsAttr(points) {
  if (!points || points.length === 0) return ''
  return points.map((p) => `${p.x},${p.y}`).join(' ')
}

/** Midpoint of a polyline by arc length -- where the numbered step badge sits. */
export function midpoint(points) {
  return pointAtProgress(points, 0.5)
}

// --- Text layout -----------------------------------------------------------

/** Rough advance width per character, as a fraction of font size. */
const CHAR_WIDTH_RATIO = 0.58
/** Horizontal padding reserved inside a node box. */
const LABEL_PADDING = 12

/**
 * Greedy word wrap for node labels.
 *
 * SVG `<text>` does not wrap, so a long label has to be split into `<tspan>`
 * lines ourselves. Widths are ESTIMATED from the character count rather than
 * measured, which keeps rendering a pure function of the flow data -- no
 * measurement pass, no layout thrash. Lives here (not beside the component) so
 * component modules export only components.
 *
 * @param {string} text
 * @param {number} boxWidth node box width in user-space units
 * @param {number} fontSize
 * @returns {string[]} one entry per line; never empty for non-empty input
 */
export function wrapLabel(text, boxWidth, fontSize = 13) {
  const maxChars = Math.max(
    6,
    Math.floor((boxWidth - LABEL_PADDING * 2) / (fontSize * CHAR_WIDTH_RATIO)),
  )
  const words = String(text ?? '')
    .split(/\s+/)
    .filter(Boolean)
  const lines = []
  let line = ''
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word
    if (candidate.length <= maxChars || !line) {
      line = candidate
    } else {
      lines.push(line)
      line = word
    }
  }
  if (line) lines.push(line)
  return lines
}
