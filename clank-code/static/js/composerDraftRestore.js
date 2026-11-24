// Atomic handoff of an owner-validated history receipt into the existing composer.
export function restoreValidatedComposerDraft(receipt, { sid, expected, read, commit }) {
  if (!receipt || receipt.available !== true) return { restored: false, reason: 'attachments-unavailable' };
  if (receipt.content_kind !== 'text' || typeof receipt.raw_content !== 'string') return { restored: false, reason: 'invalid-content' };
  if (!Array.isArray(receipt.attachment_refs)) return { restored: false, reason: 'invalid-attachments' };
  const ids = [];
  for (const ref of receipt.attachment_refs) {
    if (ref?.type !== 'attachment_ref' || typeof ref.attachment_id !== 'string' || !ref.attachment_id.trim()) return { restored: false, reason: 'invalid-attachments' };
    ids.push(ref.attachment_id);
  }
  const current = read();
  if (!expected || current.sid !== sid || expected.sid !== sid || current.epoch !== expected.epoch || current.version !== expected.version || current.text !== expected.text || current.attachments !== expected.attachments || current.pending.length !== expected.pending.length || current.pending.some((file, i) => file !== expected.pending[i])) return { restored: false, reason: 'draft-changed' };
  commit({ text: receipt.raw_content, attachmentIds: [...new Set(ids)] });
  return { restored: true };
}
