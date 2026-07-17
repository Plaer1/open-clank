// Identity metaplan Slice 02/05 (ruling R2): the chat profile is a REAL
// no-tools conversation agent — its prompt replaces the coding-agent
// framing, its hard permission denies every tool, and it never becomes the
// default agent (build stays first visible primary).
import { afterEach, test, expect } from "bun:test"
import { Effect } from "effect"
import { tmpdir, provideInstance } from "../fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Agent } from "../../src/agent/agent"
import { Permission } from "../../src/permission"

function load<A>(dir: string, fn: (svc: Agent.Interface) => Effect.Effect<A>) {
  return Effect.runPromise(provideInstance(dir)(Agent.Service.use(fn)).pipe(Effect.provide(Agent.defaultLayer)))
}

afterEach(async () => {
  await Instance.disposeAll()
})

test("chat agent exists, carries its own prompt, and hard-denies tools", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const chat = await load(tmp.path, (svc) => svc.get("chat"))
      expect(chat).toBeDefined()
      expect(chat!.mode).toBe("primary")
      // Own prompt — replaces the provider coding-agent framing entirely.
      expect(chat!.prompt).toContain("no model-callable tools")
      expect(chat!.prompt).not.toMatch(/MiMoCode|Xiaomi/)
      // Hard no-tools invariant: wildcard deny that user config cannot relax.
      const hard = chat!.hardPermission ?? []
      const denied = Permission.evaluate("bash", "*", hard)
      expect(denied.action).toBe("deny")
      const edit = Permission.evaluate("edit", "*", hard)
      expect(edit.action).toBe("deny")
      const question = Permission.evaluate("question", "*", hard)
      expect(question.action).toBe("allow")
    },
  })
})

test("default agent stays build, not chat", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const name = await load(tmp.path, (svc) => svc.defaultAgent())
      expect(name).toBe("build")
    },
  })
})
