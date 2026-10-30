// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { NavLink } from 'react-router-dom'

export function NavBar() {
  const linkClasses = ({ isActive }) =>
    `px-3 py-1 rounded text-sm font-medium transition-colors ${
      isActive
        ? 'bg-gray-700 text-white'
        : 'text-gray-400 hover:text-white'
    }`

  return (
    <nav className="flex flex-wrap items-center justify-between gap-2 bg-gray-800 px-4 py-3 mb-4 rounded-lg">
      <span className="text-lg font-bold text-white">🍽 Restaurant Simulator</span>
      <div className="flex flex-wrap gap-2">
        {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
        {/* nosemgrep: jsx-not-internationalized */}
        <NavLink to="/" end className={linkClasses}>
          Dashboard
        </NavLink>
        {/* nosemgrep: jsx-not-internationalized */}
        <NavLink to="/chat" className={linkClasses}>
          Chat
        </NavLink>
        {/* nosemgrep: jsx-not-internationalized */}
        <NavLink to="/architecture" className={linkClasses}>
          Architecture
        </NavLink>
        {/* nosemgrep: jsx-not-internationalized */}
        <NavLink to="/voice" className={linkClasses}>
          Voice
        </NavLink>
      </div>
    </nav>
  )
}
