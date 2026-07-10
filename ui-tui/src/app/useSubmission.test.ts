import { describe, expect, it } from 'vitest'

import { managedSubmitAckStatus } from './useSubmission.js'

describe('managed submission ACK semantics', () => {
  it('maps accepted submissions to started', () => {
    expect(managedSubmitAckStatus('accepted')).toBe('started')
  })

  it('maps session-busy retries to queued', () => {
    expect(managedSubmitAckStatus('session_busy')).toBe('queued')
  })

  it('maps missing-session submissions to blocked', () => {
    expect(managedSubmitAckStatus('missing_session')).toBe('blocked')
  })

  it('maps hard failures to error', () => {
    expect(managedSubmitAckStatus('error')).toBe('error')
  })
})
