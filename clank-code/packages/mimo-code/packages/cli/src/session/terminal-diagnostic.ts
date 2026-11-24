/** Native-only, content-free terminal evidence. It never grants source access. */
export const TERMINAL_ERROR_NAMES = new Set([
  'ProviderAuthError','APIError','MessageOutputLengthError','StructuredOutputError',
  'InvalidOutputError','StreamIncompleteError','TextToolCallError','ContentFilterError','ModelError','UnknownError',
])
export type TerminalDiagnostic = {version:1; errorName:string; stage:'processor'|'prompt_classification'}
export function validTerminalDiagnostic(value:unknown): value is TerminalDiagnostic {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item=value as Record<string,unknown>
  return Object.keys(item).sort().join(',')==='errorName,stage,version' && item.version===1 && typeof item.errorName==='string' && TERMINAL_ERROR_NAMES.has(item.errorName) && (item.stage==='processor' || item.stage==='prompt_classification')
}
/** Imported/forked messages retain user content but not local issuance evidence. */
export function stripImportedMessageEvidence<T>(value:T):T {
  if (!value || typeof value!=='object' || Array.isArray(value)) return value
  const row=value as Record<string,unknown>
  if (row.role!=='assistant' || !row.metadata || typeof row.metadata!=='object' || Array.isArray(row.metadata)) return value
  const {terminalDiagnostic: _diagnostic, managedExecution: _execution, ...metadata}=row.metadata as Record<string,unknown>
  return {...row,metadata} as T
}
/** Generic events cannot mint a marker or alter the one already on the row. */
export function protectTerminalDiagnostic<T extends {metadata?:Record<string,unknown>}>(previous:unknown, incoming:T):T {
  const next={...incoming}, meta=next.metadata && typeof next.metadata==='object' && !Array.isArray(next.metadata) ? {...next.metadata as Record<string,unknown>} : undefined
  if (!meta) return next
  delete meta.terminalDiagnostic
  const old=previous && typeof previous==='object' ? previous as Record<string,unknown> : undefined
  const oldMeta=old?.metadata && typeof old.metadata==='object' && !Array.isArray(old.metadata) ? old.metadata as Record<string,unknown> : undefined
  if (oldMeta && validTerminalDiagnostic(oldMeta.terminalDiagnostic) && JSON.stringify(oldMeta.managedExecution)===JSON.stringify(meta.managedExecution) && JSON.stringify(old?.error)===JSON.stringify('error' in next ? next.error : undefined)) meta.terminalDiagnostic=oldMeta.terminalDiagnostic
  return Object.assign(next,{metadata:meta})
}
