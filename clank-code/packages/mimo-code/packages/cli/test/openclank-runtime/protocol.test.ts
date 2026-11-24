import { describe, expect, test } from "bun:test"
import {
  decodeDriverFrame,
  encodeDriverEnvelope,
  encodeDriverResponse,
  OpenClankDriverSession,
  parseDriverEnvelope,
  type DriverEnvelope,
} from "../../src/openclank-runtime/protocol"

const envelope = (command: DriverEnvelope["command"]): DriverEnvelope => ({
  schema_version: 1,
  request_id: "a".repeat(32),
  owner_subject_id: "subject-1",
  session_id: "session-1",
  runtime_id: "runtime-1",
  runtime_epoch: "b".repeat(32),
  runtime_generation: 2,
  command,
  payload: { text: "hello" },
})

describe("Open Clank direct driver wire", () => {
  test("round trips an owner-bound turn envelope", () => {
    const input = envelope("submit_turn")
    expect(decodeDriverFrame(encodeDriverEnvelope(input))).toEqual(input)
  })

  test("rejects credentials, unknown commands, and bad identities", () => {
    expect(() => parseDriverEnvelope({ ...envelope("hello"), credential: "secret" })).toThrow("unknown")
    expect(() => parseDriverEnvelope({ ...envelope("hello"), command: "screen_scrape" })).toThrow("unknown driver command")
    expect(() => parseDriverEnvelope({ ...envelope("hello"), runtime_epoch: "BAD" })).toThrow("runtime_epoch")
  })

  test("rejects oversized and truncated frames", () => {
    expect(() => decodeDriverFrame(new Uint8Array([0, 0, 0]))).toThrow("truncated")
    expect(() => decodeDriverFrame(new Uint8Array([0, 0, 4, 0]))).toThrow("length mismatch")
  })

  test("encodes typed responses without allowing secret fields", () => {
    const frame = encodeDriverResponse({
      schema_version: 1,
      request_id: "a".repeat(32),
      ok: false,
      error: { code: "driver_not_activated", safe_message: "not ready", retryable: true },
    })
    expect(new DataView(frame.buffer).getUint32(0)).toBe(frame.length - 4)
    expect(() => encodeDriverResponse({
      schema_version: 1,
      request_id: "a".repeat(32),
      ok: true,
      event: "hello_ack",
      payload: { credential: "forbidden" },
    })).toThrow("forbidden")
  })

  test("binds turns to an explicitly opened session on a shared runtime", () => {
    const session = new OpenClankDriverSession()
    const hello = session.handle({
      ...envelope("hello"),
      session_id: "owner-runtime",
      payload: {
        protocol_major: 1,
        protocol_minor: 0,
        schema_sha256: "c".repeat(64),
        binding: "d".repeat(64),
      },
    })
    expect(hello.ok).toBe(true)
    expect((hello.payload?.capabilities as Record<string, unknown>)?.semantic_transcript).toBe(false)
    expect((hello.payload?.capabilities as Record<string, unknown>)?.native_tools_disabled).toBe(false)

    const unopened = session.handle(envelope("submit_turn"))
    expect(unopened.error?.code).toBe("session_not_open")

    const opened = session.handle({ ...envelope("open_session"), payload: { workspace_id: "workspace-1" } })
    expect(opened.ok).toBe(true)
    expect(opened.event).toBe("accepted")

    const restored = session.handle({
      ...envelope("restore_session"),
      session_id: "session-2",
      payload: { snapshot_sha256: "c".repeat(64), transcript_revision: 3 },
    })
    expect(restored.ok).toBe(true)
    expect(restored.payload?.session_restored).toBe(true)

    const accepted = session.handle(envelope("submit_turn"))
    expect(accepted.error?.code).toBe("driver_not_activated")

    const closed = session.handle(envelope("close"))
    expect(closed.ok).toBe(true)
    expect(closed.event).toBe("session_closed")
    expect(closed.payload?.session_closed).toBe(true)
    expect(session.handle(envelope("submit_turn")).error?.code).toBe("session_not_open")
  })
})
