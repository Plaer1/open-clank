import { managedSessionBinding, markerMatches, type ManagedSessionBinding } from '@/memory/session-scope'
import type { observeReceipts } from '@/session/user-input'

/** The getter returns clones; retain the private registration token as well as all public binding fields. */
export function inspectionBindingCurrent(sessionID: string, original: ManagedSessionBinding): boolean {
  const current = managedSessionBinding(sessionID)
  return !!current && markerMatches(sessionID, original) && markerMatches(sessionID, current) &&
    original.registrationMarker.token === current.registrationMarker.token && JSON.stringify(original) === JSON.stringify(current)
}

type ReceiptPage = ReturnType<typeof observeReceipts>
type Inspection = { actorID: string; sessionID: string; executionRevision: number; status: string;
  messages: { id: string; text: string | null }[]; messageNext?: string; receipts: ReceiptPage; coverage: 'partial' }

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
