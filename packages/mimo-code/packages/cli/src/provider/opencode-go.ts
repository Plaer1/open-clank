import { effectiveBaseURL } from "./provider"
import type { Model } from "./provider"

/**
 * OpenCode Go (`https://opencode.ai/zen/go`) hard-rejects inference requests with
 * no stable per-conversation `x-opencode-session` (400 MissingSessionID) since
 * 2026-09-05. Only that gateway requires the header.
 *
 * Target detection is identity **or** the effective request base URL (same
 * precedence as `resolveSDK` / `Provider.effectiveBaseURL`): `opencode-go*`
 * provider ids, or any URL whose effective base contains `opencode.ai/zen/go`.
 * Do not add more field names here — fix `effectiveBaseURL` if the request URL
 * resolution changes.
 */
export function isOpenCodeGoTarget(
  model: Pick<Model, "providerID" | "api">,
  providerOptions?: Record<string, unknown> | undefined,
): boolean {
  const providerID = String(model.providerID ?? "")
  if (providerID.startsWith("opencode-go")) return true
  return (effectiveBaseURL(model, providerOptions) ?? "").includes("opencode.ai/zen/go")
}

/**
 * Headers for OpenCode Go only. Non-Go targets get `{}` so their outbound
 * requests stay byte-identical. Empty/missing `sessionID` adds nothing (the
 * gateway would 400 either way; inventing an ID here would mis-route cache).
 */
export function openCodeGoSessionHeaders(
  model: Pick<Model, "providerID" | "api">,
  sessionID: string | undefined,
  providerOptions?: Record<string, unknown> | undefined,
): Record<string, string> {
  if (!sessionID) return {}
  if (!isOpenCodeGoTarget(model, providerOptions)) return {}
  return { "x-opencode-session": sessionID }
}
