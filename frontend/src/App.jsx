// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { useSimulator } from './hooks/useSimulator'
import { KPIBar } from './components/KPIBar'
import { SimControls } from './components/SimControls'
import { OrdersPanel } from './components/OrdersPanel'
import { EquipmentPanel } from './components/EquipmentPanel'
import { InventoryPanel } from './components/InventoryPanel'
import { StaffPanel } from './components/StaffPanel'
import { AlertsPanel } from './components/AlertsPanel'
import { AlertsBanner } from './components/AlertsBanner'
import { NavBar } from './components/NavBar'
import { ChatPage } from './components/ChatPage'
import { ArchitecturePage } from './architecture/ArchitecturePage'
import { VoicePage } from './components/VoicePage'

const TABS = ['Orders', 'Equipment', 'Inventory', 'Staff', 'Alerts']

function Dashboard() {
  const { state, connected, sendControl } = useSimulator()
  const [tab, setTab] = useState('Orders')

  const ackAlert = async (alertId) => {
    await fetch(`/api/alerts/${encodeURIComponent(alertId)}/ack`, { method: 'POST' })
    // The acknowledgment echoes back over the WebSocket ('alerts' domain event),
    // so the banner/badge update without an optimistic local mutation.
  }

  const unacked = (state?.alerts?.entries || []).filter(a => !a.acknowledged).length

  return (
    <>
      <SimControls state={state} sendControl={sendControl} connected={connected} />
      <KPIBar state={state} />

      {/* Proactive alerts surface unsolicited on the always-on display (spec 023). */}
      <AlertsBanner alerts={state?.alerts} onAck={ackAlert} />

      <div className="flex gap-1 mb-4">
        {TABS.map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-t text-sm font-medium ${tab === t ? 'bg-gray-700 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
          >
            {t}
            {t === 'Alerts' && unacked > 0 && (
              <span className="ml-2 rounded-full bg-red-600 px-1.5 py-0.5 text-xs text-white">{unacked}</span>
            )}
          </button>
        ))}
      </div>

      <div className="bg-gray-800 rounded-b rounded-tr p-4">
        {tab === 'Orders' && <OrdersPanel pos={state?.pos} />}
        {tab === 'Equipment' && <EquipmentPanel equipment={state?.equipment} />}
        {tab === 'Inventory' && <InventoryPanel inventory={state?.inventory} />}
        {tab === 'Staff' && <StaffPanel schedule={state?.schedule} />}
        {tab === 'Alerts' && <AlertsPanel alerts={state?.alerts} onAck={ackAlert} />}
      </div>
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-900 text-white p-4 max-w-6xl mx-auto">
        <NavBar />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/architecture" element={<ArchitecturePage />} />
          <Route path="/voice" element={<VoicePage />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}
