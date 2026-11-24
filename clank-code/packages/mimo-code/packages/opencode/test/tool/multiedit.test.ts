import { describe, expect } from "bun:test"
import path from "node:path"
import { Effect, Exit, Layer } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Agent } from "../../src/agent/agent"
import { Bus } from "../../src/bus"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Format } from "../../src/format"
import type { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID, SessionID } from "../../src/session/schema"
import { MultiEditTool } from "../../src/tool/multiedit"
import { Truncate } from "../../src/tool"
import {
  bindMemorySessionClient,
  registerManagedMcpClient,
  unbindMemorySessionClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const it = testEffect(
  Layer.mergeAll(
    AppFileSystem.defaultLayer,
    Agent.defaultLayer,
    Bus.layer,
    CrossSpawnSpawner.defaultLayer,
    Format.defaultLayer,
    Truncate.defaultLayer,
  ),
)

const baseCtx = {
  sessionID: SessionID.make("ses_multiedit"),
  messageID: MessageID.make("msg_multiedit"),
  callID: "",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [] as MessageV2.WithParts[],
  metadata: () => Effect.void,
  ask: () => Effect.void,
}

function withRead(filePath: string, fingerprint: string) {
  const messageID = MessageID.make("msg_read")
  return {
    ...baseCtx,
    messages: [
      {
        info: {
          id: messageID,
          sessionID: baseCtx.sessionID,
          role: "assistant",
        },
        parts: [
          {
            id: PartID.make("part_read"),
            messageID,
            sessionID: baseCtx.sessionID,
            type: "tool",
            tool: "read",
            callID: "call_read",
            state: {
              status: "completed",
              input: { file_path: filePath },
              output: "",
              title: `Read ${filePath}`,
              metadata: { fingerprint },
              time: { start: 0, end: 0 },
            },
          },
        ],
      },
    ] as unknown as MessageV2.WithParts[],
  }
}

describe("tool.multiedit", () => {
  it.live("commits sequential edits once and rejects a stale fingerprint", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "note.txt")
        const original = Buffer.from("alpha\r\nbeta\r\nalpha\r\n")
        yield* Effect.promise(() => Bun.write(file, original))
        const fingerprint = AppFileSystem.fingerprintBytes(original)
        const info = yield* MultiEditTool
        const tool = yield* info.init()

        const result = yield* tool.execute(
          {
            file_path: file,
            expected_fingerprint: fingerprint,
            edits: [
              { old_string: "alpha", new_string: "ALPHA", replace_all: true },
              { old_string: "beta", new_string: "BETA" },
            ],
          },
          withRead(file, fingerprint),
        )

        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("ALPHA\r\nBETA\r\nALPHA\r\n")
        expect(result.metadata.old_fingerprint).toBe(fingerprint)
        expect(result.metadata.fingerprint).toStartWith("sha256:")
        // Lore history status is reported honestly: unconfigured/paused never
        // claims ExactBatch recoverability or a durable preimage.
        expect(result.metadata.history.durable).toBe(false)
        expect(result.metadata.history.coverage).not.toBe("ExactBatch")
        expect(["unconfigured", "paused", "failed"]).toContain(result.metadata.history.status ?? "unconfigured")

        yield* Effect.promise(() => Bun.write(file, "external\r\n"))
        const nextFingerprint = result.metadata.fingerprint ?? fingerprint
        const stale = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: nextFingerprint,
              edits: [{ old_string: "ALPHA", new_string: "again" }],
            },
            withRead(file, nextFingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(stale)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("external\r\n")
      }),
    ),
  )

  it.live("rejects a fingerprint mismatch and leaves the file untouched", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "fingerprint.txt")
        const original = Buffer.from("alpha\r\nbeta\r\n")
        yield* Effect.promise(() => Bun.write(file, original))
        const fingerprint = AppFileSystem.fingerprintBytes(original)
        const info = yield* MultiEditTool
        const tool = yield* info.init()

        const stale = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
              edits: [{ old_string: "alpha", new_string: "ALPHA" }],
            },
            withRead(file, fingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(stale)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("alpha\r\nbeta\r\n")
      }),
    ),
  )

  it.live("a failed multi-edit leaves no partial result (all-or-nothing)", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "atomic.txt")
        const original = Buffer.from("alpha\r\nbeta\r\ngamma\r\n")
        yield* Effect.promise(() => Bun.write(file, original))
        const fingerprint = AppFileSystem.fingerprintBytes(original)
        const info = yield* MultiEditTool
        const tool = yield* info.init()

        // Second edit's old_string is missing: the first staged edit must not commit.
        const failed = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: fingerprint,
              edits: [
                { old_string: "alpha", new_string: "ALPHA" },
                { old_string: "THIS_STRING_IS_NOT_IN_THE_FILE", new_string: "NOPE" },
              ],
            },
            withRead(file, fingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(failed)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("alpha\r\nbeta\r\ngamma\r\n")

        // A later commit-time fingerprint race also leaves the file untouched.
        yield* Effect.promise(() => Bun.write(file, "external\r\n"))
        const raced = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: fingerprint,
              edits: [{ old_string: "alpha", new_string: "ALPHA" }],
            },
            withRead(file, fingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(raced)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("external\r\n")
      }),
    ),
  )

  it.live("leaves the file untouched when shared project policy rejects the batch", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "policy.txt")
        const original = Buffer.from("alpha\r\nbeta\r\n")
        yield* Effect.promise(() => Bun.write(file, original))
        const fingerprint = AppFileSystem.fingerprintBytes(original)
        const info = yield* MultiEditTool
        const tool = yield* info.init()

        const client = {
          callTool: async () => ({
            content: [{ type: "text", text: JSON.stringify({ enforced: true, allowed: false, reason: "blocked by policy" }) }],
          }),
        } as any
        process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = "required"
        registerManagedMcpClient("lifetools_multiedit_policy_test", client)
        bindMemorySessionClient(baseCtx.sessionID, "lifetools_multiedit_policy_test", "alice", "global")
        yield* Effect.addFinalizer(() =>
          Effect.sync(() => {
            unbindMemorySessionClient(baseCtx.sessionID)
            unregisterManagedMcpClient("lifetools_multiedit_policy_test", client)
            delete process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
          }),
        )

        const exit = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: fingerprint,
              edits: [{ old_string: "alpha", new_string: "ALPHA" }],
            },
            withRead(file, fingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(exit)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("alpha\r\nbeta\r\n")
      }),
    ),
  )
})
