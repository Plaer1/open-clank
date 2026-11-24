/** Pure rendering of a separately authenticated, complete source-outline sidecar.
 * This function is not an admission or currentness check. */
import { createHash } from 'node:crypto'

type HistoryRef = { messageID: string; partID: string; revision: number; contentHash: string }
type RecordValue = Record<string, unknown>
const MAX_BYTES = 262144
const digest = (value: string) => createHash('sha256').update(value, 'utf8').digest('hex')
const record = (value: unknown): value is RecordValue => value !== null && typeof value === 'object' && !Array.isArray(value)
const exactKeys = (value: RecordValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
const positiveText = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 4096
const hex = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const nonnegative = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0

const receiptKeys = ['workspace_id', 'registration_id', 'source_snapshot_id', 'checkout_id',
  'head_generation', 'admission_epoch', 'policy_generation', 'config_fingerprint']
const receiptNumbers = new Set(['admission_epoch', 'policy_generation'])
function validReceipt(value: unknown): value is RecordValue {
  return record(value) && exactKeys(value, receiptKeys) && receiptKeys.every(key =>
    receiptNumbers.has(key) ? nonnegative(value[key]) :
      typeof value[key] === 'string' && (value[key] as string).length > 0 && (value[key] as string).length <= 256)
}
function sameReceipt(left: RecordValue, right: RecordValue): boolean {
  return receiptKeys.every(key => left[key] === right[key])
}

/** Return undefined on any incomplete, inconsistent or non-shrinking input. */
export function sourceOutlineFrame(rawResult: string, sidecar: unknown, callID: string, ref: HistoryRef): string | undefined {
  try {
    if (typeof rawResult !== 'string' || Buffer.byteLength(rawResult, 'utf8') > MAX_BYTES || !positiveText(callID) ||
        !record(sidecar) || !exactKeys(sidecar, ['version', 'toolCallID', 'outputSHA256', 'outline', 'extraCost']) ||
        sidecar.version !== 1 || sidecar.toolCallID !== callID || sidecar.outputSHA256 !== digest(rawResult) ||
        !record(sidecar.extraCost)) return
    const extraCost = sidecar.extraCost
    if (!exactKeys(extraCost, ['work', 'bytes', 'tokens', 'rpcs', 'files_bytes_read', 'complete']) ||
        extraCost.complete !== true || !['work', 'bytes', 'tokens', 'rpcs', 'files_bytes_read'].every(key => nonnegative(extraCost[key])) ||
        !positiveText(ref.messageID) || !positiveText(ref.partID) || !Number.isSafeInteger(ref.revision) || ref.revision < 1 || !hex(ref.contentHash)) return
    const wrapper: unknown = JSON.parse(rawResult)
    if (!record(wrapper) || !exactKeys(wrapper, ['output', 'exit_code']) || wrapper.exit_code !== 0 || typeof wrapper.output !== 'string') return
    const read: unknown = JSON.parse(wrapper.output)
    const outline = sidecar.outline
    if (!record(read) || !record(outline) || Buffer.byteLength(JSON.stringify(outline), 'utf8') > MAX_BYTES ||
        read.schema !== 'open-clank.code-query/v1' || read.operation !== 'read' || read.error !== null ||
        read.partial !== false || read.degraded !== false || read.next_cursor !== null || !validReceipt(read.receipt) ||
        outline.schema !== read.schema || outline.operation !== 'outline' || outline.error !== null ||
        outline.partial !== false || outline.degraded !== false || outline.next_cursor !== null || !validReceipt(outline.receipt) ||
        !sameReceipt(read.receipt, outline.receipt) || !Array.isArray(read.hits) || read.hits.length !== 1 ||
        !Array.isArray(outline.hits) || outline.hits.length === 0 || outline.hits.length > 200 ||
        !Array.isArray(outline.omissions) || !Array.isArray(outline.counts) || !Array.isArray(outline.reasons)) return
    const hit: unknown = read.hits[0]
    if (!record(hit) || hit.kind !== 'raw_window' || hit.evidence_state !== 'current' || hit.current_source_verified !== true ||
        !positiveText(hit.id) || !hit.id.startsWith('file:') || !record(hit.source) || !record(hit.raw_source) ||
        !record(hit.properties) || typeof hit.excerpt !== 'string') return
    const source = hit.source, observed = hit.raw_source
    if (!positiveText(source.file_id) || hit.id !== `file:${source.file_id}` || !positiveText(source.rel_path) ||
        !hex(source.source_hash) || !record(source.span) || observed.rel_path !== source.rel_path ||
        observed.start_byte !== 0 || !nonnegative(observed.file_size) || observed.file_size === 0 || observed.file_size > 32768 ||
        observed.end_byte !== observed.file_size || source.span.start_byte !== 0 || source.span.end_byte !== observed.file_size || observed.hash_algorithm !== 'sha256' || observed.hash_scope !== 'file' ||
        !hex(observed.hash) || observed.hash !== digest(hit.excerpt) || Buffer.byteLength(hit.excerpt, 'utf8') !== observed.file_size ||
        observed.state !== 'observed_file' || observed.continuation !== null || hit.properties.rel_path !== source.rel_path ||
        hit.properties.window_complete !== true || hit.properties.indexed_source_hash !== source.source_hash ||
        !record(read.source_window) || read.source_window.rel_path !== source.rel_path || read.source_window.start_byte !== 0 ||
        read.source_window.end_byte !== observed.file_size || read.source_window.continuation !== null || read.source_window.continuity !== 'full_sha256') return
    const declarationIDs = new Set<string>()
    for (const declaration of outline.hits) {
      if (!record(declaration) || declaration.kind !== 'declaration' || !positiveText(declaration.id) ||
          typeof declaration.signature !== 'string' || !record(declaration.source) || !record(declaration.source.span) ||
          declaration.source.file_id !== source.file_id || declaration.source.rel_path !== source.rel_path ||
          declaration.source.source_hash !== source.source_hash || !record(declaration.properties) ||
          !Array.isArray(declaration.matches) || typeof declaration.evidence_state !== 'string' ||
          typeof declaration.current_source_verified !== 'boolean' || declarationIDs.has(declaration.id) ||
          !nonnegative(declaration.source.span.start_byte) || !nonnegative(declaration.source.span.end_byte) ||
          declaration.source.span.start_byte >= declaration.source.span.end_byte || declaration.source.span.end_byte > observed.file_size) return
      declarationIDs.add(declaration.id)
    }
    const invocation = JSON.stringify({operation:'exact_output', message_id:ref.messageID, part_id:ref.partID,
      revision:ref.revision, content_hash:ref.contentHash, byte_offset:0, byte_limit:8192})
    const frame = `[Indexed declarations; file body omitted. Original: history(${invocation}); follow nextOffset for further pages. The index is not a semantic-equivalence claim.]\n${JSON.stringify(outline)}`
    return Buffer.byteLength(frame, 'utf8') < Buffer.byteLength(rawResult, 'utf8') ? frame : undefined
  } catch {
    return undefined
  }
}
