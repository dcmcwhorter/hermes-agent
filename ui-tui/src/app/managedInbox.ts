import { appendFileSync, chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export type ManagedInboxItem = {
  created_at?: string
  id?: string
  kind?: string
  text?: string
}

export const MANAGED_LIFECYCLE_TRIGGERS = new Set(['startup', 'restart'])

export const managedSessionDir = () => process.env.HERMES_AI_SWARM_SESSION_DIR || ''
export const managedAgentName = () => process.env.HERMES_AI_SWARM_AGENT || process.env.GATEWAY_AGENT || 'agent'
export const managedProtocolEnabled = () => Boolean(process.env.HERMES_AI_SWARM_PROTOCOL && managedSessionDir())

const jsonlRows = (path: string): Array<Record<string, unknown>> => {
  if (!existsSync(path)) {
    return []
  }

  return readFileSync(path, 'utf8')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
    .flatMap(line => {
      try {
        return [JSON.parse(line) as Record<string, unknown>]
      } catch {
        return []
      }
    })
}

export const appendManagedAck = (dir: string, payload: Record<string, unknown>) => {
  if (!dir) {
    return
  }

  try {
    mkdirSync(dir, { mode: 0o700, recursive: true })
    const path = join(dir, 'acks.jsonl')
    appendFileSync(path, `${JSON.stringify({ ...payload, created_at: new Date().toISOString() })}\n`, { mode: 0o600 })
    chmodSync(path, 0o600)
  } catch {
    // Best effort only. The UI must not crash because the ACK path failed.
  }
}

export const writeManagedStatus = (dir: string, payload: Record<string, unknown>) => {
  if (!dir) {
    return
  }

  try {
    mkdirSync(dir, { mode: 0o700, recursive: true })
    const path = join(dir, 'status.json')
    writeFileSync(path, `${JSON.stringify({ ...payload, updated_at: new Date().toISOString() })}\n`, { mode: 0o600 })
    chmodSync(path, 0o600)
  } catch {
    // Best effort only. The UI must not crash because the status path failed.
  }
}

export const latestManagedInboxItem = (dir: string, trigger: string): ManagedInboxItem | null => {
  const rows = jsonlRows(join(dir, 'inbox.jsonl'))

  for (const item of rows.reverse()) {
    if (item.kind === trigger) {
      return item as ManagedInboxItem
    }
  }

  return null
}

const submittedInboxIds = (dir: string) =>
  new Set(
    jsonlRows(join(dir, 'acks.jsonl'))
      .filter(row => row.status === 'submitted' && typeof row.inbox_id === 'string')
      .map(row => row.inbox_id as string)
  )

const pendingManagedItems = (
  dir: string,
  alreadySeen = new Set<string>(),
  predicate: (kind: string) => boolean
) => {
  const submitted = submittedInboxIds(dir)

  return jsonlRows(join(dir, 'inbox.jsonl'))
    .map(row => row as ManagedInboxItem)
    .filter(item => item.id && item.kind && item.text)
    .filter(item => predicate(item.kind || ''))
    .filter(item => !submitted.has(item.id || ''))
    .filter(item => !alreadySeen.has(item.id || ''))
}

export const pendingManagedLifecycleItems = (dir: string, alreadySeen = new Set<string>()) =>
  pendingManagedItems(dir, alreadySeen, kind => MANAGED_LIFECYCLE_TRIGGERS.has(kind))

export const pendingManagedInboxItems = (dir: string, alreadySeen = new Set<string>()) =>
  pendingManagedItems(dir, alreadySeen, kind => !MANAGED_LIFECYCLE_TRIGGERS.has(kind))

export const managedEventPrompt = (item: ManagedInboxItem, agent = managedAgentName()) => `AI-Swarm managed gateway event: ${item.kind || 'event'}
Agent: ${agent}
Managed event id: ${item.id || 'unknown'}

${item.text || ''}`

export const managedLifecyclePrompt = (trigger: string, item: ManagedInboxItem, agent = managedAgentName()) => {
  const mode = trigger === 'restart' ? 'autonomous restart recovery' : 'autonomous clean startup'
  const taskInstruction =
    trigger === 'restart'
      ? 'Resume actionable in-progress or queued work immediately unless the human explicitly marked it as requiring interaction (for example: do not attempt unsupervised, wait for me, manual approval required, or human-supervised only). If no actionable task exists, report that and stop.'
      : 'Run this agent\'s bounded routine startup duties after loading context. Continue autonomously only for tasks that are queued/in-progress or explicitly part of the startup routine.'

  return `AI-Swarm managed lifecycle trigger: ${trigger}
Mode: ${mode}
Agent: ${agent}
Managed event id: ${item.id || 'unknown'}
Managed event text: ${item.text || ''}

This is a gateway-triggered autonomous mode, not a human interactive resume.
Do not interpret the word "${trigger}" as a normal user request.
Do not ask Dan for confirmation unless a task is explicitly human-supervised or unsafe.
The gateway must never send "resume"; human-sent "resume" means summarize and wait.

Required first actions:
1. Call mcp_gateway_get_briefing({"tier":1}).
2. Call mcp_gateway_check_messages().
3. Call mcp_gateway_list_tasks({"assigned_to":"${agent}","status":"all"}). Do not skip this call because the briefing says there are no active tasks.
4. If and only if the task MCP tool is unavailable, use REST fallback exactly: GET http://127.0.0.1:10000/api/tasks?assigned_to=${agent}&status=all&include_deferred=true.
5. Check reminders via the gateway reminder surface if available.
6. Verify startup readiness before starting task work:
   - Gateway/db: gateway status/briefing/messages/tasks calls succeeded.
   - Hindsight: this agent's configured daemon is reachable, the configured bank exists, and recall against a recent/task-relevant query returns useful data; if no useful data exists, mark YELLOW with the reason, not GREEN.
   - LCM: LCM status/doctor/describe (or equivalent) shows healthy storage plus useful current-session messages or summary nodes; if no nodes/data exist, mark YELLOW with counts, not GREEN.
   - AutoCtx: command exists and autoctx hermes inspect --home ~/.hermes --json succeeds.
7. Print a compact traffic-light startup/restart report before work:
   GREEN/YELLOW/RED Gateway+DB: ...
   GREEN/YELLOW/RED Hindsight: ...
   GREEN/YELLOW/RED LCM: ...
   GREEN/YELLOW/RED AutoCtx: ...
   Unread Messages: N
   New Tasks: N, In Progress Tasks: N
   Active Reminders: N
8. If any critical readiness item is RED, stop and report blocked instead of reconstructing from scratch silently. If only YELLOW items exist, state the degraded recovery path before proceeding.
9. For every pending or in-progress task assigned to ${agent}, decide whether it is actionable. If explicitly safe/unsupervised, mark it in_progress then completed with mcp_gateway_update_task or PUT /api/tasks/<id>.
10. Use the returned briefing/tasks/checkpoints to select the concrete next action.
11. ${taskInstruction}

Finish with a short status that states whether startup/restart recovery is ready, resumed, blocked, or has no actionable work.`
}
