import type { CheckpointPart, ToolPart, WithParts } from "./message-v2"

/**
 * A bounded account of assistant-side work that occurred after a checkpoint
 * watermark. It deliberately never includes tool outputs: the activity list
 * replaces an old transcript at rebuild time, rather than resembling one.
 */
const MAX_LINE_CHARS = 240
const MAX_ARG_CHARS = 80
const MAX_LINES = 200

function truncate(text: string, max: number): string {
  const cleaned = text.replace(/\s+/g, " ").trim()
  if (cleaned.length <= max) return cleaned
  return cleaned.slice(0, Math.max(0, max - 1)) + "…"
}

function formatToolArgs(input: Record<string, unknown> | undefined): string {
  if (!input) return ""
  return Object.entries(input)
    .map(([key, value]) => {
      if (typeof value === "string") return `${key}=${JSON.stringify(truncate(value, MAX_ARG_CHARS))}`
      let raw: string | undefined
      try {
        raw = JSON.stringify(value)
      } catch {
        raw = undefined
      }
      return `${key}=${truncate(raw ?? "[unserializable]", MAX_ARG_CHARS)}`
    })
    .join(", ")
}

function toolLine(part: ToolPart): string {
  const args = formatToolArgs(part.state.input)
  const call = args ? `${part.tool}(${args})` : `${part.tool}()`
  // Error text is tool output too. Keep the outcome, never its payload.
  if (part.state.status === "error") return `- ${call} → error`
  if (part.state.status === "pending" || part.state.status === "running") return `- ${call} → interrupted`
  return `- ${call}`
}

export function isBoundaryUser(msg: WithParts): boolean {
  return msg.info.role === "user" && msg.parts.some((p) => p.type === "checkpoint" || p.type === "compaction")
}

export function isRebuildBoundary(msg: WithParts): boolean {
  return msg.info.role === "user" && msg.parts.some((p) => p.type === "checkpoint")
}

function digestLines(tail: readonly WithParts[]): { lines: string[]; interrupted: boolean } {
  const lines: string[] = []
  for (const msg of tail) {
    // Never re-digest a prior rebuild/compaction boundary — that folds the
    // previous dump into itself on a second rebuild.
    if (isBoundaryUser(msg)) continue
    // Tail user messages are quoted in the boundary's Recent user input
    // section (and stay live after a legacy boundary); do not also log them.
    if (msg.info.role === "user") continue
    for (const part of msg.parts) {
      if (part.type === "text" && !part.ignored && !part.synthetic) {
        const text = part.text.trim()
        if (text) lines.push(`- ${msg.info.role}: ${truncate(text, MAX_LINE_CHARS)}`)
        continue
      }
      if (part.type === "subtask") {
        lines.push(`- subtask: ${truncate(part.command ?? part.agent, MAX_LINE_CHARS)}`)
        continue
      }
      if (part.type === "tool") lines.push(toolLine(part))
    }
  }
  // This is a *recent* activity digest. When the tail exceeds the budget,
  // retain the newest work so the model resumes from the actual current state.
  const kept = lines.slice(-MAX_LINES)
  return { lines: kept, interrupted: kept.some((line) => line.includes("→ interrupted")) }
}

/** Section body only; empty when the tail has no assistant-side activity. */
export function renderTailDigest(tail: readonly WithParts[]): string {
  const { lines, interrupted } = digestLines(tail)
  if (lines.length === 0) return ""
  return [
    "# Recent activity",
    "",
    ...(interrupted ? ["(tool loop interrupted by rebuild — re-run any tools you still need)", ""] : []),
    ...lines,
  ].join("\n")
}

function checkpointPart(msg: WithParts): CheckpointPart | undefined {
  if (msg.info.role !== "user") return undefined
  return msg.parts.find((p): p is CheckpointPart => p.type === "checkpoint")
}

/**
 * Legacy rebuild boundaries were stamped at watermark + 1 ms, so they sort
 * before the tail they digest. A boundary appended after its tail is created
 * no earlier than the newest tail message.
 */
function isLegacyBoundary(boundary: WithParts, newest: WithParts): boolean {
  return boundary.info.time.created < newest.info.time.created
}

/**
 * Drop assistant messages in (coveredUpTo, digestUpTo] after a legacy
 * rebuild boundary. A boundary appended after the newest message leaves its
 * tail outside the window, so there is nothing to drop. Older boundaries were
 * stamped at watermark + 1 ms and sort before the tail they digest; such a
 * boundary is recognised from stored data alone: its `time.created` is
 * earlier than that of the `digestUpTo` message. For those, the activity list
 * already sits in the boundary's rebuild context, so this removes the
 * now-redundant verbatim tool/assistant tail while post-insert messages stay
 * live.
 *
 * Filtering by message-id range — not by the boundary's position in the
 * array — so a same-millisecond tail message cannot escape collapse by
 * sorting before the boundary's synthetic time.
 *
 * User-role messages are never dropped: the last user is where insertReminders
 * persists skill-catalog gates, and a file-only or
 * synthetic-only user turn is still an instruction the provider must see.
 * Collapsing it would mark the reminder "already sent" while the provider
 * never receives it.
 */
export function collapseCheckpointTail(msgs: readonly WithParts[]): WithParts[] {
  const boundary = msgs.findLast((m) => m.info.role === "user" && m.parts.some((p) => p.type === "checkpoint"))
  if (!boundary) return msgs as WithParts[]

  const part = checkpointPart(boundary)
  if (!part?.digestUpTo) return msgs as WithParts[]

  const digestUpTo = part.digestUpTo
  const newest = msgs.find((m) => m.info.id === digestUpTo)
  if (!newest || !isLegacyBoundary(boundary, newest)) return msgs as WithParts[]

  const live = msgs.filter(
    (m) => m.info.id <= part.coveredUpTo || m.info.id > part.digestUpTo! || m.info.role === "user",
  )
  return live.length === msgs.length ? (msgs as WithParts[]) : (live as WithParts[])
}
