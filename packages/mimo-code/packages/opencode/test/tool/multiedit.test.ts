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

        yield* Effect.promise(() => Bun.write(file, "external\r\n"))
        const stale = yield* tool
          .execute(
            {
              file_path: file,
              expected_fingerprint: result.metadata.fingerprint,
              edits: [{ old_string: "ALPHA", new_string: "again" }],
            },
            withRead(file, result.metadata.fingerprint),
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(stale)).toBe(true)
        expect(yield* Effect.promise(() => Bun.file(file).text())).toBe("external\r\n")
      }),
    ),
  )
})
