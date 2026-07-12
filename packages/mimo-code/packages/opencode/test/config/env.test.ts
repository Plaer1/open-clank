import { test, expect, afterEach } from "bun:test"
import { Effect, Layer, Option } from "effect"
import { NodeFileSystem, NodePath } from "@effect/platform-node"
import { Config } from "../../src/config"
import { EffectFlock } from "@mimo-ai/shared/util/effect-flock"
import { Auth } from "../../src/auth"
import { Account } from "../../src/account/account"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Env } from "../../src/env"
import { tmpdir } from "../fixture/fixture"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Instance } from "../../src/project/instance"
import { Filesystem } from "../../src/util"
import { Npm } from "@/npm"
import path from "path"

const infra = CrossSpawnSpawner.defaultLayer.pipe(
  Layer.provideMerge(Layer.mergeAll(NodeFileSystem.layer, NodePath.layer)),
)

const emptyAccount = Layer.mock(Account.Service)({
  active: () => Effect.succeed(Option.none()),
  activeOrg: () => Effect.succeed(Option.none()),
})

const emptyAuth = Layer.mock(Auth.Service)({
  all: () => Effect.succeed({}),
})

const layer = Config.layer.pipe(
  Layer.provide(EffectFlock.defaultLayer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provideMerge(Env.defaultLayer),
  Layer.provide(emptyAuth),
  Layer.provide(emptyAccount),
  Layer.provideMerge(infra),
  Layer.provide(Npm.defaultLayer),
)

const clear = (wait = false) =>
  Effect.runPromise(Config.Service.use((svc) => svc.invalidate(wait)).pipe(Effect.scoped, Effect.provide(layer)))

afterEach(async () => {
  await clear(true)
})

test("config env injects new vars but defers to existing process env vars", async () => {
  const originalNew = process.env["MIMO_TEST_ENV_NEW"]
  const originalExisting = process.env["MIMO_TEST_ENV_EXISTING"]
  process.env["MIMO_TEST_ENV_EXISTING"] = "from-real-env"
  delete process.env["MIMO_TEST_ENV_NEW"]

  try {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await Filesystem.write(
          path.join(dir, "mimocode.json"),
          JSON.stringify({
            $schema: "https://opencode.ai/config.json",
            env: {
              MIMO_TEST_ENV_NEW: "hello123",
              MIMO_TEST_ENV_EXISTING: "from-config",
            },
          }),
        )
      },
    })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const result = await Effect.runPromise(
          Config.Service.use((svc) =>
            Effect.gen(function* () {
              const config = yield* svc.get()
              const envState = yield* (yield* Env.Service).all()
              return { config, envState }
            }),
          ).pipe(Effect.scoped, Effect.provide(layer)),
        )

        // config field is parsed
        expect(result.config.env).toEqual({
          MIMO_TEST_ENV_NEW: "hello123",
          MIMO_TEST_ENV_EXISTING: "from-config",
        })

        // new var injected into process.env (what the bash tool reads)
        expect(process.env["MIMO_TEST_ENV_NEW"]).toBe("hello123")
        // real env var takes precedence — config value must NOT clobber it
        expect(process.env["MIMO_TEST_ENV_EXISTING"]).toBe("from-real-env")

        // new var injected into the Env service
        expect(result.envState["MIMO_TEST_ENV_NEW"]).toBe("hello123")
        // Env service also reflects the real env value, not the config default
        expect(result.envState["MIMO_TEST_ENV_EXISTING"]).toBe("from-real-env")
      },
    })
  } finally {
    if (originalNew !== undefined) process.env["MIMO_TEST_ENV_NEW"] = originalNew
    else delete process.env["MIMO_TEST_ENV_NEW"]
    if (originalExisting !== undefined) process.env["MIMO_TEST_ENV_EXISTING"] = originalExisting
    else delete process.env["MIMO_TEST_ENV_EXISTING"]
  }
})

test("config env supports {env:VAR} substitution", async () => {
  const originalSource = process.env["MIMO_TEST_ENV_SOURCE"]
  const originalTarget = process.env["MIMO_TEST_ENV_TARGET"]
  process.env["MIMO_TEST_ENV_SOURCE"] = "substituted-value"
  delete process.env["MIMO_TEST_ENV_TARGET"]

  try {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await Filesystem.write(
          path.join(dir, "mimocode.json"),
          JSON.stringify({
            $schema: "https://opencode.ai/config.json",
            env: {
              MIMO_TEST_ENV_TARGET: "{env:MIMO_TEST_ENV_SOURCE}",
            },
          }),
        )
      },
    })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const config = await Effect.runPromise(
          Config.Service.use((svc) => svc.get()).pipe(Effect.scoped, Effect.provide(layer)),
        )
        expect(config.env?.["MIMO_TEST_ENV_TARGET"]).toBe("substituted-value")
        expect(process.env["MIMO_TEST_ENV_TARGET"]).toBe("substituted-value")
      },
    })
  } finally {
    if (originalSource !== undefined) process.env["MIMO_TEST_ENV_SOURCE"] = originalSource
    else delete process.env["MIMO_TEST_ENV_SOURCE"]
    if (originalTarget !== undefined) process.env["MIMO_TEST_ENV_TARGET"] = originalTarget
    else delete process.env["MIMO_TEST_ENV_TARGET"]
  }
})

// Reload idempotency: both get() calls run inside ONE Effect.provide(layer),
// so the layer closure (which holds the injected-key tracking map) persists
// across invalidate(). This mirrors a live config reload in a running process
// where process.env is long-lived.
const reloadScenario = (dir: string, rewrite: () => Promise<void>) =>
  Config.Service.use((svc) =>
    Effect.gen(function* () {
      yield* svc.get()
      yield* Effect.promise(rewrite)
      yield* svc.invalidate(true)
      return yield* svc.get()
    }),
  ).pipe(Effect.scoped, Effect.provide(layer))

test("editing a config env value takes effect on reload", async () => {
  const key = "MIMO_TEST_ENV_EDIT"
  const original = process.env[key]
  delete process.env[key]

  try {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await Filesystem.write(
          path.join(dir, "mimocode.json"),
          JSON.stringify({ $schema: "https://opencode.ai/config.json", env: { [key]: "v1" } }),
        )
      },
    })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const config = await Effect.runPromise(
          reloadScenario(tmp.path, () =>
            Filesystem.write(
              path.join(tmp.path, "mimocode.json"),
              JSON.stringify({ $schema: "https://opencode.ai/config.json", env: { [key]: "v2" } }),
            ),
          ),
        )
        expect(config.env?.[key]).toBe("v2")
        // The edited value replaces our own prior injection, not treated as a
        // pre-existing real env var.
        expect(process.env[key]).toBe("v2")
      },
    })
  } finally {
    if (original !== undefined) process.env[key] = original
    else delete process.env[key]
  }
})

test("removing a config env key restores original on reload", async () => {
  const key = "MIMO_TEST_ENV_REMOVE"
  const original = process.env[key]
  delete process.env[key]

  try {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await Filesystem.write(
          path.join(dir, "mimocode.json"),
          JSON.stringify({ $schema: "https://opencode.ai/config.json", env: { [key]: "injected" } }),
        )
      },
    })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        await Effect.runPromise(
          reloadScenario(tmp.path, () =>
            Filesystem.write(
              path.join(tmp.path, "mimocode.json"),
              JSON.stringify({ $schema: "https://opencode.ai/config.json", env: {} }),
            ),
          ),
        )
        // Was unset before injection → removing the config key unsets it again,
        // rather than leaving the stale "injected" value behind.
        expect(process.env[key]).toBeUndefined()
      },
    })
  } finally {
    if (original !== undefined) process.env[key] = original
    else delete process.env[key]
  }
})
