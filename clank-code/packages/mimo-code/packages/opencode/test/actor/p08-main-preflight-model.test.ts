import { expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { tmpdir } from "../fixture/fixture"

// Actual registry transaction and actor projection; the preflight observation
// is a registry read, not Host authorization or a provider invocation.
test("MAIN preflight model is visible and claim rejects a changed model", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const rt = ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer))
    try {
      const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
      const registry = await rt.runPromise(ActorRegistry.Service.use((svc) => Effect.succeed(svc)))
      const actor = await rt.runPromise(registry.get(session.id, "main"))
      expect(actor?.status).toBe("pending")
      expect(actor?.executionRevision).toBe(0)
      const first = { providerID: "local-a", modelID: "model-a" }
      const second = { providerID: "local-b", modelID: "model-b" }
      expect(await rt.runPromise(registry.prepareMainPreflightModel!(session.id, 0, first))).toBe(true)
      expect((await rt.runPromise(registry.get(session.id, "main")))?.effectiveModel).toEqual(first)
      expect(await rt.runPromise(registry.prepareMainPreflightModel!(session.id, 0, second))).toBe(true)
      expect(await rt.runPromise(registry.beginExecution(session.id, "main", undefined, 0,
        { effectiveModel: first, expectedEffectiveModel: first }))).toBeUndefined()
      const revision = await rt.runPromise(registry.beginExecution(session.id, "main", undefined, 0,
        { effectiveModel: second, expectedEffectiveModel: second }))
      expect(revision).toBe(1)
      expect(await rt.runPromise(registry.prepareMainPreflightModel!(session.id, 1, first))).toBe(false)
    } finally { await rt.dispose() }
  } })
  await Instance.disposeAll()
})
