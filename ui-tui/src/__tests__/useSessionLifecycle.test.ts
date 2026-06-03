import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  managedResetLifecycleItem,
  managedResetLifecyclePrompt,
  writeActiveSessionFile
} from '../app/useSessionLifecycle.js'

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })
})

describe('managed reset lifecycle helpers', () => {
  it('turns manual /new into a synthetic startup item', () => {
    expect(managedResetLifecycleItem('manual_new', 'sess-123')).toEqual({
      id: 'managed-reset-manual_new-sess-123',
      kind: 'startup',
      text: expect.stringContaining('manual /new requested inside managed TUI')
    })
  })

  it('builds a hidden startup prompt for manual /clear recovery', () => {
    const prompt = managedResetLifecyclePrompt('manual_clear', 'sess-456', 'mac')

    expect(prompt).toContain('AI-Swarm managed lifecycle trigger: startup')
    expect(prompt).toContain('Agent: mac')
    expect(prompt).toContain('Managed event id: managed-reset-manual_clear-sess-456')
    expect(prompt).toContain('manual /clear requested inside managed TUI')
    expect(prompt).toContain('mcp_gateway_get_briefing({"tier":1})')
  })
})
