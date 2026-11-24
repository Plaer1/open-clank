/** Bounded observed capture membership; it is never a recovery grant. */
import z from "zod"

const identifier = z.string().min(1).max(512)
export const CaptureMembership = z.object({
  version: z.literal(1),
  sessionID: identifier,
  messageID: identifier,
  stepID: identifier,
  accountID: identifier,
  hostChatID: identifier,
  workspaceID: identifier,
  actorID: identifier,
  executionRevision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  rootOperationID: identifier,
  innerTurnID: identifier,
  revision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  coverage: z.enum(["open", "partial", "interrupted"]),
  members: z.array(z.object({
    sequence: z.number().int().nonnegative().max(63),
    actionID: identifier,
    toolCallID: identifier,
    state: z.enum(["pending", "complete"]),
    actionRef: z.string().min(1).max(4096).optional(),
  }).strict()).max(64),
}).strict()
export type CaptureMembership = z.infer<typeof CaptureMembership>

/** Imported data is not an owner-issued membership, even if it has its shape. */
export function stripCaptureMembership<T>(value: T): T {
  if (!value || typeof value !== "object" || Array.isArray(value) || !("captureMembership" in value)) return value
  const clean = { ...value } as T & { captureMembership?: unknown }
  delete clean.captureMembership
  return clean
}

/** Check the exact first-party single-resource action locator, not its prefix. */
export function matchesActionRef(ref: string, binding: CaptureMembership, actionID: string, toolCallID: string): boolean {
  try {
    const prefix = ref?.startsWith("oc-history-action:v1:") ? "oc-history-action:v1:" :
      ref?.startsWith("oc-history-action:v2:") ? "oc-history-action:v2:" : ""
    if (typeof ref !== "string" || !prefix || ref.length > 4096) return false
    const encoded = ref.slice(prefix.length)
    if (!/^[A-Za-z0-9_-]+$/.test(encoded)) return false
    const bytes = Buffer.from(encoded, "base64url")
    if (bytes.toString("base64url") !== encoded) return false
    const text = new TextDecoder("utf-8", {fatal:true}).decode(bytes)
    const value: unknown = JSON.parse(text)
    if (!value || typeof value !== "object" || Array.isArray(value) || JSON.stringify(value) !== text) return false
    const item = value as Record<string, unknown>
    if (item.actionID !== actionID || !item.origin || typeof item.origin !== "object" || Array.isArray(item.origin)) return false
    if (prefix === "oc-history-action:v1:") {
      if (Object.keys(item).sort().join(",") !== "actionID,afterVersionID,beforeVersionID,origin,resourceID,version"
          || item.version !== 1 || typeof item.resourceID !== "string" || !item.resourceID.startsWith("file:")
          || item.resourceID.length <= 5 || Buffer.byteLength(item.resourceID,"utf8") > 256
          || typeof item.beforeVersionID !== "string" || !item.beforeVersionID || Buffer.byteLength(item.beforeVersionID,"utf8") > 256
          || typeof item.afterVersionID !== "string" || !item.afterVersionID || Buffer.byteLength(item.afterVersionID,"utf8") > 256) return false
    } else if (Object.keys(item).sort().join(",") !== "actionID,completeDigest,origin,requestDigest,version" || item.version !== 2 ||
        typeof item.requestDigest !== "string" || !/^[a-f0-9]{64}$/.test(item.requestDigest) ||
        typeof item.completeDigest !== "string" || !/^[a-f0-9]{64}$/.test(item.completeDigest)) return false
    const origin = item.origin as Record<string, unknown>
    if (Object.keys(origin).sort().join(",") !== "actor_id,assistant_message_id,engine_session_id,execution_revision,host_chat_id,inner_turn_id,root_operation_id,tool_call_id,version") return false
    return origin.version === 1 && origin.host_chat_id === binding.hostChatID && origin.engine_session_id === binding.sessionID
      && origin.assistant_message_id === binding.messageID && origin.actor_id === binding.actorID
      && origin.execution_revision === binding.executionRevision && origin.root_operation_id === binding.rootOperationID
      && origin.inner_turn_id === binding.innerTurnID && origin.tool_call_id === toolCallID
  } catch { return false }
}

type Part = { id: string; sessionID: string; messageID: string; type: string; captureMembership?: unknown }
let issuing: { id: string; sessionID: string; messageID: string; value: CaptureMembership } | undefined

/** The issuance window is synchronous and never crosses an await. */
export function issueMembership<T extends Part, R>(part: T, value: CaptureMembership, publish: (part: T) => R): R {
  const checked = CaptureMembership.parse(value)
  if (issuing || part.type !== "step-start" || checked.stepID !== part.id || checked.sessionID !== part.sessionID
      || checked.messageID !== part.messageID || Buffer.byteLength(JSON.stringify(checked), "utf8") > 32768)
    throw new Error("capture membership unavailable")
  issuing = { id: part.id, sessionID: part.sessionID, messageID: part.messageID, value: checked }
  try { return publish({ ...part, captureMembership: checked }) } finally { issuing = undefined }
}

/** Ordinary updates preserve only the previously stored owner field. */
export function projectMembership<T extends Part>(part: T, previous?: Part): T {
  const next = { ...part }
  delete next.captureMembership
  if (previous?.type === "step-start" && previous.captureMembership !== undefined && part.type !== "step-start")
    throw new Error("capture membership step type cannot change")
  if (part.type !== "step-start") return next
  if (issuing && issuing.id === part.id && issuing.sessionID === part.sessionID && issuing.messageID === part.messageID
      && part.captureMembership === issuing.value) {
    next.captureMembership = issuing.value
  } else if (previous?.type === "step-start" && previous.captureMembership !== undefined) {
    next.captureMembership = previous.captureMembership
  }
  return next
}
