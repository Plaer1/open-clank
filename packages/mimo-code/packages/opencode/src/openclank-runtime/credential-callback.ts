import { createHash } from "node:crypto"

export type CredentialCallbackRequest = {
  schema_version: 1
  request_id: string
  lease_id: string
  owner_subject_id: string
  runtime_id: string
  runtime_epoch: string
  runtime_generation: number
  driver_pid: number
  driver_start_token: string
  operation: string
  request_sha256: string
}

export type CredentialCallbackResponse =
  | {
      schema_version: 1
      request_id: string
      lease_id: string
      ok: true
      credential_encoding: "canonical-json-utf8-base64url-nopad"
      credential_json_base64url: string
      expires_unix_ms: number
      uses_remaining: 1
      request_sha256: string
    }
  | {
      schema_version: 1
      request_id: string
      lease_id: string
      ok: false
      error: { code: string; safe_message: string; retryable: boolean }
      request_sha256: string
    }

const HEX32 = /^[0-9a-f]{32}$/
const HEX64 = /^[0-9a-f]{64}$/
const OPERATION = /^[a-z0-9_.:-]{1,128}$/
const FORBIDDEN = new Set(["credential", "password", "secret", "raw_path", "cwd"])

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonical(object[key])}`).join(",")}}`
  }
  return JSON.stringify(value)
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex")
}

export function unsignedCredentialCallbackRequest(request: Omit<CredentialCallbackRequest, "request_sha256">): Record<string, unknown> {
  const { schema_version, ...rest } = request
  return { schema_version, ...rest }
}

export function credentialCallbackRequestHash(request: Omit<CredentialCallbackRequest, "request_sha256">): string {
  return sha256(canonical(unsignedCredentialCallbackRequest(request)))
}

export function encodeCredentialCallbackRequest(request: Omit<CredentialCallbackRequest, "request_sha256">): CredentialCallbackRequest {
  if (request.schema_version !== 1 || !HEX32.test(request.request_id) || !HEX32.test(request.runtime_epoch) || !OPERATION.test(request.operation)) {
    throw new Error("malformed credential callback request")
  }
  if (!Number.isSafeInteger(request.runtime_generation) || request.runtime_generation < 0 || !Number.isSafeInteger(request.driver_pid) || request.driver_pid < 1) {
    throw new Error("malformed credential callback request")
  }
  if (![request.lease_id, request.owner_subject_id, request.runtime_id, request.driver_start_token].every((value) => typeof value === "string" && value.length > 0 && value.length <= 128)) {
    throw new Error("malformed credential callback request")
  }
  return { ...request, request_sha256: credentialCallbackRequestHash(request) }
}

export function parseCredentialCallbackRequest(value: unknown): CredentialCallbackRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("malformed credential callback request")
  const object = value as Record<string, unknown>
  const allowed = new Set([
    "schema_version", "request_id", "lease_id", "owner_subject_id", "runtime_id", "runtime_epoch",
    "runtime_generation", "driver_pid", "driver_start_token", "operation", "request_sha256",
  ])
  if (Object.keys(object).some((key) => !allowed.has(key))) throw new Error("unknown credential callback field")
  if (typeof object.request_sha256 !== "string" || !HEX64.test(object.request_sha256)) throw new Error("invalid credential request hash")
  const request = encodeCredentialCallbackRequest({
    schema_version: object.schema_version as 1,
    request_id: object.request_id as string,
    lease_id: object.lease_id as string,
    owner_subject_id: object.owner_subject_id as string,
    runtime_id: object.runtime_id as string,
    runtime_epoch: object.runtime_epoch as string,
    runtime_generation: object.runtime_generation as number,
    driver_pid: object.driver_pid as number,
    driver_start_token: object.driver_start_token as string,
    operation: object.operation as string,
  })
  if (request.request_sha256 !== object.request_sha256) throw new Error("credential callback request hash mismatch")
  return request
}

export function parseCredentialCallbackResponse(value: unknown): CredentialCallbackResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("malformed credential callback response")
  const object = value as Record<string, unknown>
  if (object.schema_version !== 1 || typeof object.request_id !== "string" || !HEX32.test(object.request_id) || typeof object.lease_id !== "string" || object.lease_id.length === 0 || object.lease_id.length > 128 || typeof object.request_sha256 !== "string" || !HEX64.test(object.request_sha256)) {
    throw new Error("malformed credential callback response")
  }
  if (object.ok === true) {
    const allowed = new Set(["schema_version", "request_id", "lease_id", "ok", "credential_encoding", "credential_json_base64url", "expires_unix_ms", "uses_remaining", "request_sha256"])
    if (Object.keys(object).some((key) => !allowed.has(key)) || object.credential_encoding !== "canonical-json-utf8-base64url-nopad" || typeof object.credential_json_base64url !== "string" || object.credential_json_base64url.length > 64000 || !/^[A-Za-z0-9_-]+$/.test(object.credential_json_base64url) || object.uses_remaining !== 1 || typeof object.expires_unix_ms !== "number" || !Number.isSafeInteger(object.expires_unix_ms) || object.expires_unix_ms < 0) {
      throw new Error("malformed credential callback success")
    }
    return object as unknown as CredentialCallbackResponse
  }
  const allowed = new Set(["schema_version", "request_id", "lease_id", "ok", "error", "request_sha256"])
  if (Object.keys(object).some((key) => !allowed.has(key)) || object.ok !== false || !object.error || typeof object.error !== "object" || Array.isArray(object.error)) throw new Error("malformed credential callback error")
  const error = object.error as Record<string, unknown>
  if (Object.keys(error).some((key) => !["code", "safe_message", "retryable"].includes(key)) || typeof error.code !== "string" || !/^[a-z0-9_]{1,64}$/.test(error.code) || typeof error.safe_message !== "string" || error.safe_message.length === 0 || error.safe_message.length > 256 || typeof error.retryable !== "boolean") throw new Error("malformed credential callback error")
  return object as unknown as CredentialCallbackResponse
}

export function containsForbiddenCredentialField(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(containsForbiddenCredentialField)
  if (!value || typeof value !== "object") return false
  return Object.entries(value as Record<string, unknown>).some(([key, child]) => FORBIDDEN.has(key) || containsForbiddenCredentialField(child))
}
