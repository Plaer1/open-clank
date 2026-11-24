import { Database, sql } from '@/storage'
import { managedSessionBinding, markerMatches, type ManagedSessionBinding } from '@/memory/session-scope'
import type { observeReceipts } from '@/session/user-input'

/** The getter returns clones; retain the private registration token as well as all public binding fields. */
export function inspectionBindingCurrent(sessionID: string, original: ManagedSessionBinding): boolean {
  const current = managedSessionBinding(sessionID)
  return !!current && markerMatches(sessionID, original) && markerMatches(sessionID, current) &&
    original.registrationMarker.token === current.registrationMarker.token && JSON.stringify(original) === JSON.stringify(current)
}

type ReceiptPage = ReturnType<typeof observeReceipts>
export type TranscriptPart = { id: string; messageID: string; role: 'user' | 'assistant' | 'unknown'; time: number | null; kind: 'text' | 'tool' | 'omitted'; text: string | null; tool: string | null; state: 'pending' | 'running' | 'completed' | 'error' | null; omission: 'hidden' | 'attachment' | 'oversize' | 'unsupported' | null }

type Inspection = { transcriptVersion: 1; actorID: string; sessionID: string; executionRevision: number; status: string;
  messages: TranscriptPart[]; messageNext?: string; receipts: ReceiptPage; coverage: 'partial' }

/** Count actual UTF-8 JSON, including escaping; each dropped suffix is represented by its last returned key. */
export function fitInspection(input: Inspection, maxBytes = 48000): Inspection {
  const page: Inspection = { ...input, messages: [...input.messages], receipts: { ...input.receipts, items: [...input.receipts.items] } }
  const bytes = () => Buffer.byteLength(JSON.stringify(page), 'utf8')
  while (bytes() > maxBytes && page.messages.length > 1) {
    page.messages.pop()
    page.messageNext = page.messages.at(-1)!.id
  }
  while (bytes() > maxBytes && page.receipts.items.length > 1) {
    page.receipts.items.pop()
    page.receipts.next = page.receipts.items.at(-1)!.requestID
  }
  if (bytes() > maxBytes) throw new Error('inspection response exceeds budget')
  return page
}

/** Public part-only projection; neither raw JSON nor unbounded fields leave SQLite. */
export function inspectTranscript(sessionID: string, actorID: string, before?: string): TranscriptPart[] {
  return Database.use((db) => db.all<TranscriptPart>(sql`
    WITH bounded AS (
      SELECT p.id, m.id AS messageID, m.time_created AS created,
        CASE WHEN length(CAST(m.data AS BLOB)) <= 4096 AND json_valid(m.data) THEN m.data ELSE '{}' END AS message,
        CASE WHEN length(CAST(p.data AS BLOB)) <= 4096 AND json_valid(p.data) THEN p.data ELSE NULL END AS part
      FROM message m JOIN part p ON p.message_id=m.id AND p.session_id=m.session_id
      WHERE m.session_id=${sessionID} AND m.agent_id=${actorID}
        AND length(CAST(p.id AS BLOB)) BETWEEN 1 AND 256
        AND length(CAST(m.id AS BLOB)) BETWEEN 1 AND 256
        AND (${before ?? null} IS NULL OR p.id < ${before ?? null})
      ORDER BY p.id DESC LIMIT 21
    ), classified AS (
      SELECT *, CASE WHEN part IS NULL THEN 'oversize'
        WHEN json_extract(part,'$.type')='text' AND (json_extract(part,'$.synthetic') IS TRUE OR json_extract(part,'$.ignored') IS TRUE) THEN 'hidden'
        WHEN json_extract(part,'$.type')='text' AND (json_type(part,'$.text') IS NOT 'text' OR length(CAST(json_extract(part,'$.text') AS BLOB)) > 2048) THEN 'oversize'
        WHEN json_extract(part,'$.type')='text' THEN NULL
        WHEN json_extract(part,'$.type')='tool' THEN NULL
        WHEN json_extract(part,'$.type')='file' THEN 'attachment' ELSE 'unsupported' END AS omitted
      FROM bounded
    )
    SELECT id, messageID,
      CASE WHEN json_extract(message,'$.role') IN ('user','assistant') THEN json_extract(message,'$.role') ELSE 'unknown' END AS role,
      CASE WHEN typeof(created)='integer' AND created BETWEEN 0 AND 9007199254740991 THEN created ELSE NULL END AS time,
      CASE WHEN omitted IS NOT NULL THEN 'omitted' WHEN json_extract(part,'$.type')='text' THEN 'text' ELSE 'tool' END AS kind,
      CASE WHEN omitted IS NULL AND json_extract(part,'$.type')='text' THEN json_extract(part,'$.text') ELSE NULL END AS text,
      CASE WHEN omitted IS NULL AND json_extract(part,'$.type')='tool' AND json_type(part,'$.tool')='text'
        AND length(CAST(json_extract(part,'$.tool') AS BLOB)) BETWEEN 1 AND 128
        AND json_extract(part,'$.tool') NOT GLOB '*[^a-zA-Z0-9_.:-]*' THEN json_extract(part,'$.tool') ELSE NULL END AS tool,
      CASE WHEN omitted IS NULL AND json_extract(part,'$.type')='tool' AND json_extract(part,'$.state.status') IN ('pending','running','completed','error')
        THEN json_extract(part,'$.state.status') ELSE NULL END AS state, omitted AS omission
    FROM classified ORDER BY id DESC`))
}
