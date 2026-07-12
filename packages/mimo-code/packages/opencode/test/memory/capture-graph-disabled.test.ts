import { afterAll, expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"
import { Log } from "../../src/util"
import { testConfig, run, realTurn, hasCreds, fmRowsContaining, fmGraphNodesContaining } from "./capture-real-harness"

void Log.init({ print: false })

afterAll(async () => {
  await Instance.disposeAll()
})

test.skipIf(!hasCreds)(
  "memory.graph.enabled=false captures the turn but extracts no graph",
  async () => {
    const marker = `nographmark${Date.now()}`
    await using tmp = await tmpdir({ git: true, config: testConfig(true, { enabled: false }) })
    await Instance.provide({
      directory: tmp.path,
      fn: () =>
        run(
          realTurn({
            agent: "build",
            marker,
            settleMs: 8000,
            text: `Reply with exactly one short sentence that contains the token ${marker} verbatim and mentions that Quxfoo Prime maintains the widgetatron-nine project.`,
          }),
        ),
    })
    expect(fmRowsContaining(marker)).toBeGreaterThan(0)
    expect(fmGraphNodesContaining("widgetatron-nine")).toBe(0)
  },
  120000,
)
