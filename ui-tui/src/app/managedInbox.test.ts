import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  appendManagedAck,
  latestManagedInboxItem,
  managedLifecyclePrompt,
  pendingManagedInboxItems
} from './managedInbox.js'

const tempDir = () => mkdtempSync(join(tmpdir(), 'hermes-managed-inbox-'))

const appendJsonl = (path: string, rows: Array<Record<string, unknown>>) => {
  writeFileSync(path, rows.map(row => JSON.stringify(row)).join('\n') + '\n')
}

describe('managed inbox helpers', () => {
  it('finds latest matching lifecycle item', () => {
    const dir = tempDir()
    appendJsonl(join(dir, 'inbox.jsonl'), [
      { id: 'old', kind: 'startup', text: 'old startup' },
      { id: 'msg', kind: 'message_wake', text: 'check messages' },
      { id: 'new', kind: 'startup', text: 'new startup' }
    ])

    expect(latestManagedInboxItem(dir, 'startup')).toMatchObject({ id: 'new', text: 'new startup' })
  })

  it('returns only unsubmitted non-lifecycle items as pending', () => {
    const dir = tempDir()
    appendJsonl(join(dir, 'inbox.jsonl'), [
      { id: 'startup-1', kind: 'startup', text: 'GET /briefing' },
      { id: 'msg-1', kind: 'message_wake', text: 'call check_messages' },
      { id: 'msg-2', kind: 'telegram_wake', text: 'call check_messages for telegram' }
    ])
    appendJsonl(join(dir, 'acks.jsonl'), [{ inbox_id: 'msg-1', trigger: 'message_wake', status: 'submitted' }])

    expect(pendingManagedInboxItems(dir).map(item => item.id)).toEqual(['msg-2'])
  })

  it('appends ACK rows with inbox id and status', () => {
    const dir = tempDir()

    appendManagedAck(dir, { trigger: 'message_wake', inbox_id: 'msg-1', status: 'submitted' })

    const rows = readFileSync(join(dir, 'acks.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line))
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ trigger: 'message_wake', inbox_id: 'msg-1', status: 'submitted' })
    expect(rows[0].created_at).toBeTruthy()
  })

  it('builds lifecycle prompt with exact gateway calls', () => {
    const prompt = managedLifecyclePrompt('restart', { id: 'r1', kind: 'restart', text: 'GET http://127.0.0.1:10000/api/agents/porto/briefing?tier=1' }, 'porto')

    expect(prompt).toContain('AI-Swarm managed lifecycle trigger: restart')
    expect(prompt).toContain('mcp_gateway_get_briefing({"tier":1})')
    expect(prompt).toContain('mcp_gateway_check_messages()')
    expect(prompt).toContain('mcp_gateway_list_tasks({"assigned_to":"porto","status":"all"})')
  })
})
