// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * ArchitecturePage component tests (spec 030 Task 6).
 *
 * Note on querying: node labels are word-wrapped into multiple `<tspan>`s (SVG
 * text does not wrap), so a long label like "AgentCore Managed Harness" is split
 * across elements and `getByText` would miss it. These tests assert on the
 * COMPOSED text of each node's `<g>` instead, which is what a reader actually
 * sees.
 */
import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ArchitecturePage } from '../ArchitecturePage.jsx'
import { FLOWS, flowById, DEFAULT_FLOW_ID } from '../flows.js'

const flow = flowById(DEFAULT_FLOW_ID)

/** Collapse whitespace so wrapped tspans compare cleanly against the label. */
const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim()

/**
 * Strip ALL whitespace. `textContent` concatenates sibling `<tspan>`s with no
 * separator, so a wrapped label reads as "AgentCore ManagedHarness"; comparing
 * with whitespace removed on both sides makes the assertion independent of where
 * the wrap happens to fall.
 */
const squash = (s) => (s || '').replace(/\s+/g, '')

describe('ArchitecturePage', () => {
  it('renders the diagram canvas', () => {
    render(<ArchitecturePage />)
    expect(screen.getByTestId('flow-canvas')).toBeInTheDocument()
  })

  it('exposes the SVG as an image with an accessible name (AC 5.3)', () => {
    render(<ArchitecturePage />)
    const svg = screen.getByRole('img', { name: /tool round-trip/i })
    expect(svg).toBeInTheDocument()
  })

  it('gives the SVG a description summarizing the flow (AC 5.3)', () => {
    render(<ArchitecturePage />)
    const svg = screen.getByTestId('flow-canvas')
    const descId = svg.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
    expect(normalize(document.getElementById(descId).textContent)).toContain('in-process round-trip')
  })

  it('renders every node label', () => {
    render(<ArchitecturePage />)
    for (const node of flow.nodes) {
      const g = screen.getByTestId(`node-${node.id}`)
      expect(squash(g.textContent), `node "${node.id}" label missing`).toContain(squash(node.label))
    }
  })

  it('renders every node sublabel', () => {
    render(<ArchitecturePage />)
    for (const node of flow.nodes.filter((n) => n.sublabel)) {
      const g = screen.getByTestId(`node-${node.id}`)
      expect(squash(g.textContent)).toContain(squash(node.sublabel))
    }
  })

  it('renders both group labels (AC 3.2)', () => {
    render(<ArchitecturePage />)
    for (const group of flow.groups) {
      const g = screen.getByTestId(`group-${group.id}`)
      expect(squash(g.textContent)).toContain(squash(group.label))
    }
  })

  it('renders the AWS Cloud group solid and the store group dashed (AC 3.2)', () => {
    render(<ArchitecturePage />)
    const cloudRect = screen.getByTestId('group-cloud').querySelector('rect')
    const storeRect = screen.getByTestId('group-store').querySelector('rect')
    expect(cloudRect.getAttribute('stroke-dasharray')).toBeNull()
    expect(storeRect.getAttribute('stroke-dasharray')).toBeTruthy()
  })

  it('renders one connector per step', () => {
    render(<ArchitecturePage />)
    for (const step of flow.steps) {
      expect(screen.getByTestId(`edge-${step.n}`)).toBeInTheDocument()
    }
  })

  it('numbers only the ACTIVE connector, so overlapping badges cannot compete', () => {
    render(<ArchitecturePage />)
    const numbered = flow.steps.filter(
      (step) => normalize(screen.getByTestId(`edge-${step.n}`).textContent) !== '',
    )
    expect(numbered).toHaveLength(1)
    const edge = screen.getByTestId(`edge-${numbered[0].n}`)
    expect(edge.getAttribute('data-active')).toBe('true')
    expect(normalize(edge.textContent)).toBe(String(numbered[0].n))
  })

  it('emphasizes the outbound mTLS hop with the outbound treatment (AC 3.3)', () => {
    render(<ArchitecturePage />)
    const outbound = flow.steps.find((s) => s.kind === 'outbound')
    const edge = screen.getByTestId(`edge-${outbound.n}`)
    expect(edge.getAttribute('data-kind')).toBe('outbound')
    const stroke = edge.querySelector('polyline').getAttribute('stroke')
    expect(stroke).toBe('#1B660F') // OUTBOUND green from _style.py
  })

  it('marks the frozen step active on both its endpoints and its connector', () => {
    render(<ArchitecturePage />)
    const active = document.querySelectorAll('[data-testid^="edge-"][data-active="true"]')
    expect(active).toHaveLength(1)
    const step = flow.steps.find((s) => s.n === Number(active[0].dataset.testid.split('-')[1]))
    expect(screen.getByTestId(`node-${step.from}`).dataset.active).toBe('true')
    expect(screen.getByTestId(`node-${step.to}`).dataset.active).toBe('true')
  })

  it('names the doc source so drift is traceable (AC 2.6)', () => {
    render(<ArchitecturePage />)
    expect(screen.getByText(flow.docRef)).toBeInTheDocument()
  })
})

describe('accessibility invariants (AC 4.7)', () => {
  it('gives every interactive control an accessible name', () => {
    render(<ArchitecturePage />)
    const controls = [
      ...document.querySelectorAll('button'),
      ...document.querySelectorAll('input'),
    ]
    expect(controls.length).toBeGreaterThan(8)
    const unnamed = controls.filter((el) => {
      const name =
        el.getAttribute('aria-label') ||
        el.textContent?.trim() ||
        el.getAttribute('title') ||
        ''
      return name === ''
    })
    expect(
      unnamed.map((el) => el.outerHTML.slice(0, 80)),
      'every control needs an accessible name',
    ).toEqual([])
  })

  it('marks decorative diagram badges aria-hidden so they are not announced', () => {
    render(<ArchitecturePage />)
    const active = document.querySelector('[data-testid^="edge-"][data-active="true"]')
    expect(active.querySelector('[aria-hidden="true"]')).not.toBeNull()
  })
})

describe('flow selector (AC 2.1, 2.5)', () => {
  it('offers all three flows', () => {
    render(<ArchitecturePage />)
    for (const f of FLOWS) {
      expect(screen.getByRole('button', { name: f.label })).toBeInTheDocument()
    }
    expect(FLOWS.map((f) => f.id)).toEqual([
      'tool-round-trip',
      'proactive-alerting',
      'voice-edge',
    ])
  })

  it('marks the selected flow pressed, starting on the tool round-trip', () => {
    render(<ArchitecturePage />)
    expect(screen.getByRole('button', { name: 'Tool round-trip' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Voice edge' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('switches the rendered diagram to the selected flow', () => {
    render(<ArchitecturePage />)
    fireEvent.click(screen.getByRole('button', { name: 'Voice edge' }))

    const voice = flowById('voice-edge')
    // A node unique to the voice flow is now present...
    expect(screen.getByTestId('node-sonic')).toBeInTheDocument()
    // ...and one unique to the tool round-trip is gone.
    expect(screen.queryByTestId('node-harness')).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: /voice edge/i })).toBeInTheDocument()
    expect(screen.getByText(voice.docRef)).toBeInTheDocument()
  })

  it('resets the timeline to step 1 when the flow changes (AC 2.5)', () => {
    render(<ArchitecturePage />)
    // Move off step 1 first.
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    fireEvent.click(screen.getByRole('button', { name: /next step/i }))
    expect(screen.getByTestId('step-caption').textContent).toMatch(/Step 3 of/)

    fireEvent.click(screen.getByRole('button', { name: 'Proactive alerting' }))
    expect(screen.getByTestId('step-caption').textContent).toMatch(/Step 1 of 8/)
  })

  it('renders the proactive flow with its dashed acknowledgment edge', () => {
    render(<ArchitecturePage />)
    fireEvent.click(screen.getByRole('button', { name: 'Proactive alerting' }))
    const ack = flowById('proactive-alerting').steps.find((s) => s.kind === 'ack')
    expect(screen.getByTestId(`edge-${ack.n}`).getAttribute('data-kind')).toBe('ack')
  })
})
