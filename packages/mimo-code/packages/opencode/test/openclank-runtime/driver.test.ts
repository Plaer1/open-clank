import { describe, expect, test } from "bun:test"
import {
  OpenClankDriverSession,
  type DriverEnvelope,
} from "../../src/openclank-runtime/protocol"

const envelope = (command: DriverEnvelope["command"], payload: Record<string, unknown> = {}): DriverEnvelope => ({
  schema_version: 1,
  request_id: "a".repeat(32),
  owner_subject_id: "subject-1",
  session_id: "session-1",
  runtime_id: "runtime-1",
  runtime_epoch: "b".repeat(32),
  runtime_generation: 2,
  command,
  payload,
})

describe("Open Clank direct driver lifecycle shell", () => {
  test("requires hello, binds identity, and does not claim turn readiness", () => {
    const driver = new OpenClankDriverSession()
    expect(driver.handle(envelope("submit_turn")).error?.code).toBe("driver_not_initialized")
    const hello = driver.handle(envelope("hello", {
      protocol_major: 1,
      protocol_minor: 0,
      schema_sha256: "c".repeat(64),
      binding: "d".repeat(64),
    }))
    expect(hello.event).toBe("hello_ack")
    expect(hello.payload?.ready).toBe(false)
    expect(driver.handle(envelope("submit_turn")).error?.code).toBe("driver_not_activated")
  })

  test("rejects identity drift and closes idempotently", () => {
    const driver = new OpenClankDriverSession()
    driver.handle(envelope("hello", {
      protocol_major: 1,
      protocol_minor: 0,
      schema_sha256: "c".repeat(64),
      binding: "d".repeat(64),
    }))
    expect(driver.handle({ ...envelope("shutdown"), owner_subject_id: "other" }).error?.code).toBe("stale_runtime")
    expect(driver.handle(envelope("shutdown")).event).toBe("shutdown_ack")
    expect(driver.handle(envelope("shutdown")).error?.code).toBe("driver_closed")
  })

  test("requires the exact host activation binding before reporting callback readiness", () => {
    const driver = new OpenClankDriverSession()
    driver.handle(envelope("hello", {
      protocol_major: 1,
      protocol_minor: 0,
      schema_sha256: "c".repeat(64),
      binding: "d".repeat(64),
    }))
    const activation = {
      registration_id: "e".repeat(32),
      callback_endpoint: "/tmp/openclank-callback.sock",
      callback_nonce: "N".repeat(43),
      callback_binding_sha256: "f".repeat(64),
    }
    const ready = driver.handle(envelope("activate", activation))
    expect(ready.event).toBe("activated")
    expect(ready.payload?.callback_ready).toBe(true)
    expect(ready.payload?.ready).toBe(false)
    expect(driver.handle(envelope("submit_turn")).error?.code).toBe("driver_not_ready")
    expect(driver.handle(envelope("activate", activation)).event).toBe("activated")
    expect(driver.handle(envelope("activate", { ...activation, registration_id: "1".repeat(32) })).error?.code).toBe("activation_stale")
    const malformed = { ...activation, extra: true }
    const second = new OpenClankDriverSession()
    second.handle(envelope("hello", {
      protocol_major: 1,
      protocol_minor: 0,
      schema_sha256: "c".repeat(64),
      binding: "d".repeat(64),
    }))
    expect(second.handle(envelope("activate", malformed)).error?.code).toBe("activation_invalid")
  })
})
