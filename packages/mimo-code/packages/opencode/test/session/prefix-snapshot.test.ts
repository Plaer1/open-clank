import { describe, expect, test } from "bun:test"
import { jsonSchema, tool } from "ai"
import { asSchema } from "@ai-sdk/provider-utils"
import { Instance } from "../../src/project/instance"
import { Session as SessionNs } from "../../src/session"
import { SessionPrefixSnapshot } from "../../src/session/prefix-snapshot"
import { MessageID } from "../../src/session/schema"
import { AppRuntime } from "../../src/effect/app-runtime"
import { tmpdir } from "../fixture/fixture"

describe("session prefix snapshot", () => {
  test("pins, rotates, advances, and cascades with its session", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.create({})))
        const key = SessionPrefixSnapshot.profileKey({
          providerID: "test",
          modelID: "test-model",
          agent: "build",
          agentID: "main",
          harness: "auto",
          systemMode: "append",
          system: "",
          permission: [],
        })
        const firstWatermark = MessageID.ascending()
        const first = await AppRuntime.runPromise(
          SessionPrefixSnapshot.pin({
            sessionID: session.id,
            profileKey: key,
            system: ["first"],
            toolsHash: "tools-1",
            tools: [],
            watermarkMessageID: firstWatermark,
          }),
        )
        expect(first).toMatchObject({
          revision: 1,
          system: ["first"],
          tools_hash: "tools-1",
          watermark_message_id: firstWatermark,
        })

        const pinned = await AppRuntime.runPromise(
          SessionPrefixSnapshot.pin({
            sessionID: session.id,
            profileKey: key,
            system: ["ignored"],
            toolsHash: "ignored",
            tools: [],
            watermarkMessageID: MessageID.ascending(),
          }),
        )
        expect(pinned).toEqual(first)

        const rotated = await AppRuntime.runPromise(
          SessionPrefixSnapshot.rotate({
            sessionID: session.id,
            profileKey: key,
            system: ["second"],
            toolsHash: "tools-2",
            tools: [],
            watermarkMessageID: firstWatermark,
          }),
        )
        expect(rotated).toMatchObject({ revision: 2, system: ["second"], tools_hash: "tools-2" })

        const finalWatermark = MessageID.ascending()
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.advance({
            sessionID: session.id,
            profileKey: key,
            revision: 1,
            watermarkMessageID: MessageID.ascending(),
          }),
        )
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.advance({
            sessionID: session.id,
            profileKey: key,
            revision: 2,
            watermarkMessageID: finalWatermark,
          }),
        )
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.advance({
            sessionID: session.id,
            profileKey: key,
            revision: 2,
            watermarkMessageID: firstWatermark,
          }),
        )
        expect(await AppRuntime.runPromise(SessionPrefixSnapshot.get(session.id, key))).toMatchObject({
          revision: 2,
          watermark_message_id: finalWatermark,
        })

        await AppRuntime.runPromise(SessionNs.Service.use((service) => service.remove(session.id)))
        expect(await AppRuntime.runPromise(SessionPrefixSnapshot.get(session.id, key))).toBeUndefined()
      },
    })
  })

  test("profile and tool hashes are stable across key order", () => {
    const permission = [{ permission: "*", pattern: "*", action: "allow" as const }]
    const key = SessionPrefixSnapshot.profileKey({
      providerID: "p",
      modelID: "m",
      agent: "build",
      agentID: "main",
      harness: "auto",
      systemMode: "append",
      system: "",
      permission,
    })
    expect(key).toBe(
      SessionPrefixSnapshot.profileKey({
        permission,
        systemMode: "append",
        system: "",
        harness: "auto",
        agentID: "main",
        agent: "build",
        modelID: "m",
        providerID: "p",
      }),
    )
    expect(key).not.toBe(
      SessionPrefixSnapshot.profileKey({
        providerID: "p",
        modelID: "other",
        agent: "build",
        agentID: "main",
        harness: "auto",
        systemMode: "append",
        system: "",
        permission,
      }),
    )
    expect(key).not.toBe(
      SessionPrefixSnapshot.profileKey({
        providerID: "p",
        modelID: "m",
        agent: "build",
        agentID: "main",
        harness: "auto",
        systemMode: "append",
        system: "",
        format: { type: "json_schema", schema: { type: "object" } },
        permission,
      }),
    )
    const first = {
      beta: tool({ description: "b", inputSchema: jsonSchema({ type: "object", properties: {} }) }),
      alpha: tool({ description: "a", inputSchema: jsonSchema({ type: "object", properties: {} }) }),
    }
    const second = { alpha: first.alpha, beta: first.beta }
    expect(SessionPrefixSnapshot.toolsHash(first, ["beta", "alpha"])).toBe(
      SessionPrefixSnapshot.toolsHash(second, ["alpha", "beta"]),
    )
  })

  test("a snapshot is current only when both the system and tools match", () => {
    const frozen = {
      system_hash: SessionPrefixSnapshot.systemHash(["frozen system"]),
      tools_hash: "frozen-tools",
    }

    expect(SessionPrefixSnapshot.isCurrent(frozen, ["frozen system"], "frozen-tools")).toBe(true)
    expect(SessionPrefixSnapshot.isCurrent(frozen, ["changed system"], "frozen-tools")).toBe(false)
    expect(SessionPrefixSnapshot.isCurrent(frozen, ["frozen system"], "changed-tools")).toBe(false)
  })

  test("snapshot tools restore the frozen advertised schemas without executors", async () => {
    const source = {
      read: tool({
        description: "Read a file",
        inputSchema: jsonSchema({
          type: "object",
          properties: { path: { type: "string" } },
          required: ["path"],
        }),
        execute: async () => "live result",
      }),
    }
    const snapshots = await SessionPrefixSnapshot.snapshotTools(source, ["read"])
    const restored = SessionPrefixSnapshot.restoreTools(snapshots)

    expect(Object.keys(restored)).toEqual(["read"])
    expect(restored.read?.description).toBe("Read a file")
    expect(restored.read?.execute).toBeUndefined()
    expect(await Promise.resolve(asSchema(restored.read!.inputSchema).jsonSchema)).toEqual({
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
    })
  })

  test("concurrent rotations use the revision compare-and-swap rather than losing an update", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.create({})))
        const key = SessionPrefixSnapshot.profileKey({
          providerID: "test",
          modelID: "test-model",
          agent: "build",
          agentID: "main",
          harness: "default",
          systemMode: "append",
          system: "",
          permission: [],
        })
        const watermark = MessageID.ascending()
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.pin({
            sessionID: session.id,
            profileKey: key,
            system: ["initial"],
            toolsHash: "initial-tools",
            tools: [],
            watermarkMessageID: watermark,
          }),
        )

        const [left, right] = await Promise.all([
          AppRuntime.runPromise(
            SessionPrefixSnapshot.rotate({
              sessionID: session.id,
              profileKey: key,
              system: ["left"],
              toolsHash: "left-tools",
              tools: [],
              watermarkMessageID: watermark,
            }),
          ),
          AppRuntime.runPromise(
            SessionPrefixSnapshot.rotate({
              sessionID: session.id,
              profileKey: key,
              system: ["right"],
              toolsHash: "right-tools",
              tools: [],
              watermarkMessageID: watermark,
            }),
          ),
        ])

        // Both rotations ran. One may return before the second retry finishes,
        // but the durable row has advanced once per successful CAS instead of
        // two stale writers both setting revision 2.
        expect(new Set([left.revision, right.revision])).toEqual(new Set([2, 3]))
        const final = await AppRuntime.runPromise(SessionPrefixSnapshot.get(session.id, key))
        if (!final) throw new Error("expected final prefix snapshot")
        expect(final.revision).toBe(3)
        expect(["left-tools", "right-tools"]).toContain(final.tools_hash)
      },
    })
  })

  test("a pinned prefix survives an instance restart and an old revision cannot advance its watermark", async () => {
    await using tmp = await tmpdir({ git: true })
    let sessionID: string
    const key = SessionPrefixSnapshot.profileKey({
      providerID: "test",
      modelID: "test-model",
      agent: "build",
      agentID: "main",
      harness: "default",
      systemMode: "append",
      system: "",
      permission: [],
    })
    const initialWatermark = MessageID.ascending()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.create({})))
        sessionID = session.id
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.pin({
            sessionID: session.id,
            profileKey: key,
            system: ["frozen system"],
            toolsHash: "frozen-tools",
            tools: [],
            watermarkMessageID: initialWatermark,
          }),
        )
      },
    })

    await Instance.disposeAll()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const persisted = await AppRuntime.runPromise(SessionPrefixSnapshot.get(sessionID as any, key))
        expect(persisted).toMatchObject({
          revision: 1,
          system: ["frozen system"],
          tools_hash: "frozen-tools",
          watermark_message_id: initialWatermark,
        })
        const newWatermark = MessageID.ascending()
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.advance({
            sessionID: sessionID as any,
            profileKey: key,
            revision: 0,
            watermarkMessageID: newWatermark,
          }),
        )
        expect(await AppRuntime.runPromise(SessionPrefixSnapshot.get(sessionID as any, key))).toMatchObject({
          watermark_message_id: initialWatermark,
        })
      },
    })
  })

  test("a rotated tool advertisement survives restart for a frozen checkpoint prefix", async () => {
    await using tmp = await tmpdir({ git: true })
    let sessionID: string
    const key = SessionPrefixSnapshot.profileKey({
      providerID: "test",
      modelID: "test-model",
      agent: "build",
      agentID: "main",
      harness: "default",
      systemMode: "append",
      system: "",
      permission: [],
    })
    const watermark = MessageID.ascending()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.create({})))
        sessionID = session.id
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.pin({
            sessionID: session.id,
            profileKey: key,
            system: ["old system"],
            toolsHash: "old-tools",
            tools: [{ name: "old_tool", description: "old", input_schema: { type: "object" } }],
            watermarkMessageID: watermark,
          }),
        )
        await AppRuntime.runPromise(
          SessionPrefixSnapshot.rotate({
            sessionID: session.id,
            profileKey: key,
            system: ["rotated system"],
            toolsHash: "rotated-tools",
            tools: [{ name: "rotated_tool", description: "rotated", input_schema: { type: "object" } }],
            watermarkMessageID: watermark,
          }),
        )
      },
    })

    await Instance.disposeAll()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const frozen = await AppRuntime.runPromise(SessionPrefixSnapshot.get(sessionID as any, key))
        expect(frozen).toMatchObject({
          revision: 2,
          system: ["rotated system"],
          tools_hash: "rotated-tools",
          tools: [{ name: "rotated_tool", description: "rotated" }],
        })
        const restored = SessionPrefixSnapshot.restoreTools(frozen!.tools ?? [])
        expect(Object.keys(restored)).toEqual(["rotated_tool"])
        expect(restored.rotated_tool?.execute).toBeUndefined()
      },
    })
  })
})
