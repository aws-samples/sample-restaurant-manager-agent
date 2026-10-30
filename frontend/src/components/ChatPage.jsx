// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState, useEffect, useRef } from 'react'
import { useChat } from '../hooks/useChat'

function MessageBubble({ message }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] px-4 py-2 rounded-lg bg-indigo-600 text-white">
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
          <span className="block text-xs text-indigo-200 mt-1 text-right">
            {formatTime(message.timestamp)}
          </span>
        </div>
      </div>
    )
  }

  if (message.role === 'error') {
    return (
      <div className="flex justify-start">
        <div className="max-w-[75%] px-4 py-2 rounded-lg bg-red-900/30 border border-red-800">
          <p className="whitespace-pre-wrap break-words text-red-300">{message.content}</p>
          <span className="block text-xs text-red-400 mt-1">
            {formatTime(message.timestamp)}
          </span>
        </div>
      </div>
    )
  }

  // assistant
  return (
    <div className="flex justify-start">
      <div className={`max-w-[75%] px-4 py-2 rounded-lg bg-gray-700 text-gray-100${message._thinking ? ' animate-pulse italic text-gray-400' : ''}`}>
        <p className="whitespace-pre-wrap break-words">{message.content}</p>
        {!message._thinking && (
          <span className="block text-xs text-gray-400 mt-1">
            {formatTime(message.timestamp)}
          </span>
        )}
      </div>
    </div>
  )
}

function ProcessingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="px-4 py-2 rounded-lg bg-gray-700 text-gray-100">
        <span className="inline-flex gap-1">
          <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
          <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
          <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
        </span>
      </div>
    </div>
  )
}

/**
 * Confirm/decline prompt for a destructive tool call (spec 031 / threat model
 * TM-02).
 *
 * Renders the tool name and the exact VALIDATED parameters the server will
 * execute. Showing them is the whole point: the manager approves what will
 * actually run, not a paraphrase the model produced. Deliberately styled as a
 * warning rather than a neutral dialog -- `void_order` destroys a sale record.
 */
function ConfirmationPrompt({ pending, onConfirm }) {
  const entries = Object.entries(pending.parameters || {})
  return (
    <div className="flex justify-start" data-testid="confirmation-prompt">
      <div className="max-w-[75%] px-4 py-3 rounded-lg bg-amber-900/30 border border-amber-700">
        {/* semgrep: English-only demo UI; strings are intentionally not internationalized. */}
        {/* nosemgrep: jsx-not-internationalized */}
        <p className="text-amber-200 font-semibold">
          Confirm this action
        </p>
        {/* nosemgrep: jsx-not-internationalized */}
        <p className="text-sm text-amber-100/90 mt-1">
          The assistant wants to run{' '}
          <code className="px-1 rounded bg-amber-950/60">{pending.toolName}</code>. This
          changes store data and is not easily undone.
        </p>
        {entries.length > 0 && (
          <dl className="mt-2 text-sm text-amber-100/90">
            {entries.map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="text-amber-300">{key}:</dt>
                <dd className="break-words">{String(value)}</dd>
              </div>
            ))}
          </dl>
        )}
        <div className="flex gap-2 mt-3">
          {/* nosemgrep: jsx-not-internationalized */}
          <button
            type="button"
            onClick={() => onConfirm('approve')}
            className="px-3 py-1.5 rounded bg-amber-600 hover:bg-amber-500 text-white text-sm font-medium"
          >
            Confirm
          </button>
          {/* nosemgrep: jsx-not-internationalized */}
          <button
            type="button"
            onClick={() => onConfirm('deny')}
            className="px-3 py-1.5 rounded bg-gray-600 hover:bg-gray-500 text-white text-sm font-medium"
          >
            Decline
          </button>
        </div>
        {/* nosemgrep: jsx-not-internationalized */}
        {typeof pending.expiresInS === 'number' && (
          <p className="text-xs text-amber-400/80 mt-2">
            Expires in about {pending.expiresInS}s. If it expires, nothing is changed.
          </p>
        )}
      </div>
    </div>
  )
}

function formatTime(timestamp) {
  if (!timestamp) return ''
  try {
    return new Date(timestamp).toLocaleTimeString()
  } catch {
    return ''
  }
}

export function ChatPage() {
  const {
    messages,
    sendMessage,
    isProcessing,
    pendingConfirmation,
    confirmAction,
    loadHistory,
    startNewChat,
    hasPreviousSession
  } = useChat()

  const [input, setInput] = useState('')
  const [showResumeBanner, setShowResumeBanner] = useState(false)
  const messagesEndRef = useRef(null)

  // On mount, check for previous session
  useEffect(() => {
    if (hasPreviousSession && messages.length === 0) {
      setShowResumeBanner(true)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll to latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isProcessing])

  const handleSend = () => {
    const trimmed = input.trim()
    if (!trimmed || isProcessing) return
    sendMessage(trimmed)
    setInput('')
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleResume = () => {
    setShowResumeBanner(false)
    loadHistory()
  }

  const handleStartNew = () => {
    setShowResumeBanner(false)
    startNewChat()
  }

  return (
    <div className="flex flex-col bg-gray-800 rounded-lg" style={{ height: 'calc(100vh - 120px)' }}>
      {/* Session resume banner */}
      {showResumeBanner && (
        <div className="flex items-center justify-between px-4 py-3 bg-indigo-900/50 border-b border-gray-700 rounded-t-lg">
          {/* nosemgrep: jsx-not-internationalized */}
          <span className="text-sm text-gray-200">You have a previous session. Resume or Start New?</span>
          <div className="flex gap-2">
            {/* nosemgrep: jsx-not-internationalized */}
            <button
              onClick={handleResume}
              className="px-3 py-1 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white"
            >
              Resume
            </button>
            {/* nosemgrep: jsx-not-internationalized */}
            <button
              onClick={handleStartNew}
              className="px-3 py-1 text-sm rounded bg-gray-600 hover:bg-gray-500 text-white"
            >
              Start New
            </button>
          </div>
        </div>
      )}

      {/* Conversation thread */}
      <div
        className="flex-1 overflow-y-auto p-4 space-y-3"
        role="log"
        aria-live="polite"
      >
        {/* nosemgrep: jsx-not-internationalized */}
        {messages.length === 0 && !showResumeBanner && (
          <div className="flex items-center justify-center h-full text-gray-500 text-sm">
            Send a message to start chatting with the supervisor agent.
          </div>
        )}

        {messages.map((msg, idx) => (
          <MessageBubble key={idx} message={msg} />
        ))}

        {pendingConfirmation && (
          <ConfirmationPrompt
            pending={pendingConfirmation}
            onConfirm={confirmAction}
          />
        )}
        {isProcessing && !pendingConfirmation && <ProcessingIndicator />}

        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-gray-700 p-3">
        <div className="flex gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isProcessing}
            placeholder="Type a message..."
            aria-label="Chat message"
            rows={1}
            className="flex-1 px-3 py-2 rounded-lg bg-gray-700 text-white border border-gray-600 placeholder-gray-400 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
          />
          {/* nosemgrep: jsx-not-internationalized */}
          <button
            onClick={handleSend}
            disabled={isProcessing || !input.trim()}
            aria-label="Send message"
            className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
