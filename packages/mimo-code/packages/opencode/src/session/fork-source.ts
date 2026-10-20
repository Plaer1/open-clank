import type { MessageV2 } from "./message-v2"

export type SourceMessage = { info: { id: string; role: string; agentID?: string }; parts: Array<{ type: string; text?: string; synthetic?: boolean; ignored?: boolean; metadata?: Record<string, unknown> }> }

/** Explicit legacy server framing is recoverable; arbitrary message ordering is not. */
export function hostSourceForMessage(message: SourceMessage): string | undefined {
  const explicit = message.parts.filter((part) => part.type === "text" && typeof part.metadata?.hostSourceMessageID === "string")
  if (explicit.length > 1) throw new Error("Multiple host source anchors occur in one native message")
  if (explicit.length === 1) return explicit[0].metadata!.hostSourceMessageID as string
  const first = message.parts.find((part) => part.type === "text" && !part.synthetic && !part.ignored)
  return first?.text?.match(/^\[turn_source_id=([^\]]{1,256})\]/)?.[1]
}

/** Validate the complete immutable source snapshot before creating a managed fork. */
export function validateManagedForkSources(messages: MessageV2.WithParts[], mapping: Record<string, string>) {
  const sources = messages.filter((message) => message.info.role === "user" && (!message.info.agentID || message.info.agentID === "main")).map((message) => {
    const source = hostSourceForMessage(message)
    if (!source) throw new Error("Managed fork requires exactly one host source anchor per user message")
    return source
  })
  if (new Set(sources).size !== sources.length || Object.keys(mapping).length !== sources.length || sources.some((source) => !mapping[source])) throw new Error("Managed fork source mapping is incomplete or ambiguous")
  if (new Set(Object.values(mapping)).size !== sources.length) throw new Error("Managed fork target source IDs must be unique")
}

export function remapForkPart(part: MessageV2.Part, mapping?: Record<string, string>, legacySource?: string): MessageV2.Part {
  if (!mapping || part.type !== "text") return part
  const { nativeForkReceipt: previousReceipt, ...metadata } = part.metadata ?? {}
  if (legacySource) {
    const target = mapping[legacySource]
    if (!target) throw new Error("Managed fork part has an unmapped legacy source anchor")
    return { ...part, text: part.text.replace(/^\[turn_source_id=[^\]]+\]/, `[turn_source_id=${target}]`), metadata: { ...metadata, hostSourceMessageID: target } }
  }
  if (typeof metadata.hostSourceMessageID !== "string") return previousReceipt ? { ...part, metadata } : part
  const target = mapping[metadata.hostSourceMessageID]
  if (!target) throw new Error("Managed fork part has an unmapped host source anchor")
  return { ...part, metadata: { ...metadata, hostSourceMessageID: target } }
}
