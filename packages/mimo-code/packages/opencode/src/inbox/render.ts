import type { InboxRow } from "./inbox.sql"
import z from "zod"
import { ActorHostContext, ActorModel } from "@/actor/schema"

/** The only shape accepted for a trusted lifecycle notification. */
export const ActorNotificationEvent = z
  .object({
    actorID: z.string().min(1),
    description: z.string().min(1),
    status: z.enum(["completed", "failed", "cancelled", "stalled"]),
    result: z.string().optional(),
    error: z.string().optional(),
    reportedStatus: z.enum(["success", "partial", "failed", "blocked", "unknown"]).optional(),
    reportedSummary: z.string().optional(),
    requestedModel: z.string().min(1).optional(),
    effectiveModel: ActorModel.optional(),
    hostContext: ActorHostContext.optional(),
    stalledForMs: z.number().int().nonnegative().optional(),
  })
  .strict()
export type ActorNotificationEvent = z.infer<typeof ActorNotificationEvent>

const escapeText = (value: string) =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
const escapeHeader = (value: string) =>
  escapeText(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;")
const contentText = (content: unknown) => {
  if (typeof content === "object" && content !== null && "text" in content && typeof content.text === "string") {
    return content.text
  }
  if (content === undefined || content === null) return "(empty)"
  if (typeof content === "string") return content
  try {
    return JSON.stringify(content)
  } catch {
    return String(content)
  }
}

function renderUntrustedInboxRow(row: InboxRow): string {
  const sender = row.sender_session_id
    ? `${row.sender_session_id}:${row.sender_actor_id ?? "?"}`
    : "system"
  const sentAt = new Date(row.created_at).toISOString()
  return `<inbox trust="untrusted" type="${escapeHeader(row.type)}" from="${escapeHeader(sender)}" sent_at="${escapeHeader(sentAt)}">\n${escapeText(contentText(row.content))}\n</inbox>`
}

export function renderInboxRow(row: InboxRow): string {
  if (row.type === "actor_notification") {
    // Lifecycle rows carry the structured event, never model-provided text.
    // Invalid or legacy rows fall back to the escaped untrusted wrapper so a
    // forged actor-notification cannot become lifecycle state.
    const event =
      typeof row.content === "object" && row.content !== null && "notification" in row.content
        ? ActorNotificationEvent.safeParse(row.content.notification)
        : undefined
    if (event?.success) return renderActorNotification(event.data)
    return renderUntrustedInboxRow(row)
  }
  // Model-facing messages are explicitly untrusted. Escape both attributes
  // and text so payloads cannot close the wrapper or mint lifecycle tags.
  return renderUntrustedInboxRow(row)
}

export function renderActorNotification(event: ActorNotificationEvent): string {
  const header = `Background sub-session "${escapeHeader(event.description)}" (actor_id: ${escapeHeader(event.actorID)})`
  const identity = [
    event.requestedModel ? `\nRequested model: ${escapeText(event.requestedModel)}` : "",
    event.effectiveModel ? `\nEffective model: ${escapeText(JSON.stringify(event.effectiveModel))}` : "",
    event.hostContext ? `\nHost context: ${escapeText(JSON.stringify(event.hostContext))}` : "",
  ].join("")
  if (event.status === "completed") {
    // event.status is the sub-session *process lifecycle* — it ended cleanly.
    // event.reportedStatus is the *task* outcome the sub-session self-reported
    // via a `**Status**: ...` header. These are independent: a process can exit
    // cleanly while the task failed/blocked. Word the top line by the task
    // outcome so we never imply a success the sub-session didn't claim.
    const reported = event.reportedStatus?.toLowerCase()
    const summaryLine = event.reportedSummary ? `\nSummary: ${escapeText(event.reportedSummary)}` : ""
    const resultLine = `\nResult: ${escapeText(event.result ?? "(no output)")}`
    // success/partial (or absent → treat as a plain completion) keep the
    // affirmative "completed" verb.
    if (!reported || reported === "success" || reported === "partial") {
      const statusLine = reported ? `\nStatus: ${reported}` : ""
      return `<actor-notification>\n${header}${identity} completed.${statusLine}${summaryLine}${resultLine}\n</actor-notification>`
    }
    // failed/blocked → the sub-session ran to the end but the task did not
    // succeed. State the outcome; never say "completed".
    if (reported === "failed" || reported === "blocked") {
      return `<actor-notification>\n${header}${identity} finished (status: ${reported}).${summaryLine}${resultLine}\n</actor-notification>`
    }
    // Any other reported value = unknown/unrecognized → neutral verb, and omit
    // the misleading "Status: unknown" line entirely.
    return `<actor-notification>\n${header}${identity} ended (status not reported).${summaryLine}${resultLine}\n</actor-notification>`
  }
  if (event.status === "failed") {
    return `<actor-notification>\n${header}${identity} failed.\nError: ${escapeText(event.error ?? "unknown")}\n</actor-notification>`
  }
  if (event.status === "stalled") {
    const forLine =
      event.stalledForMs !== undefined ? ` (no turn advance for ${Math.floor(event.stalledForMs / 1000)}s)` : ""
    return `<actor-notification>\n${header}${identity} appears stalled${forLine}. It is still running but has made no progress. Consider checking on it, sending it a nudge, or cancelling it.\n</actor-notification>`
  }
  return `<actor-notification>\n${header}${identity} was cancelled.\n</actor-notification>`
}

export type ParsedActorNotification = {
  // "stalled" is reserved for a future watchdog-emitted notification;
  // renderActorNotification never produces it today (only completed/failed/
  // cancelled lifecycle). The parse + card styling exist ahead of that producer.
  // "ended" is the completed-lifecycle case where the sub-session's task
  // outcome was not reported — neutral, neither success nor failure.
  status: "completed" | "failed" | "cancelled" | "stalled" | "ended"
  description: string
  summary?: string
}

// Inverse of renderActorNotification: recover the structured fields from the
// pre-rendered <actor-notification> text so the TUI can show a card instead of
// the raw wrapper. Pure + exported so it's unit-testable without the renderer.
// Returns null for any text that isn't an actor notification.
export function parseActorNotification(text: string): ParsedActorNotification | null {
  if (!text.trimStart().startsWith("<actor-notification>")) return null
  // The verb reflects the *task* outcome, not just the process lifecycle:
  //   completed                         → task succeeded / plain completion
  //   finished (status: failed|blocked) → process ended cleanly, task not ok
  //   ended (status not reported)       → process ended cleanly, outcome unknown
  //   failed                            → the process itself failed
  //   was cancelled / stalled           → cancelled / watchdog
  const header = text.match(
    /Background (?:sub-session|actor) "(.*?)" \(actor_id: [^)]*\)\s+(completed|finished|ended|failed|was cancelled|stalled)\b/,
  )
  if (!header) return null
  const description = header[1]
  const verb = header[2]
  const status: ParsedActorNotification["status"] =
    verb === "completed"
      ? "completed"
      : verb === "finished" || verb === "failed"
        ? "failed"
        : verb === "ended"
          ? "ended"
          : verb === "stalled"
            ? "stalled"
            : "cancelled"
  // Prefer the most human-relevant one-liner: Summary > Result > Error.
  // renderActorNotification always emits the Summary line before the Result
  // line, so restrict the Summary match to the region before the first
  // "Result:" line — otherwise a `Summary:`-prefixed line inside the Result
  // body would be mistaken for the notification's own summary.
  const resultIdx = text.search(/^Result:/m)
  const beforeResult = resultIdx === -1 ? text : text.slice(0, resultIdx)
  const line = (label: string, scope: string) => scope.match(new RegExp(`^${label}:\\s*(.+)$`, "m"))?.[1]?.trim()
  const summary = line("Summary", beforeResult) ?? line("Result", text) ?? line("Error", text)
  return summary ? { status, description, summary } : { status, description }
}
