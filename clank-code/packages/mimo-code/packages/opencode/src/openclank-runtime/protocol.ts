export const DRIVER_PROTOCOL_MAJOR = 1
export const MAX_DRIVER_ENVELOPE_BYTES = 256 * 1024

export type DriverCommand =
  | "hello"
  | "activate"
  | "open_session"
  | "restore_session"
  | "submit_turn"
  | "cancel_turn"
  | "set_model"
  | "set_mode"
  | "set_config"
  | "resolve_permission"
  | "resolve_question"
  | "provider_control"
  | "close"
  | "shutdown"

export type DriverEnvelope = {
  schema_version: 1
  request_id: string
  owner_subject_id: string
  session_id: string
  runtime_id: string
  runtime_epoch: string
  runtime_generation: number
  run_id?: string
  turn_id?: string
  command: DriverCommand
  payload: Record<string, unknown>
  deadline_unix_ms?: number
  idempotency_key?: string
}

export type DriverResponse = {
  schema_version: 1
  request_id: string
  ok: boolean
  event?: "hello_ack" | "activated" | "accepted" | "session_closed" | "shutdown_ack"
  payload?: Record<string, unknown>
  error?: {
    code: string
    safe_message: string
    retryable: boolean
  }
}

const commands = new Set<DriverCommand>([
  "hello",
  "activate",
  "open_session",
  "restore_session",
  "submit_turn",
  "cancel_turn",
  "set_model",
  "set_mode",
  "set_config",
  "resolve_permission",
  "resolve_question",
  "provider_control",
  "close",
  "shutdown",
])

const textFields = new Set([
  "request_id",
  "owner_subject_id",
  "session_id",
  "runtime_id",
  "runtime_epoch",
  "run_id",
  "turn_id",
  "command",
  "idempotency_key",
])

const allowedFields = new Set([
  "schema_version",
  ...textFields,
  "runtime_generation",
  "payload",
  "deadline_unix_ms",
])

function assertHex(value: unknown, field: string) {
  if (typeof value !== "string" || !/^[0-9a-f]{32}$/.test(value)) {
    throw new Error(`invalid ${field}`)
  }
}

function assertText(value: unknown, field: string, required = true) {
  if (value === undefined && !required) return
  if (typeof value !== "string" || value.length === 0 || value.length > 128) {
    throw new Error(`invalid ${field}`)
  }
}

export function parseDriverEnvelope(value: unknown): DriverEnvelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("driver envelope must be an object")
  const raw = value as Record<string, unknown>
  if (Object.keys(raw).some((key) => !allowedFields.has(key))) throw new Error("unknown driver envelope field")
  if (raw.schema_version !== DRIVER_PROTOCOL_MAJOR) throw new Error("unsupported driver schema")
  assertHex(raw.request_id, "request_id")
  assertText(raw.owner_subject_id, "owner_subject_id")
  assertText(raw.session_id, "session_id")
  assertText(raw.runtime_id, "runtime_id")
  assertHex(raw.runtime_epoch, "runtime_epoch")
  assertText(raw.run_id, "run_id", false)
  assertText(raw.turn_id, "turn_id", false)
  if (raw.idempotency_key !== undefined) assertHex(raw.idempotency_key, "idempotency_key")
  if (typeof raw.runtime_generation !== "number" || !Number.isSafeInteger(raw.runtime_generation) || raw.runtime_generation < 0) {
    throw new Error("invalid runtime_generation")
  }
  if (raw.deadline_unix_ms !== undefined && (typeof raw.deadline_unix_ms !== "number" || !Number.isSafeInteger(raw.deadline_unix_ms) || raw.deadline_unix_ms < 0)) {
    throw new Error("invalid deadline_unix_ms")
  }
  if (typeof raw.command !== "string" || !commands.has(raw.command as DriverCommand)) throw new Error("unknown driver command")
  if (!raw.payload || typeof raw.payload !== "object" || Array.isArray(raw.payload)) throw new Error("driver payload must be an object")
  return raw as DriverEnvelope
}

export function encodeDriverEnvelope(value: DriverEnvelope): Uint8Array {
  const envelope = parseDriverEnvelope(value)
  const payload = new TextEncoder().encode(JSON.stringify(envelope))
  if (payload.length > MAX_DRIVER_ENVELOPE_BYTES) throw new Error("driver envelope too large")
  const output = new Uint8Array(4 + payload.length)
  new DataView(output.buffer).setUint32(0, payload.length, false)
  output.set(payload, 4)
  return output
}

export function decodeDriverFrame(frame: Uint8Array): DriverEnvelope {
  if (frame.length < 4) throw new Error("driver frame truncated")
  const length = new DataView(frame.buffer, frame.byteOffset, frame.byteLength).getUint32(0, false)
  if (length === 0 || length > MAX_DRIVER_ENVELOPE_BYTES) throw new Error("driver frame length invalid")
  if (frame.length !== length + 4) throw new Error("driver frame length mismatch")
  const value = JSON.parse(new TextDecoder().decode(frame.slice(4))) as unknown
  return parseDriverEnvelope(value)
}

export function encodeDriverResponse(value: DriverResponse): Uint8Array {
  if (!/^[0-9a-f]{32}$/.test(value.request_id)) throw new Error("invalid response request_id")
  if (value.ok && (!value.event || !value.payload || value.error)) throw new Error("invalid successful driver response")
  if (!value.ok && (!value.error || value.event || value.payload)) throw new Error("invalid driver error response")
  if (value.payload && Object.keys(value.payload).some((key) => ["credential", "password", "secret", "raw_path", "cwd"].includes(key))) {
    throw new Error("forbidden driver response field")
  }
  const payload = new TextEncoder().encode(JSON.stringify(value))
  if (payload.length > MAX_DRIVER_ENVELOPE_BYTES) throw new Error("driver response too large")
  const output = new Uint8Array(4 + payload.length)
  new DataView(output.buffer).setUint32(0, payload.length, false)
  output.set(payload, 4)
  return output
}

type DriverIdentity = Pick<DriverEnvelope, "owner_subject_id" | "session_id" | "runtime_id" | "runtime_epoch" | "runtime_generation">

const responseError = (request_id: string, code: string, safe_message: string, retryable: boolean): DriverResponse => ({
  schema_version: 1,
  request_id,
  ok: false,
  error: { code, safe_message, retryable },
})

// The protocol shell is intentionally not an agent engine.  Publishing an
// explicit all-false authority matrix is safer than an empty object: the Rust
// host can distinguish "unknown" from "known but unavailable" and must not
// infer Chat/Plan/Agent readiness from a successful hello.
const PROTOCOL_SHELL_CAPABILITIES = Object.freeze({
  semantic_transcript: false,
  typed_tools: false,
  native_tools_disabled: false,
  host_tool_broker: false,
  permissions: false,
  questions: false,
  plans: false,
  models: false,
  usage: false,
  subagents: false,
  resume: false,
  memory_digest: false,
  memory_tools: false,
  memory_capture: false,
  skills: false,
  pty: false,
  terminal_input: false,
  secret_lane: false,
  provider_control: false,
  managed_operations: false,
})

/**
 * The first executable driver is intentionally a protocol/authority shell.
 * It proves the child-side binding and lifecycle without pretending that the
 * retained engine can execute a host-authorized turn yet.
 */
export class OpenClankDriverSession {
  #identity?: DriverIdentity
  #activation?: { registration_id: string; binding_sha256: string }
  #sessions = new Set<string>()
  #closed = false

  handle(envelope: DriverEnvelope): DriverResponse {
    if (this.#closed) return responseError(envelope.request_id, "driver_closed", "driver is closed", false)
    if (!this.#identity) {
      if (envelope.command !== "hello") {
        return responseError(envelope.request_id, "driver_not_initialized", "driver hello is required", false)
      }
      const payload = envelope.payload
      if (payload.protocol_major !== DRIVER_PROTOCOL_MAJOR || payload.protocol_minor !== 0) {
        return responseError(envelope.request_id, "protocol_mismatch", "driver protocol is not supported", false)
      }
      if (typeof payload.schema_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(payload.schema_sha256)) {
        return responseError(envelope.request_id, "schema_mismatch", "driver schema identity is invalid", false)
      }
      if (typeof payload.binding !== "string" || !/^[0-9a-f]{64}$/.test(payload.binding)) {
        return responseError(envelope.request_id, "binding_invalid", "driver binding is invalid", false)
      }
      this.#identity = {
        owner_subject_id: envelope.owner_subject_id,
        session_id: envelope.session_id,
        runtime_id: envelope.runtime_id,
        runtime_epoch: envelope.runtime_epoch,
        runtime_generation: envelope.runtime_generation,
      }
      return {
        schema_version: 1,
        request_id: envelope.request_id,
        ok: true,
        event: "hello_ack",
        payload: {
          ready: false,
          reason: "host_activation_required",
          capabilities: PROTOCOL_SHELL_CAPABILITIES,
        },
      }
    }
    if (!sameIdentity(this.#identity, envelope)) {
      return responseError(envelope.request_id, "stale_runtime", "driver runtime identity is stale", false)
    }
    if (envelope.command === "open_session" || envelope.command === "restore_session") {
      if (envelope.session_id === this.#identity.session_id) {
        return responseError(envelope.request_id, "session_invalid", "driver session binding is invalid", false)
      }
      this.#sessions.add(envelope.session_id)
      return {
        schema_version: 1,
        request_id: envelope.request_id,
        ok: true,
        event: "accepted",
        payload: envelope.command === "restore_session"
          ? { session_restored: true }
          : { session_open: true },
      }
    }
    if (envelope.command === "submit_turn" && !this.#sessions.has(envelope.session_id)) {
      return responseError(envelope.request_id, "session_not_open", "driver session is not open", false)
    }
    if (envelope.command === "close") {
      if (!this.#sessions.delete(envelope.session_id)) {
        return responseError(envelope.request_id, "session_not_open", "driver session is not open", false)
      }
      return {
        schema_version: 1,
        request_id: envelope.request_id,
        ok: true,
        event: "session_closed",
        payload: { session_closed: true },
      }
    }
    if (envelope.command === "activate") {
      const activation = parseActivation(envelope.payload)
      if (!activation) return responseError(envelope.request_id, "activation_invalid", "driver activation is invalid", false)
      if (this.#activation && (this.#activation.registration_id !== activation.registration_id || this.#activation.binding_sha256 !== activation.binding_sha256)) {
        return responseError(envelope.request_id, "activation_stale", "driver activation is stale", false)
      }
      this.#activation = activation
      return {
        schema_version: 1,
        request_id: envelope.request_id,
        ok: true,
        event: "activated",
        payload: {
          callback_ready: true,
          ready: false,
          reason: "turn_execution_not_implemented",
          capabilities: PROTOCOL_SHELL_CAPABILITIES,
        },
      }
    }
    if (envelope.command === "shutdown") {
      this.#closed = true
      return {
        schema_version: 1,
        request_id: envelope.request_id,
        ok: true,
        event: "shutdown_ack",
        payload: { closed: true },
      }
    }
    return responseError(
      envelope.request_id,
      this.#activation ? "driver_not_ready" : "driver_not_activated",
      this.#activation ? "driver turn execution is not ready" : "host activation is required before turn traffic",
      true,
    )
  }
}

function parseActivation(payload: Record<string, unknown>): { registration_id: string; binding_sha256: string } | undefined {
  const fields = new Set(["registration_id", "callback_endpoint", "callback_nonce", "callback_binding_sha256"])
  if (Object.keys(payload).some((key) => !fields.has(key)) || Object.keys(payload).length !== fields.size) return undefined
  const registration_id = payload.registration_id
  const binding_sha256 = payload.callback_binding_sha256
  const endpoint = payload.callback_endpoint
  const nonce = payload.callback_nonce
  if (typeof registration_id !== "string" || !/^[0-9a-f]{32}$/.test(registration_id)) return undefined
  if (typeof binding_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(binding_sha256)) return undefined
  if (typeof endpoint !== "string" || endpoint.length === 0 || endpoint.length > 100 || endpoint.includes("\u0000")) return undefined
  if (typeof nonce !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(nonce)) return undefined
  return { registration_id, binding_sha256 }
}

function sameIdentity(left: DriverIdentity, right: DriverEnvelope): boolean {
  return left.owner_subject_id === right.owner_subject_id &&
    left.runtime_id === right.runtime_id &&
    left.runtime_epoch === right.runtime_epoch &&
    left.runtime_generation === right.runtime_generation
}
