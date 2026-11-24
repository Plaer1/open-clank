import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { eq } from "drizzle-orm"
import { Bus } from "../../src/bus"
import { Config } from "../../src/config"
import { AutoDream } from "../../src/session/auto-dream"
import { Session as SessionNs } from "../../src/session"
import { SessionTable } from "../../src/session/session.sql"
import { ProjectTable } from "../../src/project/project.sql"
import { Database } from "../../src/storage"
import { Log } from "../../src/util"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"

void Log.init({ print: false })

const it = testEffect(
  Layer.mergeAll(CrossSpawnSpawner.defaultLayer, Bus.defaultLayer, Config.defaultLayer, SessionNs.defaultLayer),
)

const DAY_MS = 24 * 60 * 60 * 1000

// Both auto-runs refuse to fire on a project younger than their interval, so a
// backdated top-level session is what makes these cases non-vacuous: the interval
// brake is released, and the ONLY remaining reason to return false is the switch.
// 60 days clears dream (7d) and distill (30d) alike.
const seedOldProject = Effect.fn("seedOldProject")(function* () {
  const ssn = yield* SessionNs.Service
  const info = yield* ssn.create({})
  yield* Effect.sync(() =>
    Database.use((db) =>
      db
        .update(SessionTable)
        .set({ time_created: Date.now() - 60 * DAY_MS })
        .where(eq(SessionTable.id, info.id))
        .run(),
    ),
  )
  return info
})

// W6. Dream rewrites project memory; distill mines memory for patterns and then
// auto-produces artifacts in the background. Neither may run while memory writing
// is off.
//
// Only the DISABLED direction is asserted here, and deliberately so: the enabled
// direction additionally passes through a module-level 10s spawn throttle
// (lastDreamSpawnTime / lastDistillSpawnTime) that is process-global and armed by
// any other test file whose session-create path evaluates these same predicates.
// An "enabled → true" control is therefore order-dependent across files. The
// enabled polarity is pinned instead by test/config/memory-disable-write.test.ts
// (accessor level, where `disable_write: false` and an absent field both mean
// enabled). The switch check is the first statement in both functions, so the
// assertions below hold regardless of throttle state.
describe("shouldAutoDream × memory write switch", () => {
  it.live(
    "disable_write: true → no auto dream, even on a project old enough to be due",
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const info = yield* seedOldProject()
          const cfg = yield* (yield* Config.Service).get()
          expect(yield* AutoDream.shouldAutoDream(cfg, info.projectID)).toBe(false)
        }),
      { outsideGit: true, config: { memory: { disable_write: true } } },
    ),
  )

  // dream.auto is opt-in upstream, so this pins that the switch overrides the
  // feature's own enable flag rather than merely coinciding with it.
  it.live(
    "disable_write: true beats an explicit dream.auto: true",
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const info = yield* seedOldProject()
          const cfg = yield* (yield* Config.Service).get()
          expect(yield* AutoDream.shouldAutoDream(cfg, info.projectID)).toBe(false)
        }),
      { outsideGit: true, config: { dream: { auto: true }, memory: { disable_write: true } } },
    ),
  )
})

describe("shouldAutoDistill × memory write switch", () => {
  it.live(
    "disable_write: true → no auto distill, even on a project old enough to be due",
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const info = yield* seedOldProject()
          const cfg = yield* (yield* Config.Service).get()
          expect(yield* AutoDream.shouldAutoDistill(cfg, info.projectID)).toBe(false)
        }),
      { outsideGit: true, config: { memory: { disable_write: true } } },
    ),
  )

  it.live(
    "disable_write: true beats an explicit distill.auto: true",
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const info = yield* seedOldProject()
          const cfg = yield* (yield* Config.Service).get()
          expect(yield* AutoDream.shouldAutoDistill(cfg, info.projectID)).toBe(false)
        }),
      { outsideGit: true, config: { distill: { auto: true }, memory: { disable_write: true } } },
    ),
  )
})


it.live("automatic dream cooldown and last run are project scoped", provideTmpdirInstance(() => Effect.gen(function* () {
  const ssn = yield* SessionNs.Service
  const older = yield* seedOldProject()
  const other = yield* ssn.create({ title: AutoDream.AUTO_DREAM_TITLE })
  const otherProject = (`project-${crypto.randomUUID()}`) as typeof older.projectID
  yield* Effect.sync(() => Database.use((db) => {
    db.insert(ProjectTable).values({ id: otherProject, worktree: `/tmp/${otherProject}`, sandboxes: [], time_created: Date.now(), time_updated: Date.now() }).run()
    db.update(SessionTable).set({ project_id: otherProject }).where(eq(SessionTable.id, other.id)).run()
  }))
  const cfg = yield* (yield* Config.Service).get()
  const enabled = { ...cfg, dream: { ...cfg.dream, auto: true }, memory: { ...cfg.memory, disable_write: false } }
  // A recent run in project B must neither suppress project A nor share its cooldown.
  expect(yield* AutoDream.shouldAutoDream(enabled, otherProject)).toBe(false)
  expect(yield* AutoDream.shouldAutoDream(enabled, older.projectID)).toBe(true)
}), { outsideGit: true }))
