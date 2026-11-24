import { AppFileSystem } from "@mimo-ai/shared/filesystem"

/** A locator for one exact direct-edit action, never a Snapshot or grant. */
export const MANAGED_ACTION_PREFIX = "oc-history-action:v1:"
export const assertGitSnapshotRef = (value: string) => {
  if (value.startsWith("oc-history-action:") || value.startsWith("oc-snapshot:")) throw new Error("Managed History evidence requires explicit scoped authority; it is not a Git Snapshot.")
}
const MAX_BYTES = 512 * 1024
const keys = ["version", "actionID", "resourceID", "beforeVersionID", "afterVersionID", "origin"]
const originKeys = ["version", "host_chat_id", "engine_session_id", "root_operation_id", "actor_id", "execution_revision", "inner_turn_id", "tool_call_id", "assistant_message_id"]
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0 && Buffer.byteLength(value) <= 256 && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/.test(value)
const closed = (value: any, fields: string[]) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === fields.length && fields.every((key) => Object.hasOwn(value, key))

export type ManagedActionSide = { state: "absent" | "expired" | "unavailable" } | { state: "text" | "binary"; bytes: Uint8Array }
export type ManagedActionResult = { before: ManagedActionSide; after: ManagedActionSide }

/** Exact-action read with an authority supplied by the caller, never a model argument. */
export async function readManagedAction(ref: string, context: AppFileSystem.HistoryContext, isCurrent: () => boolean): Promise<ManagedActionResult> {
  if (typeof isCurrent !== "function" || !isCurrent() || !ref.startsWith(MANAGED_ACTION_PREFIX) || ref.length > 4096) throw new Error("managed action authority unavailable")
  const encoded = ref.slice(MANAGED_ACTION_PREFIX.length)
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) throw new Error("invalid managed action reference")
  const raw = Buffer.from(encoded, "base64url")
  if (raw.toString("base64url") !== encoded || raw.length > 3000) throw new Error("invalid managed action reference")
  let value: any
  try { value = JSON.parse(raw.toString("utf8")) } catch { throw new Error("invalid managed action reference") }
  if (!closed(value, keys) || value.version !== 1 || ![value.actionID, value.resourceID, value.beforeVersionID, value.afterVersionID].every(text) ||
      !value.resourceID.startsWith("file:") || !closed(value.origin, originKeys) || value.origin.version !== 1 ||
      !Number.isSafeInteger(value.origin.execution_revision) || value.origin.execution_revision < 0 ||
      !originKeys.filter((k) => k !== "version" && k !== "execution_revision").every((k) => text(value.origin[k]))) throw new Error("invalid managed action reference")
  const request = async (operation: Parameters<typeof AppFileSystem.historyActionRead>[2]) => {
    if (!isCurrent()) throw new Error("managed action authority changed")
    const result = await AppFileSystem.historyActionRead(context, value.actionID, operation)
    if (!isCurrent()) throw new Error("managed action authority changed")
    if (result?.Unavailable) throw new Error("managed action unavailable")
    return result
  }
  const resolved = await request({ kind: "resource", resourceID: value.resourceID })
  const handle = resolved?.Resource?.handle
  if (handle?.resource_id !== value.resourceID || handle?.account_id !== context.accountId || handle?.workspace_id !== context.workspaceId) throw new Error("managed resource unavailable")
  const response = await request({ kind: "action" })
  const action = response?.Action
  if (!action || action.state !== "Complete" || action.action_id !== value.actionID || action.actor_id !== context.actorId || action.actor_account_id !== context.accountId ||
      !closed(action.managed_tool_origin_v1, originKeys) || !originKeys.every((k) => action.managed_tool_origin_v1[k] === value.origin[k]) ||
      !Array.isArray(action.before_resources) || action.before_resources.length !== 1 || !Array.isArray(action.after_resources) || action.after_resources.length !== 1) throw new Error("managed action evidence unavailable")
  const before = action.before_resources[0], after = action.after_resources[0]
  const sameResource = (resource: any) => resource?.resource_id === value.resourceID && resource?.account_id === context.accountId && resource?.workspace_id === context.workspaceId && resource?.provider === "mimo-filesystem"
  if (!sameResource(before?.resource_key) || !sameResource(after?.resource_key) || before.before?.version_id !== value.beforeVersionID || after.after?.version_id !== value.afterVersionID) throw new Error("managed action resource or version changed")
  let remaining = MAX_BYTES
  const read = async (receipt: any): Promise<ManagedActionSide> => {
    try {
      const info = (await request({ kind: "info", receipt }))?.VersionInfo
      if (!info || !Object.hasOwn(info, "content_length")) return { state: "unavailable" }
      if (info.content_length === null) return receipt.content === "Tombstone" ? { state: "absent" } : { state: "unavailable" }
      const length = info.content_length
      if (!Number.isSafeInteger(length) || length < 0 || length > remaining) return { state: "unavailable" }
      remaining -= length
      const chunks: Buffer[] = []; let offset = 0
      do {
        const count = Math.min(256 * 1024, Math.max(1, length - offset))
        const chunk = (await request({ kind: "chunk", receipt, offset, length: count }))?.Chunk
        if (!chunk || chunk.offset !== offset || typeof chunk.content !== "string" || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(chunk.content)) return { state: "unavailable" }
        const bytes = Buffer.from(chunk.content, "base64")
        if (bytes.toString("base64") !== chunk.content || bytes.length > count || offset + bytes.length > length || (!bytes.length && offset < length)) return { state: "unavailable" }
        chunks.push(bytes); offset += bytes.length
        if (chunk.eof !== (offset === length)) return { state: "unavailable" }
      } while (offset < length)
      const bytes = Buffer.concat(chunks)
      if (bytes.includes(0)) return { state: "binary", bytes }
      try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); return { state: "text", bytes } } catch { return { state: "binary", bytes } }
    } catch (error) {
      if (!isCurrent()) throw new Error("managed action authority changed")
      return { state: error instanceof Error && error.message === "history payload expired" ? "expired" : "unavailable" }
    }
  }
  const first = await read(before.before)
  const second = await read(after.after)
  if (!isCurrent()) throw new Error("managed action authority changed")
  return { before: first, after: second }
}
