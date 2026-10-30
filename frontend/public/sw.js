// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Web Push service worker (spec 025 Task 1d). Shows a proactive alert
// notification when the always-on display is backgrounded/closed; clicking it
// focuses the display (which reconnects and speaks the alert). No app imports.
self.addEventListener('push', (event) => {
  let data = {}
  try { data = event.data ? event.data.json() : {} } catch { data = {} }
  const title = data.title || 'Restaurant alert'
  const severity = data.severity || 'info'
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || 'New alert',
      tag: data.alert_id || undefined,            // collapse re-fires of one alert
      renotify: severity === 'critical',
      requireInteraction: severity === 'critical',
      data: { url: data.url || '/voice', alert_id: data.alert_id },
    })
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data && event.notification.data.url) || '/voice'
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const c of wins) {
      if ('focus' in c) {
        await c.focus()
        if ('navigate' in c) { try { await c.navigate(url) } catch { /* same-origin only */ } }
        return
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(url)
  })())
})
