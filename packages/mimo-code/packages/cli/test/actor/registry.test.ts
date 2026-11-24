import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry, sweepAbandonedZombies } from "../../src/actor/registry"
import { ActorRegistryTable } from "../../src/actor/actor.sql"
import { SessionID, MessageID } from "../../src/session/schema"
import { Database, eq, and } from "../../src/storage"
import { Log } from "../../src/util"
import { tmpdir } from "../fixture/fixture"
import { encodeForkContext } from "../../src/actor/fork-context"
import type { ForkContext } from "../../src/actor/fork-context"
import { ProviderID, ModelID } from "../../src/provider/schema"

void Log.init({ print: false })

// Combined layer for tests: Session + ActorRegistry both using the same Bus/DB
const testLayer = Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer)

afterEach(async () => {
  await Instance.disposeAll()
})

// Database.Client is a process-level singleton so rows survive across tmpdirs.
// orphan recovery only touches rows whose instance_id differs, and same-process
// tests share the same PROCESS_INSTANCE_ID — so wipe leftover rows before each test.
beforeEach(() => {
  Database.use((db) => db.delete(ActorRegistryTable).run())
})

/**
 * Run test body with a single runtime instance per `Instance.provide` scope.
 * This ensures orphan recovery and the stuck-detection fiber are started only
 * once, and all reads/writes share the same layer state.
 */
async function withRegistry(directory: string, fn: (rt: ManagedRuntime.ManagedRuntime<Session.Service | ActorRegistry.Service, never>) => Promise<void>) {
  return Instance.provide({
    directory,
    fn: async () => {
      const rt = ManagedRuntime.make(testLayer)
      try {
        await fn(rt)
      } finally {
        await rt.dispose()
      }
    },
  })
}

describe("ActorRegistry", () => {
  test("beginExecution binds a route preflight to the exact registry revision", async () => {
    await using tmp = await tmpdir({ git: true })
    await withRegistry(tmp.path, async (rt) => {
      const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
      const actor = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.register({
        sessionID: parent.id,
        actorID: "cas-actor",
        mode: "subagent",
        agent: "explore",
        description: "CAS fixture",
        contextMode: "full",
        background: false,
        lifecycle: "persistent",
      })))
      const stale = await rt.runPromise(ActorRegistry.Service.use((svc) =>
        svc.beginExecution(parent.id, actor.actorID, "pending", 1),
      ))
      expect(stale).toBeUndefined()
      const unchanged = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, actor.actorID)))
      expect(unchanged?.executionRevision).toBe(0)
      const started = await rt.runPromise(ActorRegistry.Service.use((svc) =>
        svc.beginExecution(parent.id, actor.actorID, "pending", 0),
      ))
      expect(started).toBe(1)
    })
  })

  test("stores full-context snapshots durably and retains them after actor settlement", async () => {
    await using tmp = await tmpdir({ git: true })
    await withRegistry(tmp.path, async (rt) => {
      const sessions = rt.runPromise(Session.Service.use((svc) => svc.create()))
      const parent = await sessions
      const child = await rt.runPromise(Session.Service.use((svc) => svc.create({ parentID: parent.id })))
      const model = { providerID: ProviderID.make("test-provider"), modelID: ModelID.make("test-model") }
      const hostContext = {
        chatID: "chat-fork", workspaceID: "workspace-fork", cwd: tmp.path,
        accountID: "source-account", grantID: "source-grant", grantRevision: 2, credentialRevision: 4,
      }
      const registry = await rt.runPromise(ActorRegistry.Service.use((svc) => Effect.succeed(svc)))
      await rt.runPromise(registry.updateModel(parent.id, "main", undefined, model))
      await rt.runPromise(registry.updateHostContext(parent.id, "main", hostContext))
      const context = {
        system: ["frozen prefix"],
        tools: {},
        parentPermission: [{ permission: "read", pattern: "*", action: "allow" }],
        inheritedMessages: [{ role: "user", content: "captured message" }],
        watermarkMsgID: MessageID.ascending(),
        model,
      } satisfies ForkContext
      const encoded = await encodeForkContext(context, { sessionID: parent.id, actorID: "main" })
      const childActorID = SessionID.descending()
      await rt.runPromise(registry.register({
        sessionID: child.id,
        actorID: childActorID,
        mode: "subagent",
        parentActorID: "main",
        agent: "explore",
        description: "durable fork fixture",
        contextMode: "full",
        contextWatermark: context.watermarkMsgID,
        background: true,
        lifecycle: "persistent",
        effectiveModel: model,
        hostContext,
        forkContext: encoded,
      }))

      const stored = await rt.runPromise(registry.getForkContext(child.id, childActorID))
      expect(stored?.snapshotSHA256).toBe(encoded.snapshotSHA256)
      await rt.runPromise(registry.updateHostContext(parent.id, "main", {
        ...hostContext, accountID: "refreshed-source-account", grantRevision: 3, credentialRevision: 5,
      }))
      expect((await rt.runPromise(registry.getForkContext(child.id, childActorID)))?.snapshotSHA256)
        .toBe(encoded.snapshotSHA256)
      await rt.runPromise(registry.updateStatus(child.id, childActorID, { status: "idle", lastOutcome: "success" }))
      const afterSettlement = await rt.runPromise(registry.getForkContext(child.id, childActorID))
      expect(afterSettlement?.snapshotJSON).toBe(encoded.snapshotJSON)
      const row = Database.use((db) => db.select().from(ActorRegistryTable).where(and(
        eq(ActorRegistryTable.session_id, child.id), eq(ActorRegistryTable.actor_id, childActorID),
      )).get())
      expect(row?.status).toBe("idle")
    })
  })
  describe("register", () => {
    test("registers a new task and returns a Actor", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))

        const taskId = SessionID.descending()
        const entry = await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Research authentication",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        expect(entry.actorID).toBe(taskId)
        expect(entry.sessionID).toBe(parent.id)
        expect(entry.status).toBe("pending")
        expect(entry.agent).toBe("explore")
        expect(entry.description).toBe("Research authentication")
        expect(entry.contextMode).toBe("none")
        expect(entry.background).toBe(false)
        expect(entry.turnCount).toBe(0)
        expect(typeof entry.time.created).toBe("number")
        expect(typeof entry.time.updated).toBe("number")
        expect(entry.time.completed).toBeUndefined()
      })
    })

    test("registers a background task", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        const entry = await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "build",
              description: "Build project",
              contextMode: "state",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )

        expect(entry.background).toBe(true)
        expect(entry.contextMode).toBe("state")
      })
    })

    test("round-trips host context and preserves it through terminal status", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        const hostContext = {
          accountID: "host-account-1",
          grantID: "grant-1",
          grantRevision: 4,
          credentialRevision: 8,
          chatID: "chat-1",
          workspaceID: "workspace-1",
          cwd: "/tmp/chat-1",
          goalID: "goal-1",
        }
        const entry = await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Host context test",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
              requestedModel: "requested-model",
              effectiveModel: { providerID: "mimo", modelID: "gpt-5.6-luna" },
              hostContext,
            }),
          ),
        )

        expect(entry.hostContext).toEqual(hostContext)
        expect(entry.requestedModel).toBe("requested-model")
        expect(entry.effectiveModel).toEqual({ providerID: "mimo", modelID: "gpt-5.6-luna" })
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.updateStatus(parent.id, taskId, { status: "idle", lastOutcome: "success" }),
          ),
        )
        const terminal = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(terminal?.hostContext).toEqual(hostContext)
      })
    })
  })

  describe("get", () => {
    test("retrieves a registered task by id", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Test task",
              contextMode: "full",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        const found = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(found).toBeDefined()
        expect(found!.actorID).toBe(taskId)
        expect(found!.description).toBe("Test task")
      })
    })

    test("returns undefined for unknown id", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const unknown = SessionID.descending()
        const result = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, unknown)))
        expect(result).toBeUndefined()
      })
    })
  })

  describe("updateStatus", () => {
    test("updates task status to running", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Status test",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        await rt.runPromise(ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, taskId, { status: "running" })))

        const updated = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(updated!.status).toBe("running")
        expect(updated!.time.completed).toBeUndefined()
      })
    })

    test("sets completed time for terminal status (idle + success outcome)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Terminal status test",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, taskId, { status: "idle", lastOutcome: "success" })),
        )

        const updated = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(updated!.status).toBe("idle")
        expect(updated!.lastOutcome).toBe("success")
        expect(typeof updated!.time.completed).toBe("number")
      })
    })

    test("stores error message for failed status (idle + failure outcome)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Error test",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.updateStatus(parent.id, taskId, { status: "idle", lastOutcome: "failure", lastError: "something went wrong" }),
          ),
        )

        const updated = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(updated!.status).toBe("idle")
        expect(updated!.lastOutcome).toBe("failure")
        expect(updated!.lastError).toBe("something went wrong")
        expect(typeof updated!.time.completed).toBe("number")
      })
    })
  })

  describe("updateTurn", () => {
    test("increments turn count and updates last_turn_time", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Turn count test",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        const before = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(before!.turnCount).toBe(0)

        await rt.runPromise(ActorRegistry.Service.use((svc) => svc.updateTurn(parent.id, taskId)))
        await rt.runPromise(ActorRegistry.Service.use((svc) => svc.updateTurn(parent.id, taskId)))

        const after = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(after!.turnCount).toBe(2)
      })
    })
  })

  describe("listBySession", () => {
    test("returns all actors registered under a session", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const other = await rt.runPromise(Session.Service.use((svc) => svc.create()))

        const id1 = SessionID.descending()
        const id2 = SessionID.descending()
        const id3 = SessionID.descending()

        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: id1,
              mode: "subagent",
              agent: "explore",
              description: "Task 1",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: id2,
              mode: "subagent",
              agent: "build",
              description: "Task 2",
              contextMode: "state",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: other.id,
              actorID: id3,
              mode: "subagent",
              agent: "explore",
              description: "Other parent task",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )

        const tasks = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.listBySession(parent.id)))
        expect(tasks.length).toBe(3) // "main" (auto-registered) + id1 + id2
        const ids = tasks.map((t) => t.actorID)
        expect(ids).toContain("main")
        expect(ids).toContain(id1)
        expect(ids).toContain(id2)
        expect(ids).not.toContain(id3)
      })
    })
  })

  describe("listActive", () => {
    test("returns tasks with pending or running status", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))

        const idPending = SessionID.descending()
        const idRunning = SessionID.descending()
        const idCompleted = SessionID.descending()

        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: idPending,
              mode: "subagent",
              agent: "explore",
              description: "Pending task",
              contextMode: "none",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: idRunning,
              mode: "subagent",
              agent: "build",
              description: "Running task",
              contextMode: "none",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: idCompleted,
              mode: "subagent",
              agent: "explore",
              description: "Completed task",
              contextMode: "none",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )

        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, idRunning, { status: "running" })),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, idCompleted, { status: "idle", lastOutcome: "success" })),
        )

        const active = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.listActive()))
        const activeIds = active.map((t) => t.actorID)

        expect(activeIds).toContain(idPending)
        expect(activeIds).toContain(idRunning)
        expect(activeIds).not.toContain(idCompleted)
      })
    })

    test("returns empty list when no active tasks", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const active = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.listActive()))
        expect(active).toHaveLength(0)
      })
    })
  })

  describe("orphan recovery", () => {
    // Desktop tool-step-schema regression [TP-R14-10].
    test("preserves running tasks from another instance on new layer init", async () => {
      // First, create a task in "running" state
      await using tmp = await tmpdir({ git: true })

      let taskId: SessionID
      let parentId: SessionID

      // First runtime: register and set running
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        parentId = parent.id
        taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Orphan test task",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, taskId, { status: "running" })),
        )

        const before = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(before!.status).toBe("running")
      })

      // Simulate a different process by manually setting a different instance_id
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          await Database.use((db) =>
            db
              .update(ActorRegistryTable)
              .set({ instance_id: "old-process-id" })
              .where(
                and(
                  eq(ActorRegistryTable.session_id, parentId!),
                  eq(ActorRegistryTable.actor_id, taskId!),
                ),
              )
              .run(),
          )
        },
      })

      // Second runtime (simulates restart): orphan recovery should mark it idle+failure
      await withRegistry(tmp.path, async (rt) => {
        const recovered = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.get(parentId!, taskId!)),
        )
        expect(recovered!.status).toBe("running")
        expect(recovered!.lastOutcome).toBeUndefined()
        expect(recovered!.lastError).toBeUndefined()
      })
    })

    test("does NOT orphan actors registered in the same registry instance", async () => {
      await using tmp = await tmpdir({ git: true })

      // Register an actor and set it to running within the same runtime
      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parent.id,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Current instance task",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parent.id, taskId, { status: "running" })),
        )

        // Actor should still be running — not orphaned
        const actor = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parent.id, taskId)))
        expect(actor!.status).toBe("running")
        expect(actor!.lastOutcome).toBeUndefined()
        expect(actor!.lastError).toBeUndefined()
      })
    })

    test("same-process layer rebuild does NOT orphan running children (singleton instanceID)", async () => {
      await using tmp = await tmpdir({ git: true })
      // Use Instance.provide to share the same directory across multiple layer constructions
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const taskId = SessionID.descending()
          let parentId: SessionID

          // First layer construction: register and set running
          const rt1 = ManagedRuntime.make(testLayer)
          try {
            const parent = await rt1.runPromise(Session.Service.use((svc) => svc.create()))
            parentId = parent.id
            await rt1.runPromise(
              ActorRegistry.Service.use((svc) =>
                svc.register({
                  sessionID: parentId,
                  actorID: taskId,
                  mode: "subagent",
                  agent: "explore",
                  description: "Layer rebuild test task",
                  contextMode: "none",
                  background: false,
                  lifecycle: "ephemeral",
                }),
              ),
            )
            await rt1.runPromise(
              ActorRegistry.Service.use((svc) => svc.updateStatus(parentId, taskId, { status: "running" })),
            )
          } finally {
            await rt1.dispose()
          }

          // Second layer construction (simulates layer rebuild): should NOT orphan
          const rt2 = ManagedRuntime.make(testLayer)
          try {
            const actor = await rt2.runPromise(
              ActorRegistry.Service.use((svc) => svc.get(parentId, taskId)),
            )
            expect(actor!.status).toBe("running")
            expect(actor!.lastOutcome).toBeUndefined()
            expect(actor!.lastError).toBeUndefined()
          } finally {
            await rt2.dispose()
          }
        },
      })
    })

    test("[TP-R14-10] a different instanceID is not evidence of failure", async () => {
      await using tmp = await tmpdir({ git: true })

      // First runtime: register an actor
      let taskId: SessionID
      let parentId: SessionID

      await withRegistry(tmp.path, async (rt) => {
        const parent = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        parentId = parent.id
        taskId = SessionID.descending()
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: parentId,
              actorID: taskId,
              mode: "subagent",
              agent: "explore",
              description: "Different instance test task",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.updateStatus(parentId, taskId, { status: "running" })),
        )

        // Verify it's running
        const before = await rt.runPromise(ActorRegistry.Service.use((svc) => svc.get(parentId, taskId)))
        expect(before!.status).toBe("running")
      })

      // Manually update the instance_id to simulate a different process
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          await Database.use((db) =>
            db
              .update(ActorRegistryTable)
              .set({ instance_id: "different-process-id" })
              .where(
                and(
                  eq(ActorRegistryTable.session_id, parentId!),
                  eq(ActorRegistryTable.actor_id, taskId!),
                ),
              )
              .run(),
          )
        },
      })

      // Second runtime: should orphan the row with different instance_id
      await withRegistry(tmp.path, async (rt) => {
        const recovered = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.get(parentId!, taskId!)),
        )
        expect(recovered!.status).toBe("running")
        expect(recovered!.lastOutcome).toBeUndefined()
        expect(recovered!.lastError).toBeUndefined()
      })
    })
  })

  describe("agentTypeFor", () => {
    test("returns 'main' when actorID is 'main'", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.agentTypeFor(session.id, "main")),
        )
        expect(result).toBe("main")
      })
    })

    test("returns the agent type for a registered actor", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "writer-1",
              mode: "subagent",
              agent: "checkpoint-writer",
              description: "test",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.agentTypeFor(session.id, "writer-1")),
        )
        expect(result).toBe("checkpoint-writer")
      })
    })
  })

  describe("isSystemSpawned", () => {
    test("true for checkpoint-writer", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "writer-1",
              mode: "subagent",
              agent: "checkpoint-writer",
              description: "x",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.isSystemSpawned(session.id, "writer-1")),
        )
        expect(result).toBe(true)
      })
    })

    test("false for explorer (model-spawned)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "explorer-1",
              mode: "subagent",
              agent: "explorer",
              description: "x",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.isSystemSpawned(session.id, "explorer-1")),
        )
        expect(result).toBe(false)
      })
    })

    test("false for 'main' actorID", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.isSystemSpawned(session.id, "main")),
        )
        expect(result).toBe(false)
      })
    })
  })

  describe("servesCheckpoint", () => {
    test("true for undefined actorID (main runLoop)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, undefined)),
        )
        expect(result).toBe(true)
      })
    })

    test("true for 'main' actorID", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, "main")),
        )
        expect(result).toBe(true)
      })
    })

    test("true for peer (agentID is child.id, mode peer)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "child-session-1",
              mode: "peer",
              agent: "build",
              description: "x",
              contextMode: "none",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, "child-session-1")),
        )
        expect(result).toBe(true)
      })
    })

    test("false for subagent (explorer) — uses per-actor compaction", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "explorer-1",
              mode: "subagent",
              agent: "explorer",
              description: "x",
              contextMode: "none",
              background: false,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, "explorer-1")),
        )
        expect(result).toBe(false)
      })
    })

    test("false for checkpoint-writer (system-spawned)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "writer-1",
              mode: "subagent",
              agent: "checkpoint-writer",
              description: "x",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, "writer-1")),
        )
        expect(result).toBe(false)
      })
    })

    test("true for unregistered actorID (fail open)", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.servesCheckpoint(session.id, "ghost-9")),
        )
        expect(result).toBe(true)
      })
    })
  })

  describe("allocateActorID", () => {
    test("returns sequential <type>-<n>", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const first = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.allocateActorID(session.id, "writer")),
        )
        expect(first).toBe("writer-1")
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "writer-1",
              mode: "subagent",
              agent: "writer",
              description: "x",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const second = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.allocateActorID(session.id, "writer")),
        )
        expect(second).toBe("writer-2")
        const explorer = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.allocateActorID(session.id, "explorer")),
        )
        expect(explorer).toBe("explorer-1")
      })
    })

    test("is scoped per session", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const sessionA = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const sessionB = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: sessionA.id,
              actorID: "writer-1",
              mode: "subagent",
              agent: "writer",
              description: "x",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const result = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.allocateActorID(sessionB.id, "writer")),
        )
        expect(result).toBe("writer-1")
      })
    })

    test("registerAllocated serializes concurrent writers and follows explicit high IDs", async () => {
      await using tmp = await tmpdir({ git: true })
      await withRegistry(tmp.path, async (rt) => {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.register({
              sessionID: session.id,
              actorID: "writer-9",
              mode: "subagent",
              agent: "writer",
              description: "manual high ID",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        const high = await rt.runPromise(
          ActorRegistry.Service.use((svc) =>
            svc.registerAllocated({
              sessionID: session.id,
              mode: "subagent",
              agent: "writer",
              description: "after manual ID",
              contextMode: "full",
              background: true,
              lifecycle: "ephemeral",
            }),
          ),
        )
        expect(high.actorID).toBe("writer-10")

        const concurrent = await Promise.all(
          Array.from({ length: 4 }, (_, index) =>
            rt.runPromise(
              ActorRegistry.Service.use((svc) =>
                svc.registerAllocated({
                  sessionID: session.id,
                  mode: "subagent",
                  agent: "writer",
                  description: `concurrent ${index}`,
                  contextMode: "full",
                  background: true,
                  lifecycle: "ephemeral",
                }),
              ),
            ),
          ),
        )
        expect(concurrent.map((entry) => entry.actorID).sort()).toEqual(["writer-11", "writer-12", "writer-13", "writer-14"])
        expect((await rt.runPromise(ActorRegistry.Service.use((svc) => svc.listBySession(session.id)))).length).toBe(7)
      })
    })
  })

  describe("zombie sweep on init", () => {
    test("settles running/pending rows older than the abandon threshold", async () => {
      await using tmp = await tmpdir({ git: true })
      // Create sessions first (FK constraint), then insert zombie/fresh rows
      // BEFORE the layer inits so the sweep sees them.
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const rt = ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer))
          try {
            const zombieSession = await rt.runPromise(Session.Service.use((svc) => svc.create()))
            const freshSession = await rt.runPromise(Session.Service.use((svc) => svc.create()))
            const stale = Date.now() - 11 * 60 * 1000
            Database.use((db) =>
              db
                .insert(ActorRegistryTable)
                .values({
                  session_id: zombieSession.id,
                  actor_id: "zombie-1",
                  mode: "subagent",
                  parent_actor_id: null,
                  status: "running",
                  last_outcome: null,
                  result_message_id: null,
                  lifecycle: "ephemeral",
                  agent: "explore",
                  description: "crashed child",
                  context_mode: "none",
                  context_watermark: null,
                  background: true,
                  tools: null,
                  last_turn_time: stale,
                  turn_count: 1,
                  last_activity_time: stale,
                  last_error: null,
                  instance_id: "dead-instance",
                  time_completed: null,
                  time_created: stale,
                  time_updated: stale,
                })
                .run(),
            )
            Database.use((db) =>
              db
                .insert(ActorRegistryTable)
                .values({
                  session_id: freshSession.id,
                  actor_id: "fresh-1",
                  mode: "subagent",
                  parent_actor_id: null,
                  status: "running",
                  last_outcome: null,
                  result_message_id: null,
                  lifecycle: "ephemeral",
                  agent: "explore",
                  description: "live child",
                  context_mode: "none",
                  context_watermark: null,
                  background: true,
                  tools: null,
                  last_turn_time: Date.now(),
                  turn_count: 1,
                  last_activity_time: Date.now(),
                  last_error: null,
                  instance_id: "live-instance",
                  time_completed: null,
                  time_created: Date.now(),
                  time_updated: Date.now(),
                })
                .run(),
            )
            // Store for later assertion
            ;(globalThis as Record<string, unknown>).__zombieTest = {
              zombieSession: zombieSession.id,
              freshSession: freshSession.id,
            }
          } finally {
            await rt.dispose()
          }
        },
      })
      const ids = (globalThis as Record<string, unknown>).__zombieTest as {
        zombieSession: string
        freshSession: string
      }
      // Bootstrap calls the actor sweep explicitly; layer construction leaves
      // existing rows alone.
      await withRegistry(tmp.path, async (rt) => {
        void rt
        sweepAbandonedZombies()
        const zombie = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.get(ids.zombieSession as never, "zombie-1")),
        )
        expect(zombie?.status).toBe("idle")
        expect(zombie?.lastOutcome).toBe("failure")
        expect(zombie?.lastError).toContain("abandon threshold")
        // [TP-RUN-R12-32] recoverability guidance on real sweep output
        expect(zombie?.lastError).toContain("Not final")
        expect(zombie?.lastError).toContain("actor send")
        expect(zombie?.lastError).not.toContain("session Resume")
        const fresh = await rt.runPromise(
          ActorRegistry.Service.use((svc) => svc.get(ids.freshSession as never, "fresh-1")),
        )
        expect(fresh?.status).toBe("running")
      })
    })

    test("same-process rebuild does not settle a stale-but-alive running row", async () => {
      await using tmp = await tmpdir({ git: true })
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const taskId = SessionID.descending()
          let parentId: SessionID
          const rt1 = ManagedRuntime.make(testLayer)
          try {
            const parent = await rt1.runPromise(Session.Service.use((svc) => svc.create()))
            parentId = parent.id
            await rt1.runPromise(
              ActorRegistry.Service.use((svc) =>
                svc.register({
                  sessionID: parentId,
                  actorID: taskId,
                  mode: "subagent",
                  agent: "explore",
                  description: "long LLM step, no part writes",
                  contextMode: "none",
                  background: false,
                  lifecycle: "ephemeral",
                }),
              ),
            )
            await rt1.runPromise(
              ActorRegistry.Service.use((svc) => svc.updateStatus(parentId, taskId, { status: "running" })),
            )
            // Backdate activity past the abandon threshold. This row still
            // belongs to PROCESS_INSTANCE_ID — a rebuild must not settle it.
            const stale = Date.now() - 11 * 60 * 1000
            Database.use((db) =>
              db
                .update(ActorRegistryTable)
                .set({ last_activity_time: stale, last_turn_time: stale, time_created: stale, time_updated: stale })
                .where(and(eq(ActorRegistryTable.session_id, parentId), eq(ActorRegistryTable.actor_id, taskId)))
                .run(),
            )
          } finally {
            await rt1.dispose()
          }
          const rt2 = ManagedRuntime.make(testLayer)
          try {
            // Sweep after rebuild — same-process running row must stay (layer no longer sweeps).
            sweepAbandonedZombies()
            const actor = await rt2.runPromise(
              ActorRegistry.Service.use((svc) => svc.get(parentId, taskId)),
            )
            expect(actor?.status).toBe("running")
            expect(actor?.lastOutcome).toBeUndefined()
            expect(actor?.lastError).toBeUndefined()
          } finally {
            await rt2.dispose()
          }
        },
      })
    })

  })
})
