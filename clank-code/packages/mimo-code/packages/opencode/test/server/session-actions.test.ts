import { afterEach, describe, expect, mock, test } from "bun:test"
import { Effect } from "effect"
import { Instance } from "../../src/project/instance"
import { Server } from "../../src/server/server"
import { Session as SessionNs } from "../../src/session"
import { Truncate } from "../../src/tool"
import type { SessionID } from "../../src/session/schema"
import { Log } from "../../src/util"
import { tmpdir } from "../fixture/fixture"

void Log.init({ print: false })

function run<A, E>(fx: Effect.Effect<A, E, SessionNs.Service>) {
  return Effect.runPromise(fx.pipe(Effect.provide(SessionNs.defaultLayer)))
}

const svc = {
  ...SessionNs,
  create(input?: SessionNs.CreateInput) {
    return run(SessionNs.Service.use((svc) => svc.create(input)))
  },
  remove(id: SessionID) {
    return run(SessionNs.Service.use((svc) => svc.remove(id)))
  },
}

afterEach(async () => {
  mock.restore()
  await Instance.disposeAll()
})

describe("session action routes", () => {
  test("abort route returns success", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await svc.create({})
        const app = Server.Default().app

        const res = await app.request(`/session/${session.id}/abort`, { method: "POST" })

        expect(res.status).toBe(200)
        expect(await res.json()).toBe(true)

        await svc.remove(session.id)
      },
    })
  })

  test("delete route removes retained output for the session", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await svc.create({})
        const output = await Effect.runPromise(
          Truncate.Service.use((truncate) =>
            truncate.write("retained output", {
              owner: process.env.OPEN_CLANK_OWNER ?? "local",
              workspace: tmp.path,
              sessionID: session.id,
            }),
          ).pipe(Effect.provide(Truncate.defaultLayer)),
        )
        expect(await Bun.file(output).exists()).toBe(true)

        const res = await Server.Default().app.request(`/session/${session.id}`, {
          method: "DELETE",
        })

        expect(res.status).toBe(200)
        expect(await Bun.file(output).exists()).toBe(false)
        expect(await Bun.file(output + ".meta.json").exists()).toBe(false)
      },
    })
  })
})
