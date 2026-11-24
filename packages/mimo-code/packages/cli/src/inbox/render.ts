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
    warnings: z.array(z.string()).optional(),
  })
  .strict()
export type ActorNotificationEvent = z.infer<typeof ActorNotificationEvent>

const escapeText = (value: string) =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
const escapeHeader = (value: string) =>
  escapeText(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;")
const contentText = (content: unknown) => {
  if (typeof content === "object" && content !== null && "text" in content && typeof content.text === "string") {
    return blankTo(content.text, "(empty)")
  }
  if (content === undefined || content === null) return "(empty)"
  // A row whose content object carries nothing is as empty as a missing body.
  if (typeof content === "object" && Object.keys(content).length === 0) return "(empty)"
  if (typeof content === "string") return blankTo(content, "(empty)")
  try {
    return JSON.stringify(content)
  } catch {
    return String(content)
  }
}

// Structural subset of InboxRow — render only reads these fields, so callers
// (including tests) need not construct a full queue row.
type RenderableRow = {
  type: string
  content: unknown
  sender_session_id?: string | null
  sender_actor_id?: string | null
  created_at: number
  [other: string]: unknown
}

function renderUntrustedInboxRow(row: RenderableRow): string {
  const sender = row.sender_session_id ? `${row.sender_session_id}:${row.sender_actor_id ?? "?"}` : "system"
  const sentAt = new Date(row.created_at).toISOString()
  return `<inbox trust="untrusted" type="${escapeHeader(row.type)}" from="${escapeHeader(sender)}" sent_at="${escapeHeader(sentAt)}">\n${escapeText(contentText(row.content))}\n</inbox>`
}

export function renderInboxRow(row: RenderableRow): string {
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

// A blank body must fall back to the placeholder, not just a missing one.
// `?? placeholder` only catches null/undefined, but a blank body is stored as
// "" (or whitespace) — and for actor_notification the body is passed through
// RAW, so "" would become a user text part with text:"". The AI SDK's user
// branch filters empty text parts out with no backfill, leaving `content: []`
// and a provider 400 ("user messages must have non-empty content").
function blankTo(text: string | undefined, placeholder: string) {
  return text !== undefined && text.trim().length > 0 ? text : placeholder
}

// Display-only decoding; keep the persisted synthetic part unchanged for model context.
export function parseAgentInboxPart(part: { type: string; synthetic?: boolean; ignored?: boolean; text?: string }) {
  if (part.type !== "text" || !part.synthetic || part.ignored || typeof part.text !== "string") return undefined
  const match = /^<inbox( trust="untrusted" type="[^"<>]*")? from="([^"<>\s:]+:[^"<>\s:]+)" sent_at="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)">\n([\s\S]*)\n<\/inbox>$/.exec(part.text)
  if (!match || !Number.isFinite(Date.parse(match[3]!))) return undefined
  // Our wrapper escapes its body; undo that for display only.
  const text = match[1]
    ? match[4]!.replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&")
    : match[4]!
  return { from: match[2]!, sentAt: match[3]!, text }
}

// stalledForMs: how long (ms) the child has been SILENT — nothing has landed for
// its slice. NOT time since the last completed step: liveness classifies on
// last_activity_time (actor/schema.ts deriveLiveness), and a child inside one
// long step keeps writing parts, so it never lands here.
export function renderActorNotification(event: ActorNotificationEvent): string {
  const header = `Background sub-session "${escapeHeader(event.description)}" (actor_id: ${escapeHeader(event.actorID)})`
  // Follows the verb sentence so parseActorNotification still reads the header.
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
    const warningLines = event.warnings?.map((warning) => `\nWarning: ${escapeText(warning)}`).join("") ?? ""
    const resultLine = `${warningLines}\nResult: ${escapeText(event.result ?? "(no output)")}`
    // success/partial (or absent → treat as a plain completion) keep the
    // affirmative "completed" verb.
    if (!reported || reported === "success" || reported === "partial") {
      const statusLine = reported ? `\nStatus: ${reported}` : ""
      return `<actor-notification>\n${header} completed.${identity}${statusLine}${summaryLine}${resultLine}\n</actor-notification>`
    }
    // failed/blocked → the sub-session ran to the end but the task did not
    // succeed. State the outcome; never say "completed".
    if (reported === "failed" || reported === "blocked") {
      return `<actor-notification>\n${header} finished (status: ${reported}).${identity}${summaryLine}${resultLine}\n</actor-notification>`
    }
    // Any other reported value = unknown/unrecognized → neutral verb, and omit
    // the misleading "Status: unknown" line entirely.
    return `<actor-notification>\n${header} ended (status not reported).${identity}${summaryLine}${resultLine}\n</actor-notification>`
  }
  if (event.status === "failed") {
    const partial = event.result === undefined ? "" : `\nPartial result: ${escapeText(event.result)}`
    return `<actor-notification>\n${header} failed.${identity}\nError: ${escapeText(event.error ?? "unknown")}${partial}\n</actor-notification>`
  }
  if (event.status === "stalled") {
    // "no activity", not "no turn advance". The quantity is silence since the last
    // part write; saying "turn" described a signal the derivation stopped reading
    // and made healthy children inside long steps look wedged. Likewise "nothing
    // has landed" rather than "has made no progress" — a long step IS progress,
    // and we cannot see inside one, only whether anything is coming out.
    const forLine =
      event.stalledForMs !== undefined ? ` (no activity for ${Math.floor(event.stalledForMs / 1000)}s)` : ""
    return `<actor-notification>\n${header} appears stalled${forLine}. It is still running, but nothing has landed for it in that time. Consider checking on it, sending it a nudge, or cancelling it.${identity}\n</actor-notification>`
  }
  return `<actor-notification>\n${header} was cancelled.${identity}\n</actor-notification>`
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
  warnings?: string[]
}

// Inverse of renderActorNotification: recover the structured fields from the
// pre-rendered <actor-notification> text so the TUI can show a card instead of
// the raw wrapper. Pure + exported so it's unit-testable without the renderer.
// Returns null for any text that isn't an actor notification.
//
// TUI-facing fields (summary/warnings) are compacted: stack frames and long
// dumps are noise on a terminal card. The raw XML stays on the synthetic
// message part for the main agent / transcript.
const CARD_LINE_LIMIT = 160

function compactForCard(raw: string): string {
  const first = raw
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.length > 0 && !/^\s*at\s+\S/.test(line) && !/^\s*\.{3}\s/.test(line))
  const text = (first ?? raw.trim()).replace(/\s+/g, " ")
  return text.length > CARD_LINE_LIMIT ? `${text.slice(0, CARD_LINE_LIMIT - 1)}…` : text
}

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
  const resultIdx = text.search(/^(?:Result|Partial result):/m)
  const beforeResult = resultIdx === -1 ? text : text.slice(0, resultIdx)
  const line = (label: string, scope: string) => scope.match(new RegExp(`^${label}:\\s*(.+)$`, "m"))?.[1]?.trim()
  const resultSummary =
    resultIdx !== -1 && text.slice(resultIdx).startsWith("Result:") ? line("Result", text.slice(resultIdx)) : undefined
  const warningIdx = beforeResult.search(/^Warning:/m)
  const metadataHeader = warningIdx === -1 ? beforeResult : beforeResult.slice(0, warningIdx)
  const summaryRaw = line("Summary", metadataHeader) ?? resultSummary ?? line("Error", metadataHeader)
  const warnings = beforeResult
    .split(/^Warning:[ \t]*/m)
    .slice(1)
    .map((warning) => warning.replace(/\n<\/actor-notification>\s*$/, "").trim())
    .filter(Boolean)
    .map(compactForCard)
    .filter(Boolean)
  return {
    status,
    description,
    ...(summaryRaw ? { summary: compactForCard(summaryRaw) } : {}),
    ...(warnings.length ? { warnings } : {}),
  }
}
