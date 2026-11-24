/** Bounded projection of a private first-party search_code return. No source
 * bytes leave this function; a resource is linked only to verified full-file
 * evidence and its original code receipt. */
export type CodeReadResource = {resourceID:string; sourceHash:string; receipt:Record<string, unknown>} | null
export function codeReadResource(text: string): {resource:CodeReadResource} | undefined {
  if (Buffer.byteLength(text, 'utf8') > 1_000_000) return
  try {
    const wrapper = JSON.parse(text)
    if (!wrapper || wrapper.exit_code !== 0 || typeof wrapper.output !== 'string') return
    const result = JSON.parse(wrapper.output)
    const window = result?.source_window
    if (result?.schema !== 'open-clank.code-query/v1' || result.operation !== 'read' || result.error != null ||
        !window || !Number.isSafeInteger(window.start_byte) || !Number.isSafeInteger(window.end_byte) ||
        window.start_byte < 0 || window.end_byte < window.start_byte || !Array.isArray(result.hits)) return
    const hit = result.hits.length === 1 ? result.hits[0] : null
    const receipt = result.receipt
    const keys = ['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint']
    const receiptValid = receipt && typeof receipt === 'object' && !Array.isArray(receipt) &&
      Object.keys(receipt).length === keys.length && keys.every(k => ['admission_epoch','policy_generation'].includes(k)
        ? Number.isSafeInteger(receipt[k]) && receipt[k] >= 0
        : typeof receipt[k] === 'string' && receipt[k].length > 0 && receipt[k].length <= 256)
    const source = hit?.source
    if (window.continuity !== 'full_sha256' || !receiptValid || hit?.kind !== 'raw_window' ||
        hit.current_source_verified !== true || hit.evidence_state !== 'current' ||
        typeof hit.id !== 'string' || !/^file:[0-9a-f]{64}$/i.test(hit.id) ||
        typeof source?.file_id !== 'string' || hit.id !== `file:${source.file_id}` ||
        typeof source.source_hash !== 'string' || !/^[0-9a-f]{64}$/i.test(source.source_hash)) return {resource:null}
    return {resource:{resourceID:hit.id, sourceHash:source.source_hash, receipt}}
  } catch { return }
}
