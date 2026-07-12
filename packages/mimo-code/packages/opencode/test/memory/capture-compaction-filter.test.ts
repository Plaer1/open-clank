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
  "does not capture parts from the compaction agent",
  async () => {
    const marker = `compactmark${Date.now()}`
    await using tmp = await tmpdir({ git: true, config: testConfig(true) })
    await Instance.provide({
      directory: tmp.path,
      fn: () => run(realTurn({ agent: "compaction", marker, settleMs: 8000 })),
    })
    expect(fmRowsContaining(marker)).toBe(0)
  },
  120000,
)
