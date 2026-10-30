// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Harness smoke test — spec 030 Task 2.
//
// Proves the whole test chain is wired: vitest runs this file, the jsdom
// environment provides a DOM, @testing-library/react can render a component
// into it, and the @testing-library/jest-dom matchers (toBeInTheDocument) are
// registered by src/test/setup.js.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

function Hello() {
  return <p>harness ok</p>
}

describe('frontend test harness', () => {
  it('runs vitest with a jsdom environment', () => {
    expect(typeof document).toBe('object')
    expect(document.body).toBeTruthy()
  })

  it('renders a React component and exposes jest-dom matchers', () => {
    render(<Hello />)
    expect(screen.getByText('harness ok')).toBeInTheDocument()
  })
})
