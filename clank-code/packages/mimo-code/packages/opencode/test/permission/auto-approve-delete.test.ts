import { afterEach, describe, expect } from "bun:test"
import { Effect, Fiber, Layer } from "effect"
import { Bus } from "../../src/bus"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Permission } from "../../src/permission"
import { Instance } from "../../src/project/instance"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { Log } from "../../src/util"

void Log.init({ print: false })

afterEach(async () => {
  await Instance.disposeAll()
})

const bus = Bus.layer
const env = Layer.mergeAll(Permission.layer.pipe(Layer.provide(bus)), bus, CrossSpawnSpawner.defaultLayer)
const it = testEffect(env)

function buildRequest(extra?: Partial<Parameters<Permission.Interface["ask"]>[0]>) {
  return {
    permission: "bash_delete" as never,
    patterns: ["rm foo.txt"],
    always: [],
    metadata: {},
    sessionID: "ses_test" as never,
    ruleset: [],
    tool: { messageID: "msg_test" as never, callID: "call_test" },
    ...extra,
  }
}

describe("Permission autoApproveDelete scope", () => {
  it.live(
    "defaults to off",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const perm = yield* Permission.Service
        expect(yield* perm.autoApproveDelete()).toBe(false)
      }),
    ),
  )

  it.live(
    "auto-approves the named delete case (bash_delete)",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const perm = yield* Permission.Service
        yield* perm.setAutoApproveDelete(true)
        let asked = 0
        const unsub = Bus.subscribe(Permission.Event.Asked, () => {
          asked += 1
        })
        const result = yield* perm.ask(buildRequest()).pipe(Effect.exit)
        unsub()
        expect(result._tag).toBe("Success")
        expect(asked).toBe(0)
        expect((yield* perm.list()).length).toBe(0)
      }),
    ),
  )

  it.live(
    "bash_destructive still forces ask when autoApproveDelete is on",
    provideTmpdirInstance(() =>
      Effect.scoped(
        Effect.gen(function* () {
          const perm = yield* Permission.Service
          yield* perm.setAutoApproveDelete(true)
          const fiber = yield* perm
            .ask(buildRequest({ permission: "bash_destructive" as never }))
            .pipe(Effect.forkScoped)
          while ((yield* perm.list()).length === 0) {
            yield* Effect.promise(() => Bun.sleep(10))
          }
          // Lands in the human ask flow — autoApproveDelete must not swallow it.
          const items = yield* perm.list()
          expect(items).toHaveLength(1)
          expect(items[0].permission).toBe("bash_destructive")
          yield* perm.reply({ requestID: items[0].id, reply: "reject" })
          const result = yield* Fiber.await(fiber)
          expect(result._tag).toBe("Failure")
        }),
      ),
    ),
  )

  it.live(
    "bash_delete stays forced-ask when autoApproveDelete is off",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const perm = yield* Permission.Service
        // interactive:false so the forced ask fails fast instead of blocking.
        const result = yield* perm.ask(buildRequest({ interactive: false })).pipe(Effect.exit)
        expect(result._tag).toBe("Failure")
      }),
    ),
  )

  it.live(
    "explicit deny still wins over autoApproveDelete",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const perm = yield* Permission.Service
        yield* perm.setAutoApproveDelete(true)
        const result = yield* perm
          .ask(buildRequest({ ruleset: [{ permission: "bash_delete", pattern: "*", action: "deny" }] }))
          .pipe(Effect.exit)
        expect(result._tag).toBe("Failure")
      }),
    ),
  )

  it.live(
    "wildcard allow cannot pre-authorize bash_destructive even with autoApproveDelete on",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const perm = yield* Permission.Service
        yield* perm.setAutoApproveDelete(true)
        const result = yield* perm
          .ask(
            buildRequest({
              permission: "bash_destructive" as never,
              ruleset: [{ permission: "*", pattern: "*", action: "allow" }],
              interactive: false,
            }),
          )
          .pipe(Effect.exit)
        expect(result._tag).toBe("Failure")
      }),
    ),
  )
})
