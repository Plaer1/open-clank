import { describe, expect, test } from "bun:test"
import { Effect } from "effect"
import { SessionStatus } from "../../src/session/status"
import { SessionID } from "../../src/session/schema"
import { Instance } from "@/project/instance"
import { AppRuntime } from "@/effect/app-runtime"

describe("SessionStatus.setRetry is visible to GET /session/status (list)", () => {
  test("setRetry updates the map that list() serves", async () => {
    await Instance.provide({
      directory: process.cwd(),
      fn: async () => {
        const sessionID = SessionID.make("ses_recover_retry_map")
        const result = await AppRuntime.runPromise(
          SessionStatus.Service.use((svc) =>
            Effect.gen(function* () {
              yield* svc.set(sessionID, { type: "busy" })
              yield* svc.setRetry(sessionID, {
                type: "retry",
                attempt: 1,
                phaseAttempt: 1,
                message: "No available channel for model mopd-a-20step",
                next: Date.now() + 100,
                phase: "request",
                scope: "request",
              })
              const map = yield* svc.list()
              return map.get(sessionID)
            }),
          ),
        )
        expect(result).toMatchObject({
          type: "retry",
          attempt: 1,
          message: "No available channel for model mopd-a-20step",
        })
      },
    })
  })
})
