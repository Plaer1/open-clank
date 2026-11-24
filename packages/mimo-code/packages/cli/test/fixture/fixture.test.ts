import { $ } from "bun"
import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { Deferred, Effect, Fiber } from "effect"
import { Instance } from "../../src/project/instance"
import { tmpdir, cleanupTmpdir, provideInstance } from "./fixture"

describe("tmpdir", () => {
  test("disables fsmonitor for git fixtures", async () => {
    await using tmp = await tmpdir({ git: true })

    const value = (await $`git config core.fsmonitor`.cwd(tmp.path).quiet().text()).trim()
    expect(value).toBe("false")
  })

  test("removes directories on dispose", async () => {
    const tmp = await tmpdir({ git: true })
    const dir = tmp.path

    await tmp[Symbol.asyncDispose]()

    const exists = await fs
      .stat(dir)
      .then(() => true)
      .catch(() => false)
    expect(exists).toBe(false)
  })

  test("reports dispose failures after cleaning the directory", async () => {
    let dirpath = ""

    await expect(
      (async () => {
        await using tmp = await tmpdir({
          dispose: async (dir) => {
            dirpath = dir
            await fs.rm(path.join(dir, "missing", "child"))
          },
        })
      })(),
    ).rejects.toThrow()

    const exists = await fs
      .stat(dirpath)
      .then(() => true)
      .catch(() => false)
    expect(exists).toBe(false)
  })

  test("interrupting an instance effect waits for its finalizers and releases admission", async () => {
    await using tmp = await tmpdir({ git: true })
    const started = Effect.runSync(Deferred.make<void>())
    let finalized = false
    const fiber = Effect.gen(function* () {
      yield* Effect.addFinalizer(() => Effect.sleep("20 millis").pipe(
        Effect.andThen(Effect.sync(() => { finalized = true })),
      ))
      yield* Deferred.succeed(started, undefined)
      yield* Effect.never
    }).pipe(Effect.scoped, provideInstance(tmp.path), Effect.runFork)
    await Effect.runPromise(Deferred.await(started))
    await Effect.runPromise(Fiber.interrupt(fiber))
    expect(finalized).toBe(true)
    await Instance.disposeDirectory(tmp.path)
    expect(Instance.refreshStatus(tmp.path).state).toBe("applied")
  })

  test("reports cleanup failures", async () => {
    const dir = await fs.mkdtemp(path.join("/tmp", "openclank-test-cleanup-failure-"))

    await expect(cleanupTmpdir(dir, () => Promise.reject(new Error("cleanup failed")))).rejects.toThrow(
      `Failed to cleanup temporary directory ${dir}: cleanup failed`,
    )

    await fs.rm(dir, { recursive: true, force: true })
  })
})
