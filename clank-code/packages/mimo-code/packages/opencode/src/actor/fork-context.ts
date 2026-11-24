import { createHash } from "node:crypto"
import { asSchema } from "@ai-sdk/provider-utils"
import { jsonSchema, tool, type ModelMessage, type Tool as AITool } from "ai"
import type { Permission } from "@/permission"
import type { MessageID, SessionID } from "@/session/schema"
import type { ModelID, ProviderID } from "@/provider/schema"

export const FORK_CONTEXT_FORMAT_VERSION = 1
export const MAX_FORK_CONTEXT_BYTES = 8 * 1024 * 1024

export interface ForkContext {
  readonly system: string[]
  readonly tools: Record<string, AITool>
  readonly parentPermission: Permission.Ruleset
  readonly inheritedMessages: ModelMessage[]
  readonly watermarkMsgID: MessageID
  readonly model: { providerID: ProviderID; modelID: ModelID }
}

export type ForkContextSource = { sessionID: SessionID; actorID: string }

export type EncodedForkContext = {
  formatVersion: number
  sourceSessionID: SessionID
  sourceActorID: string
  sourceWatermarkMessageID: MessageID
  modelProviderID: ProviderID
  modelID: ModelID
  snapshotJSON: string
  snapshotSHA256: string
  snapshotBytes: number
}

export type StoredForkContext = EncodedForkContext & {
  sessionID: SessionID
  actorID: string
  timeCreated: number
}

export type ForkContextProjection = {
  readonly context: ForkContext
  readonly encoded: EncodedForkContext
  readonly source: ForkContextSource
  /** Independent current rulesets; decisions must be evaluated separately, then intersected. */
  readonly permissionRulesets: Permission.Ruleset[]
  /** Effective tool whitelist intersected through the full-context source lineage. */
  readonly effectiveToolWhitelist?: string[]
}

type Tagged =
  | ["undefined"]
  | ["null"]
  | ["boolean", boolean]
  | ["number", number]
  | ["string", string]
  | ["url", string]
  | ["bytes", string]
  | ["buffer", string]
  | ["arraybuffer", string]
  | ["array", Tagged[]]
  | ["object", [string, Tagged][]]

export class ForkContextCodecError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "ForkContextCodecError"
  }
}

function encodeValue(value: unknown, seen = new WeakSet<object>()): Tagged {
  if (value === undefined) return ["undefined"]
  if (value === null) return ["null"]
  if (typeof value === "boolean") return ["boolean", value]
  if (typeof value === "string") return ["string", value]
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ForkContextCodecError("snapshot contains a non-finite number")
    return ["number", value]
  }
  if (typeof value !== "object") throw new ForkContextCodecError(`snapshot contains unsupported ${typeof value}`)
  if (seen.has(value)) throw new ForkContextCodecError("snapshot contains a circular value")
  seen.add(value)
  try {
    if (value instanceof URL) {
      if (Object.keys(value).length) throw new ForkContextCodecError("snapshot URL has unsupported own properties")
      return ["url", value.href]
    }
    if (Buffer.isBuffer(value)) return ["buffer", value.toString("base64")]
    if (value instanceof Uint8Array) {
      if (Object.getPrototypeOf(value) !== Uint8Array.prototype)
        throw new ForkContextCodecError("snapshot contains an unsupported Uint8Array subclass")
      return ["bytes", Buffer.from(value).toString("base64")]
    }
    if (value instanceof ArrayBuffer) return ["arraybuffer", Buffer.from(value).toString("base64")]
    if (Array.isArray(value)) return ["array", value.map((item) => encodeValue(item, seen))]
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null)
      throw new ForkContextCodecError("snapshot contains an unsupported object prototype")
    if (Object.getOwnPropertySymbols(value).length)
      throw new ForkContextCodecError("snapshot contains symbol properties")
    const properties = Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => {
        return [key, encodeValue((value as Record<string, unknown>)[key], seen)] as [string, Tagged]
      })
    return ["object", properties]
  } finally {
    seen.delete(value)
  }
}

function decodeValue(value: unknown): unknown {
  if (!Array.isArray(value) || typeof value[0] !== "string")
    throw new ForkContextCodecError("snapshot has an invalid tagged value")
  switch (value[0]) {
    case "undefined":
      if (value.length !== 1) break
      return undefined
    case "null":
      if (value.length !== 1) break
      return null
    case "boolean":
      if (value.length === 2 && typeof value[1] === "boolean") return value[1]
      break
    case "number":
      if (value.length === 2 && typeof value[1] === "number" && Number.isFinite(value[1])) return value[1]
      break
    case "string":
      if (value.length === 2 && typeof value[1] === "string") return value[1]
      break
    case "url":
      if (value.length === 2 && typeof value[1] === "string") {
        try {
          return new URL(value[1])
        } catch {
          throw new ForkContextCodecError("snapshot contains an invalid URL")
        }
      }
      break
    case "bytes":
      if (value.length === 2 && typeof value[1] === "string") {
        const bytes = Buffer.from(value[1], "base64")
        if (bytes.toString("base64") !== value[1]) throw new ForkContextCodecError("snapshot contains invalid base64 bytes")
        return new Uint8Array(bytes)
      }
      break
    case "buffer":
      if (value.length === 2 && typeof value[1] === "string") {
        const bytes = Buffer.from(value[1], "base64")
        if (bytes.toString("base64") !== value[1]) throw new ForkContextCodecError("snapshot contains invalid base64 buffer")
        return bytes
      }
      break
    case "arraybuffer":
      if (value.length === 2 && typeof value[1] === "string") {
        const bytes = Buffer.from(value[1], "base64")
        if (bytes.toString("base64") !== value[1]) throw new ForkContextCodecError("snapshot contains invalid base64 ArrayBuffer")
        return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
      }
      break
    case "array":
      if (value.length === 2 && Array.isArray(value[1])) return value[1].map(decodeValue)
      break
    case "object":
      if (value.length === 2 && Array.isArray(value[1])) {
        const result: Record<string, unknown> = {}
        const seen = new Set<string>()
        for (const entry of value[1]) {
          if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string" || seen.has(entry[0]))
            throw new ForkContextCodecError("snapshot contains an invalid object entry")
          seen.add(entry[0])
          Object.defineProperty(result, entry[0], { value: decodeValue(entry[1]), enumerable: true, writable: true, configurable: true })
        }
        return result
      }
      break
  }
  throw new ForkContextCodecError("snapshot has an invalid or unsupported tagged value")
}

function validateContext(input: ForkContext) {
  if (!input || !Array.isArray(input.system) || !input.system.every((item) => typeof item === "string"))
    throw new ForkContextCodecError("snapshot system prefix is invalid")
  if (!Array.isArray(input.inheritedMessages) || !input.inheritedMessages.some((message) => message?.role === "user"))
    throw new ForkContextCodecError("full-context snapshot must contain a user message")
  if (!input.watermarkMsgID || !input.model?.providerID || !input.model?.modelID)
    throw new ForkContextCodecError("snapshot watermark or effective model is missing")
  if (!Array.isArray(input.parentPermission) || !input.parentPermission.every((rule) =>
    rule && typeof rule.permission === "string" && typeof rule.pattern === "string" &&
    (rule.action === "allow" || rule.action === "deny" || rule.action === "ask")))
    throw new ForkContextCodecError("snapshot permission prefix is invalid")
}

export async function encodeForkContext(input: ForkContext, source: ForkContextSource): Promise<EncodedForkContext> {
  validateContext(input)
  const tools = await Promise.all(Object.entries(input.tools).sort(([a], [b]) => a.localeCompare(b)).map(async ([name, item]) => {
    let schema: unknown
    try {
      schema = await asSchema(item.inputSchema).jsonSchema
    } catch (error) {
      throw new ForkContextCodecError(`tool schema ${name} cannot be serialized: ${String(error)}`)
    }
    return { name, description: item.description, inputSchema: schema }
  }))
  const tagged = encodeValue({
    system: input.system,
    tools,
    parentPermission: input.parentPermission,
    inheritedMessages: input.inheritedMessages,
  })
  const snapshotJSON = JSON.stringify(tagged)
  const snapshotBytes = Buffer.byteLength(snapshotJSON, "utf8")
  if (snapshotBytes > MAX_FORK_CONTEXT_BYTES)
    throw new ForkContextCodecError(`snapshot is ${snapshotBytes} bytes; maximum is ${MAX_FORK_CONTEXT_BYTES} bytes`)
  return {
    formatVersion: FORK_CONTEXT_FORMAT_VERSION,
    sourceSessionID: source.sessionID,
    sourceActorID: source.actorID,
    sourceWatermarkMessageID: input.watermarkMsgID,
    modelProviderID: input.model.providerID,
    modelID: input.model.modelID,
    snapshotJSON,
    snapshotSHA256: createHash("sha256").update(snapshotJSON, "utf8").digest("hex"),
    snapshotBytes,
  }
}

export async function decodeForkContext(input: StoredForkContext): Promise<ForkContext> {
  if (input.formatVersion !== FORK_CONTEXT_FORMAT_VERSION)
    throw new ForkContextCodecError(`unsupported snapshot format version ${input.formatVersion}`)
  const actualBytes = Buffer.byteLength(input.snapshotJSON, "utf8")
  if (actualBytes !== input.snapshotBytes || actualBytes > MAX_FORK_CONTEXT_BYTES)
    throw new ForkContextCodecError(`snapshot byte count is invalid (${actualBytes}; recorded ${input.snapshotBytes}; limit ${MAX_FORK_CONTEXT_BYTES})`)
  if (createHash("sha256").update(input.snapshotJSON, "utf8").digest("hex") !== input.snapshotSHA256)
    throw new ForkContextCodecError("snapshot checksum does not match")
  let parsed: unknown
  try {
    parsed = JSON.parse(input.snapshotJSON)
  } catch {
    throw new ForkContextCodecError("snapshot JSON is invalid")
  }
  const decoded = decodeValue(parsed)
  if (!decoded || typeof decoded !== "object" || Array.isArray(decoded))
    throw new ForkContextCodecError("snapshot payload is invalid")
  const payload = decoded as Record<string, unknown>
  if (!Array.isArray(payload.system) || !payload.system.every((item) => typeof item === "string") ||
    !Array.isArray(payload.inheritedMessages) || !Array.isArray(payload.parentPermission) ||
    !Array.isArray(payload.tools))
    throw new ForkContextCodecError("snapshot payload fields are invalid")
  const tools: Record<string, AITool> = {}
  for (const item of payload.tools) {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new ForkContextCodecError("snapshot tool entry is invalid")
    const entry = item as Record<string, unknown>
    if (typeof entry.name !== "string" || (entry.description !== undefined && typeof entry.description !== "string") ||
      !entry.inputSchema || typeof entry.inputSchema !== "object" || Array.isArray(entry.inputSchema) || tools[entry.name])
      throw new ForkContextCodecError("snapshot tool entry fields are invalid")
    tools[entry.name] = tool({
      ...(typeof entry.description === "string" ? { description: entry.description } : {}),
      inputSchema: jsonSchema(entry.inputSchema as Record<string, unknown>),
    })
  }
  const context: ForkContext = {
    system: payload.system as string[],
    tools,
    parentPermission: payload.parentPermission as Permission.Ruleset,
    inheritedMessages: payload.inheritedMessages as ModelMessage[],
    watermarkMsgID: input.sourceWatermarkMessageID,
    model: { providerID: input.modelProviderID, modelID: input.modelID },
  }
  validateContext(context)
  return context
}
