import { describe, expect, test } from "bun:test"
import {
  containsForbiddenCredentialField,
  encodeCredentialCallbackRequest,
  parseCredentialCallbackRequest,
  parseCredentialCallbackResponse,
} from "../../src/openclank-runtime/credential-callback"

const request = () => encodeCredentialCallbackRequest({
  schema_version: 1,
  request_id: "a".repeat(32),
  lease_id: "lease-1",
  owner_subject_id: "subject-1",
  runtime_id: "runtime-1",
  runtime_epoch: "b".repeat(32),
  runtime_generation: 2,
  driver_pid: 42,
  driver_start_token: "start-1",
  operation: "provider.http",
})

describe("Open Clank one-shot credential callback contract", () => {
  test("hashes and round-trips the exact request binding", () => {
    const value = request()
    expect(parseCredentialCallbackRequest(value)).toEqual(value)
    expect(() => parseCredentialCallbackRequest({ ...value, operation: "provider.other" })).toThrow("mismatch")
  })

  test("accepts metadata-only success/error and rejects forbidden fields", () => {
    const value = request()
    expect(parseCredentialCallbackResponse({
      schema_version: 1,
      request_id: value.request_id,
      lease_id: value.lease_id,
      ok: true,
      credential_encoding: "canonical-json-utf8-base64url-nopad",
      credential_json_base64url: "e30",
      expires_unix_ms: 1,
      uses_remaining: 1,
      request_sha256: value.request_sha256,
    }).ok).toBe(true)
    expect(parseCredentialCallbackResponse({
      schema_version: 1,
      request_id: value.request_id,
      lease_id: value.lease_id,
      ok: false,
      error: { code: "credential_expired", safe_message: "expired", retryable: false },
      request_sha256: value.request_sha256,
    }).ok).toBe(false)
    expect(containsForbiddenCredentialField({ nested: [{ credential: "secret" }] })).toBe(true)
  })
})
