// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Spec 031 / threat model TM-02 — the confirm/decline prompt for destructive
// tools.
//
// The backend already refuses to execute a gated tool without approval, and that
// is tested server-side. What can only be tested here is whether a manager can
// actually SEE and ACT on the prompt: if the UI silently drops the
// `confirmation_required` state, `void_order` becomes unreachable rather than
// gated, which is a usability regression disguised as a security feature.
//
// These tests drive the real `useChat` hook through a stubbed `fetch`, so the
// polling and confirm plumbing is exercised rather than mocked away.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { ChatPage } from '../components/ChatPage'

const PENDING = {
  status: 'confirmation_required',
  pending: {
    pending_id: 'pend-123',
    tool_name: 'void_order',
    parameters: { order_id: 41, reason: 'spill' },
    expires_in_s: 88,
  },
}

/** Stub fetch: POST /api/chat -> 202, poll -> scripted, confirm -> recorded. */
function stubFetch({ pollBodies, confirmStatus = 202 }) {
  const calls = { confirm: [] }
  let pollIndex = 0
  globalThis.fetch = vi.fn(async (url, init) => {
    const target = String(url)
    if (target === '/api/chat') {
      return {
        ok: true,
        status: 202,
        json: async () => ({ executionId: 'exec-1', sessionId: 's'.repeat(40) }),
      }
    }
    if (target === '/api/chat/confirm') {
      calls.confirm.push(JSON.parse(init.body))
      return { ok: confirmStatus === 202, status: confirmStatus, json: async () => ({}) }
    }
    if (target.startsWith('/api/chat/result/')) {
      const body = pollBodies[Math.min(pollIndex, pollBodies.length - 1)]
      pollIndex += 1
      return { ok: true, status: 200, json: async () => body }
    }
    if (target.startsWith('/api/chat/history/')) {
      return { ok: true, status: 200, json: async () => ({ messages: [] }) }
    }
    throw new Error(`unexpected fetch: ${target}`)
  })
  return calls
}

// fireEvent rather than user-event: the repo already uses it (see
// architecture/__tests__) and a sample should not grow a dependency for a click.
async function askSomething() {
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: 'void order 41' } })
  fireEvent.click(screen.getByRole('button', { name: /send/i }))
}

function clickButton(name) {
  fireEvent.click(screen.getByRole('button', { name }))
}

beforeEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  // jsdom does not implement scrollIntoView, and ChatPage auto-scrolls on every
  // message. Stubbed locally rather than in the shared setup so this file's
  // requirements stay visible at the point of use.
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('destructive-tool confirmation prompt', () => {
  it('renders the tool name and the exact parameters that will run', async () => {
    stubFetch({ pollBodies: [PENDING] })
    render(<ChatPage />)
    await askSomething()

    await waitFor(
      () => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(),
      { timeout: 6000 }
    )
    const prompt = screen.getByTestId('confirmation-prompt')
    expect(prompt).toHaveTextContent('void_order')
    // The manager must approve exactly what executes, not a paraphrase.
    expect(prompt).toHaveTextContent('order_id')
    expect(prompt).toHaveTextContent('41')
    expect(prompt).toHaveTextContent('reason')
    expect(prompt).toHaveTextContent('spill')
    // And be told it is not free-form.
    expect(prompt).toHaveTextContent(/not easily undone/i)
  }, 15000)

  it('sends approve with the pending id when Confirm is clicked', async () => {
    const calls = stubFetch({
      pollBodies: [PENDING, { status: 'succeeded', response: 'Order 41 voided.' }],
    })
    render(<ChatPage />)
    await askSomething()

    await waitFor(() => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(), {
      timeout: 6000,
    })
    clickButton(/^confirm$/i)

    await waitFor(() => expect(calls.confirm).toHaveLength(1), { timeout: 6000 })
    expect(calls.confirm[0]).toEqual({ pending_id: 'pend-123', decision: 'approve' })
  }, 15000)

  it('sends deny when Decline is clicked, and never approve', async () => {
    const calls = stubFetch({
      pollBodies: [PENDING, { status: 'succeeded', response: 'I did not void it.' }],
    })
    render(<ChatPage />)
    await askSomething()

    await waitFor(() => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(), {
      timeout: 6000,
    })
    clickButton(/decline/i)

    await waitFor(() => expect(calls.confirm).toHaveLength(1), { timeout: 6000 })
    expect(calls.confirm[0].decision).toBe('deny')
    expect(calls.confirm.some((c) => c.decision === 'approve')).toBe(false)
  }, 15000)

  it('removes the prompt on click so it cannot be submitted twice from the UI', async () => {
    const calls = stubFetch({
      pollBodies: [PENDING, { status: 'succeeded', response: 'done' }],
    })
    render(<ChatPage />)
    await askSomething()

    await waitFor(() => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(), {
      timeout: 6000,
    })
    clickButton(/^confirm$/i)
    await waitFor(() =>
      expect(screen.queryByTestId('confirmation-prompt')).not.toBeInTheDocument()
    )
    // The server enforces single-use independently; this is defence in depth.
    expect(calls.confirm).toHaveLength(1)
  }, 15000)

  it('tells the manager nothing was changed when the window expired', async () => {
    stubFetch({
      pollBodies: [
        PENDING,
        {
          status: 'failed',
          error: 'The confirmation window expired, so nothing was changed.',
        },
      ],
    })
    render(<ChatPage />)
    await askSomething()

    await waitFor(() => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(), {
      timeout: 6000,
    })
    // Expiry must take the prompt down AND say so -- a manager believing a void
    // happened when it did not is the worst available outcome.
    await waitFor(
      () => expect(screen.getByText(/nothing was changed/i)).toBeInTheDocument(),
      { timeout: 8000 }
    )
    expect(screen.queryByTestId('confirmation-prompt')).not.toBeInTheDocument()
  }, 20000)

  it('reports a stale confirmation instead of pretending it worked', async () => {
    stubFetch({ pollBodies: [PENDING], confirmStatus: 410 })
    render(<ChatPage />)
    await askSomething()

    await waitFor(() => expect(screen.getByTestId('confirmation-prompt')).toBeInTheDocument(), {
      timeout: 6000,
    })
    clickButton(/^confirm$/i)
    await waitFor(
      () => expect(screen.getByText(/expired, so nothing was changed/i)).toBeInTheDocument(),
      { timeout: 6000 }
    )
  }, 15000)
})
