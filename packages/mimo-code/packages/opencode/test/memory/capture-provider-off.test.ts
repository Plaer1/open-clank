import { afterAll, expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"
import { Log } from "../../src/util"
import { testConfig, run, realTurn, hasCreds, fmRowsContaining } from "./capture-real-harness"

void Log.init({ print: false })

afterAll(async () => {
  await Instance.disposeAll()
})

test.skipIf(!hasCreds)(
  "captures nothing when memory provider is not frankenmemory",
  async () => {
    const marker = `provideroffmark${Date.now()}`
    await using tmp = await tmpdir({ git: true, config: testConfig(false) })
    await Instance.provide({
      directory: tmp.path,
      fn: () => run(realTurn({ agent: "build", marker, settleMs: 8000 })),
    })
    expect(fmRowsContaining(marker)).toBe(0)
  },
  120000,
)
