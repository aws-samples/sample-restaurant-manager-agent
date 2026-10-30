// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Vitest setup — spec 030 (frontend test harness).
//
// Registers @testing-library/jest-dom's custom matchers (toBeInTheDocument,
// toHaveAttribute, ...) on Vitest's expect, and unmounts React trees between
// tests so component tests don't leak DOM state into each other.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
