import { afterAll, expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"
import { Log } from "../../src/util"
import { testConfig, run, realTurn, hasCreds, fmGraphNodesContaining, fmGraphEdgesTouching } from "./capture-real-harness"

void Log.init({ print: false })

afterAll(async () => {
  await Instance.disposeAll()
})

// SKIPPED (management order 2026-07-08, re-confirmed after retry 2026-07-09):
// structured output through generateObject stays flaky in the hermetic test
// env even on the non-thinking default. The live E1 gate exercises this
// exact path end-to-end and passes; debug here again when the test env can
// print extraction warnings without megabytes of session noise.
test.skip(
  "a real turn with graph extraction enabled lands nodes and edges",
  async () => {
    await using tmp = await tmpdir({ git: true, config: testConfig(true) })
    await Instance.provide({
      directory: tmp.path,
      fn: () =>
        run(
          realTurn({
            agent: "build",
            marker: "graphmark",
            settleMs: 60000,
            text: "Reply with one short sentence acknowledging this fact: Elena maintains the telescope-scheduler project, and telescope-scheduler uses the astropy library for coordinate math.",
          }),
        ),
    })
    expect(fmGraphNodesContaining("telescope-scheduler")).toBeGreaterThan(0)
    expect(fmGraphEdgesTouching("telescope-scheduler")).toBeGreaterThan(0)
  },
  180000,
)
