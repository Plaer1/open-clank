export const CLAUDE_SUBSCRIPTION_REQUEST_PROFILE = Object.freeze({
  id: "openclank-claude-subscription-v1",
  anthropicVersion: "2023-06-01",
  oauthBeta: "oauth-2025-04-20",
  codeBeta: "claude-code-20250219",
  thinkingBeta: "interleaved-thinking-2025-05-14",
  toolsBeta: "fine-grained-tool-streaming-2025-05-14",
  identity: "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
  billingPrefix: "x-anthropic-billing-header:",
  checksumMarker: "cch=00000",
  checksumSeed: 0x4d659218e32a3268n,
  checksumMask: 0xfffffn,
  paidFallbackBeta: "fallback-credit-2026-06-01",
  cliVersion: "2.1.280",
  userAgent: "claude-cli/2.1.280 (external, cli)",
  packageVersion: "0.71.2",
  billingVersion: "1.0.0",
  billingEntrypoint: "openclank",
})

const BILLING_TEXT = `${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix} ${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMarker}; cc_version=${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingVersion}; cc_entrypoint=${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingEntrypoint};`
const IDENTITY = CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.identity
const CONDITIONAL_BETAS = new Set<string>([
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.oauthBeta,
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.codeBeta,
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta,
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta,
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.paidFallbackBeta,
])

export type ClaudeSubscriptionRequestContext = {
  /** Existing provider account UUID, passed through unchanged when available. */
  accountUUID?: string
  /** Existing stable Open Clank session and installation identifiers. */
  sessionID: string
  deviceID: string
  platform: string
  architecture: string
  runtime: string
  runtimeVersion: string
}

export type ClaudeSubscriptionRequestProfile = {
  profileId: string
  body: string
  headers: Headers
}

function stableIdentifier(name: string, value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 256 || /[\u0000-\u001f]/u.test(value)) {
    throw new Error(`Claude request profile requires a valid ${name}`)
  }
  return value
}

function record(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

function isOwnedProfilePrefix(system: unknown[]): boolean {
  const billing = record(system[0])
  const identity = record(system[1])
  const line = billing?.type === "text" && typeof billing.text === "string" ? billing.text : undefined
  if (!line || identity?.type !== "text" || identity.text !== IDENTITY) return false
  const start = `${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix} cch=`
  const suffix = `; cc_version=${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingVersion}; cc_entrypoint=${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingEntrypoint};`
  if (!line.startsWith(start) || !line.endsWith(suffix)) return false
  const checksum = line.slice(start.length, line.length - suffix.length)
  return /^[0-9a-f]{5}$/u.test(checksum)
}

function withChecksum(serialized: string): string {
  const prefix = `{"system":[{"type":"text","text":"${CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix} `
  if (!serialized.startsWith(prefix)) throw new Error("Claude billing prefix is not in its canonical request position")
  const markerAt = serialized.indexOf(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMarker, prefix.length - 1)
  const markerEnd = markerAt + CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMarker.length
  if (markerAt < prefix.length - 1 || serialized[markerEnd] !== ";") {
    throw new Error("Claude billing checksum marker is missing from the owned system prefix")
  }
  const bytes = new TextEncoder().encode(serialized)
  const checksum = (Bun.hash.xxHash64(bytes, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumSeed)
    & CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMask).toString(16).padStart(5, "0")
  const byteOffset = new TextEncoder().encode(serialized.slice(0, markerAt + 4)).length
  bytes.set(new TextEncoder().encode(checksum), byteOffset)
  return new TextDecoder().decode(bytes)
}

function setSubscriptionBetas(headers: Headers, hasTools: boolean, hasThinking: boolean): void {
  const values = (headers.get("anthropic-beta") ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item && !CONDITIONAL_BETAS.has(item))
  values.push(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.oauthBeta, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.codeBeta)
  if (hasTools) values.push(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta)
  if (hasThinking) values.push(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta)
  headers.set("anthropic-beta", [...new Set(values)].join(","))
}

/** Transform one SDK-serialized request copy; caller-owned conversation data is untouched. */
export function profileClaudeSubscriptionRequest(
  body: string,
  inputHeaders: HeadersInit,
  context: ClaudeSubscriptionRequestContext,
): ClaudeSubscriptionRequestProfile {
  const parsed: unknown = JSON.parse(body)
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Claude subscription request body must be an object")
  }
  const original = record(parsed)!
  const system: unknown[] = Array.isArray(original.system)
    ? original.system.slice()
    : typeof original.system === "string"
      ? [{ type: "text", text: original.system }]
      : []

  if (isOwnedProfilePrefix(system)) system.splice(0, 2)
  const identityIndex = system.findIndex((item) => {
    const block = record(item)
    return block?.type === "text" && block.text === IDENTITY
  })
  const identity = identityIndex >= 0 ? system.splice(identityIndex, 1)[0] : { type: "text", text: IDENTITY }
  system.unshift({ type: "text", text: BILLING_TEXT }, identity)

  const metadata: Record<string, unknown> = original.metadata && typeof original.metadata === "object" && !Array.isArray(original.metadata)
    ? { ...original.metadata }
    : {}
  const userIdentity: Record<string, string> = {
    device_id: stableIdentifier("device ID", context.deviceID),
    session_id: stableIdentifier("session ID", context.sessionID),
  }
  if (context.accountUUID) userIdentity.account_uuid = stableIdentifier("account UUID", context.accountUUID)
  metadata.user_id = JSON.stringify(userIdentity)

  // Put the owned system prefix first so the checksum anchor is unambiguous,
  // independent of messages containing the same marker text.
  const { system: _system, ...rest } = original
  const request: Record<string, unknown> = { system, ...rest, metadata }
  const headers = new Headers(inputHeaders)
  headers.set("accept", "application/json")
  headers.set("content-type", "application/json")
  headers.set("anthropic-version", CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.anthropicVersion)
  headers.set("user-agent", CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.userAgent)
  headers.set("x-app", "cli")
  headers.set("anthropic-dangerous-direct-browser-access", "true")
  for (const [name, value] of Object.entries({
    "x-stainless-lang": "js",
    "x-stainless-runtime": stableIdentifier("runtime", context.runtime),
    "x-stainless-runtime-version": stableIdentifier("runtime version", context.runtimeVersion),
    "x-stainless-os": stableIdentifier("platform", context.platform),
    "x-stainless-arch": stableIdentifier("architecture", context.architecture),
    "x-stainless-package-version": CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.packageVersion,
    "x-stainless-retry-count": "0",
    "x-stainless-timeout": "600",
  })) headers.set(name, value)
  headers.delete("x-api-key")
  headers.delete("api-key")
  headers.delete("proxy-authorization")
  headers.delete("content-length")
  setSubscriptionBetas(
    headers,
    Array.isArray(request.tools) && request.tools.length > 0,
    Boolean(request.thinking) || typeof request.effort === "string",
  )

  return {
    profileId: CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.id,
    body: withChecksum(JSON.stringify(request)),
    headers,
  }
}

