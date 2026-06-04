import { describe, expect, it } from 'vitest'

import { managedSubmitAckStatus } from '../app/useSubmission.js'

describe('managedSubmitAckStatus', () => {
  it('marks accepted managed submissions as started', () => {
    expect(managedSubmitAckStatus({ accepted: true })).toBe('started')
  })

  it('marks session-busy retries as queued instead of terminal', () => {
    expect(managedSubmitAckStatus({ busyRetry: true })).toBe('queued')
  })

  it('marks missing-session submissions as blocked', () => {
    expect(managedSubmitAckStatus({ missingSession: true })).toBe('blocked')
  })

  it('marks hard failures as error', () => {
    expect(managedSubmitAckStatus({ failed: true })).toBe('error')
  })
})
