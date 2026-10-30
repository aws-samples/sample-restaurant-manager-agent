// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState, useCallback, useEffect, useRef } from 'react'

const STORAGE_KEY = 'chat_session_id'

// Polling cadence per design decision D4.
const POLL_INTERVAL_MS = 2000
const POLL_JITTER_MS = 250
const POLL_TIMEOUT_MS = 300_000

export function useChat() {
  const [messages, setMessages] = useState([])
  const [isProcessing, setIsProcessing] = useState(false)
  // spec 031: set while a destructive tool awaits the manager's approval.
  const [pendingConfirmation, setPendingConfirmation] = useState(null)
  const [sessionId, setSessionId] = useState(() => {
    return localStorage.getItem(STORAGE_KEY) || null
  })

  const hasPreviousSession = !!localStorage.getItem(STORAGE_KEY)

  // Polling refs. Kept outside of state so that cancelling them from
  // the unmount-cleanup effect does not trigger a re-render.
  const pollTimeoutRef = useRef(null)
  const pollAbortRef = useRef(null)
  const pollDeadlineRef = useRef(0)
  const isMountedRef = useRef(true)

  const cancelPoll = useCallback(() => {
    if (pollTimeoutRef.current !== null) {
      clearTimeout(pollTimeoutRef.current)
      pollTimeoutRef.current = null
    }
    if (pollAbortRef.current !== null) {
      try {
        pollAbortRef.current.abort()
      } catch {
        /* ignore */
      }
      pollAbortRef.current = null
    }
  }, [])

  // Unmount-cleanup: cancel any in-flight poll when the hook unmounts
  // so that background network traffic cannot leak past the component
  // (Requirement 7.8).
  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
      cancelPoll()
    }
  }, [cancelPoll])

  const appendError = useCallback((content) => {
    setMessages((prev) => [
      ...prev.filter((m) => !m._thinking),
      {
        role: 'error',
        content,
        timestamp: new Date().toISOString(),
      },
    ])
  }, [])

  const doPoll = useCallback(
    async (executionId, pollSessionId) => {
      if (!isMountedRef.current) return

      const controller = new AbortController()
      pollAbortRef.current = controller

      let res
      try {
        const url = `/api/chat/result/${encodeURIComponent(
          executionId
        )}?session_id=${encodeURIComponent(pollSessionId)}`
        res = await fetch(url, { signal: controller.signal })
      } catch (err) {
        if (err && err.name === 'AbortError') {
          // Unmount or explicit cancel. Do nothing.
          return
        }
        if (!isMountedRef.current) return
        appendError('Network error. Please try again.')
        setIsProcessing(false)
        cancelPoll()
        return
      }

      if (!isMountedRef.current) return

      if (!res.ok) {
        let errorMessage = `Request failed with status ${res.status}`
        try {
          const body = await res.json()
          if (body && body.error) errorMessage = body.error
        } catch {
          /* default */
        }
        appendError(errorMessage)
        setIsProcessing(false)
        cancelPoll()
        return
      }

      let body
      try {
        body = await res.json()
      } catch {
        appendError('Received malformed response from the server.')
        setIsProcessing(false)
        cancelPoll()
        return
      }

      if (!isMountedRef.current) return

      const status = body && body.status

      if (status === 'in_progress') {
        schedulePoll(executionId, pollSessionId)
        return
      }

      if (status === 'confirmation_required') {
        // spec 031 / TM-02: a destructive tool is waiting on the manager. Keep
        // polling -- the pending record expires server-side (90s) and the next
        // poll will report `failed` with "nothing was changed", so the UI cannot
        // strand the user on a prompt that is no longer actionable.
        const pending = body.pending || {}
        setPendingConfirmation({
          pendingId: pending.pending_id,
          toolName: pending.tool_name,
          // Already key-allowlisted and type-validated server-side, so it is
          // safe to render. Showing it is the point: the manager must approve
          // exactly what will run, not a paraphrase of it.
          parameters: pending.parameters || {},
          expiresInS: pending.expires_in_s,
          executionId,
          sessionId: pollSessionId,
        })
        schedulePoll(executionId, pollSessionId)
        return
      }

      if (status === 'succeeded') {
        setPendingConfirmation(null)
        setMessages((prev) => [
          ...prev.filter((m) => !m._thinking),
          {
            role: 'assistant',
            content: body.response,
            timestamp: body.timestamp,
          },
        ])
        const newSession = body.session_id || pollSessionId
        if (newSession) {
          setSessionId(newSession)
          localStorage.setItem(STORAGE_KEY, newSession)
        }
        setIsProcessing(false)
        cancelPoll()
        return
      }

      if (status === 'failed') {
        // Covers the expiry case: the server reports that nothing was changed,
        // and the prompt must come down with it.
        setPendingConfirmation(null)
        appendError(body.error || 'Supervisor execution failed.')
        setIsProcessing(false)
        cancelPoll()
        return
      }

      // Unknown status — treat as error.
      appendError('Unexpected response from the server.')
      setIsProcessing(false)
      cancelPoll()
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  )

  const schedulePoll = useCallback(
    (executionId, pollSessionId) => {
      if (!isMountedRef.current) return

      if (Date.now() >= pollDeadlineRef.current) {
        appendError('Request timed out. Please try again.')
        setIsProcessing(false)
        cancelPoll()
        return
      }

      const jitter = (Math.random() * 2 - 1) * POLL_JITTER_MS
      const delay = POLL_INTERVAL_MS + jitter
      pollTimeoutRef.current = setTimeout(() => {
        pollTimeoutRef.current = null
        doPoll(executionId, pollSessionId)
      }, delay)
    },
    [appendError, cancelPoll, doPoll]
  )

  const sendMessage = useCallback(
    async (text) => {
      if (!text || text.trim() === '') return

      // Cancel any leftover polling from a previous send so we never
      // leak timers across submissions.
      cancelPoll()

      const userMessage = {
        role: 'user',
        content: text,
        timestamp: new Date().toISOString(),
      }

      setMessages((prev) => [...prev, userMessage])
      setIsProcessing(true)

      const body = { message: text }
      if (sessionId) body.session_id = sessionId

      let res
      try {
        res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
      } catch {
        if (!isMountedRef.current) return
        appendError('Network error. Please check your connection and try again.')
        setIsProcessing(false)
        return
      }

      if (!isMountedRef.current) return

      if (!res.ok) {
        let errorMessage = `Request failed with status ${res.status}`
        try {
          const errBody = await res.json()
          if (errBody && errBody.error) errorMessage = errBody.error
        } catch {
          /* default */
        }
        appendError(errorMessage)
        setIsProcessing(false)
        return
      }

      let data
      try {
        data = await res.json()
      } catch {
        appendError('Received malformed response from the server.')
        setIsProcessing(false)
        return
      }

      const execId = data && (data.executionId || data.execution_id)
      const newSession = data && data.session_id
      if (!execId) {
        appendError('Supervisor did not return an execution id.')
        setIsProcessing(false)
        return
      }

      if (newSession) {
        setSessionId(newSession)
        localStorage.setItem(STORAGE_KEY, newSession)
      }

      // Show immediate acknowledgment while waiting for the agent
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: 'Let me look into that for you…',
          timestamp: new Date().toISOString(),
          _thinking: true,
        },
      ])

      pollDeadlineRef.current = Date.now() + POLL_TIMEOUT_MS
      schedulePoll(execId, newSession || sessionId || '')
    },
    [sessionId, appendError, cancelPoll, schedulePoll]
  )

  const loadHistory = useCallback(async () => {
    const storedSessionId = localStorage.getItem(STORAGE_KEY)
    if (!storedSessionId) return

    try {
      const res = await fetch(`/api/chat/history/${storedSessionId}`)
      if (!res.ok) {
        console.warn('Failed to load chat history:', res.status)
        return
      }

      const data = await res.json()
      setMessages(data.messages || [])
      setSessionId(data.session_id || storedSessionId)
    } catch (err) {
      console.warn('Failed to load chat history:', err.message)
    }
  }, [])

  const startNewChat = useCallback(() => {
    cancelPoll()
    setMessages([])
    setSessionId(null)
    localStorage.removeItem(STORAGE_KEY)
  }, [cancelPoll])

  /**
   * Approve or decline a pending destructive tool call (spec 031 / TM-02).
   *
   * The prompt is removed before the request is sent, so a double-click cannot
   * submit twice from the UI. The server enforces single-use independently -- it
   * pops the pending record under a lock -- so this is defence in depth, not the
   * guarantee.
   */
  const confirmAction = useCallback(
    async (decision) => {
      const pending = pendingConfirmation
      if (!pending) return
      setPendingConfirmation(null)

      let res
      try {
        res = await fetch('/api/chat/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pending_id: pending.pendingId, decision }),
        })
      } catch {
        if (!isMountedRef.current) return
        appendError('Network error while confirming. Nothing was changed.')
        setIsProcessing(false)
        cancelPoll()
        return
      }
      if (!isMountedRef.current) return

      if (res.status === 410) {
        appendError('The confirmation window expired, so nothing was changed.')
        setIsProcessing(false)
        cancelPoll()
        return
      }
      if (!res.ok) {
        appendError('That confirmation is no longer valid. Nothing was changed.')
        setIsProcessing(false)
        cancelPoll()
        return
      }
      // Accepted: the turn resumes server-side and the existing poll picks it up.
      schedulePoll(pending.executionId, pending.sessionId)
    },
    [pendingConfirmation, appendError, cancelPoll, schedulePoll]
  )

  return {
    messages,
    pendingConfirmation,
    confirmAction,
    sendMessage,
    isProcessing,
    sessionId,
    loadHistory,
    startNewChat,
    hasPreviousSession,
  }
}
