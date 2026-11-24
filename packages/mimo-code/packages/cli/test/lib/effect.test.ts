import { expect, test } from "bun:test"
import { Effect } from "effect"
import { Instance } from "../../src/project/instance"
import { GlobalBus } from "../../src/bus/global"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "./effect"

if (process.env.OPENCLANK_EFFECT_TIMEOUT_TEST === "1") {
  const it = testEffect(CrossSpawnSpawner.defaultLayer)
  let directory = ""
  let finalized = false
  const listeners = GlobalBus.listenerCount("event")
  it.live("timeout interrupts the instance effect", () => provideTmpdirInstance((dir) => Effect.gen(function* () {
    directory = dir
    const listener = () => {}
    yield* Effect.acquireRelease(
      Effect.sync(() => GlobalBus.on("event", listener)),
      () => Effect.sync(() => { GlobalBus.off("event", listener) }),
    )
    yield* Effect.addFinalizer(() => Effect.sleep("25 millis").pipe(
      Effect.andThen(Effect.sync(() => { finalized = true })),
    ))
    yield* Effect.never
  }), { git: true }), 1000)
  test("the next test observes completed cleanup", () => {
    expect(directory).not.toBe("")
    expect(finalized).toBe(true)
    expect(GlobalBus.listenerCount("event")).toBe(listeners)
    expect(Instance.refreshStatus(directory).state).toBe("applied")
    console.log("effect timeout cleanup verified")
  })
} else {
  test("Bun timeout interrupts and finalizes Effect tests before the next test", async () => {
    const child = Bun.spawn([process.execPath, "test", import.meta.path], {
      cwd: process.cwd(),
      env: { ...process.env, OPENCLANK_EFFECT_TIMEOUT_TEST: "1" },
      stdout: "pipe",
      stderr: "pipe",
    })
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
      child.exited,
    ])
    expect(code).toBe(1)
    expect(stderr).toContain("timed out after 1000ms")
    expect(stderr).toContain("1 pass")
    expect(stderr).toContain("1 fail")
    expect(stdout).toContain("effect timeout cleanup verified")
    expect(stderr).not.toContain("Session not found")
    expect(stderr).not.toContain("MaxListenersExceeded")
  }, 10_000)
}
