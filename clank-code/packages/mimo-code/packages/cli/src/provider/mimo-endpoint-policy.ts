import type { Auth } from "@/auth"
import { XIAOMI_API_PROVIDER_ID, XIAOMI_CREDENTIAL_PROVIDER_IDS, xiaomiDefaultBaseUrl } from "./xiaomi-family"

export type MimoEndpointPolicy = "mimo-metered-api-v1" | "mimo-token-plan-v1"
export type MimoBillingLane = Extract<Auth.BillingLane, "metered_api" | "subscription">

export interface MimoEndpoint {
  readonly url: string
  readonly policy: MimoEndpointPolicy
}

// The gateway table is upstream's Xiaomi provider split (`xiaomi-family.ts`). This file only
// keeps what the Open Clank Host contract needs on top of it: the billing-lane check and the
// two `endpointPolicy` ids.
const DEFAULT_METERED: MimoEndpoint = Object.freeze({
  url: xiaomiDefaultBaseUrl(XIAOMI_API_PROVIDER_ID)!,
  policy: "mimo-metered-api-v1",
})

const TOKEN_PLAN_URLS = new Set(
  XIAOMI_CREDENTIAL_PROVIDER_IDS.filter((id) => id !== XIAOMI_API_PROVIDER_ID).map((id) => xiaomiDefaultBaseUrl(id)!),
)

/** Validate one explicit MiMo endpoint against the selected billing lane. */
export function parseMimoEndpoint(value: unknown, lane: MimoBillingLane): MimoEndpoint | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return undefined
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.search ||
    parsed.hash ||
    parsed.pathname.replace(/\/$/, "") !== "/v1"
  ) return undefined
  const url = `${parsed.origin}/v1`
  if (lane === "metered_api" && url === DEFAULT_METERED.url) return DEFAULT_METERED
  if (lane === "subscription" && TOKEN_PLAN_URLS.has(url)) {
    return { url, policy: "mimo-token-plan-v1" }
  }
  return undefined
}

/**
 * Resolve credential and connection endpoints together. Missing metered API
 * metadata retains the historical fixed default; subscription endpoints must
 * be explicit in the callback credential or selected connection.
 */
export function resolveMimoEndpoint(input: {
  readonly lane: MimoBillingLane
  readonly credentialURL?: unknown
  readonly connectionURL?: unknown
}): MimoEndpoint | undefined {
  const hasCredentialURL = input.credentialURL !== undefined && input.credentialURL !== null && input.credentialURL !== ""
  const hasConnectionURL = input.connectionURL !== undefined && input.connectionURL !== null && input.connectionURL !== ""
  const credential = hasCredentialURL ? parseMimoEndpoint(input.credentialURL, input.lane) : undefined
  const connection = hasConnectionURL ? parseMimoEndpoint(input.connectionURL, input.lane) : undefined
  if ((hasCredentialURL && !credential) || (hasConnectionURL && !connection)) return undefined
  if (credential && connection && credential.url !== connection.url) return undefined
  if (credential || connection) return credential ?? connection
  return input.lane === "metered_api" ? DEFAULT_METERED : undefined
}
