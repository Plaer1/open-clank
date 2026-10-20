import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { Skill } from "../../src/skill"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideInstance, provideTmpdirInstance, tmpdir } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { withEnv } from "../lib/env"
import path from "path"
import fs from "fs/promises"
import { createHash } from "crypto"
import {
  bindMemorySessionClient,
  registerManagedMcpClient,
  unbindMemorySessionClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"

withEnv({ MIMOCODE_DISABLE_COMPOSE_SKILLS: "true", MIMOCODE_DISABLE_BUILTIN_SKILLS: "true" })

const node = CrossSpawnSpawner.defaultLayer

const it = testEffect(Layer.mergeAll(Skill.defaultLayer, node))

async function createRevisionBundle(
  skillDir: string,
  revision: number,
  contentHash: string,
  files: Record<string, string>,
) {
  const mode = 0o644
  const manifest = {
    version: 2,
    files: Object.fromEntries(
      Object.entries(files)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([relative, content]) => [
          relative,
          {
            sha256: createHash("sha256").update(content).digest("hex"),
            size: Buffer.byteLength(content),
            mode,
          },
        ]),
    ),
  }
  const manifestText = JSON.stringify(manifest)
  const bundleSHA256 = createHash("sha256").update(manifestText).digest("hex")
  const bundleRoot = `_revisions/${String(revision).padStart(8, "0")}-${contentHash}-${bundleSHA256}.bundle`
  await Promise.all(
    Object.entries(files).map(async ([relative, content]) => {
      const target = path.join(skillDir, bundleRoot, ...relative.split("/"))
      await Bun.write(target, content)
      await fs.chmod(target, mode)
    }),
  )
  await Bun.write(path.join(skillDir, bundleRoot, "_manifest.json"), manifestText)
  return {
    bundle_root: bundleRoot,
    bundle_manifest: `${bundleRoot}/_manifest.json`,
    bundle_sha256: bundleSHA256,
    bundle_version: 2,
  }
}

async function createGlobalSkill(homeDir: string) {
  const skillDir = path.join(homeDir, ".claude", "skills", "global-test-skill")
  await fs.mkdir(skillDir, { recursive: true })
  await Bun.write(
    path.join(skillDir, "SKILL.md"),
    `---
name: global-test-skill
description: A global skill from ~/.claude/skills for testing.
---

# Global Test Skill

This skill is loaded from the global home directory.
`,
  )
}

async function createPublishedOpenClankSkill(
  dataDir: string,
  name: string,
  description: string,
  owner?: string,
) {
  const skillDir = path.join(dataDir, "skills", "general", name)
  const skillID = `stable-${name}`
  const contentHash = `hash-${name}`
  const snapshot = `---
name: ${name}
skill_id: ${skillID}
revision: 1
content_hash: ${contentHash}
description: ${description}
${owner ? `owner: ${owner}\n` : ""}status: published
---
`
  const revision = `_revisions/00000001-${contentHash}.md`
  await Promise.all([
    Bun.write(path.join(skillDir, "SKILL.md"), snapshot),
    Bun.write(path.join(skillDir, revision), snapshot),
  ])
  const bundle = await createRevisionBundle(skillDir, 1, contentHash, {
    "SKILL.md": snapshot,
  })
  await Bun.write(
    path.join(skillDir, "_lifecycle.json"),
    JSON.stringify({
      skill_id: skillID,
      owner: owner ?? "",
      head_revision: 1,
      head_hash: contentHash,
      published: {
        skill_id: skillID,
        owner: owner ?? "",
        revision: 1,
        content_hash: contentHash,
        snapshot: revision,
        snapshot_sha256: createHash("sha256").update(snapshot).digest("hex"),
        publisher: owner ? `user:${owner}` : "user:local",
        ...bundle,
      },
    }),
  )
}

const withHome = <A, E, R>(home: string, self: Effect.Effect<A, E, R>) =>
  Effect.acquireUseRelease(
    Effect.sync(() => {
      const prev = process.env.HOME
      const prevUserProfile = process.env.USERPROFILE
      process.env.HOME = home
      process.env.USERPROFILE = home
      return { prev, prevUserProfile }
    }),
    () => self,
    ({ prev, prevUserProfile }) =>
      Effect.sync(() => {
        process.env.HOME = prev
        process.env.USERPROFILE = prevUserProfile
      }),
  )

describe("skill", () => {
  it.live("discovers skills from .mimocode/skill/ directory", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".mimocode", "skill", "test-skill", "SKILL.md"),
              `---
name: test-skill
description: A test skill for verification.
---

# Test Skill

Instructions here.
`,
            ),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(1)
          const item = list.find((x) => x.name === "test-skill")
          expect(item).toBeDefined()
          expect(item!.description).toBe("A test skill for verification.")
          expect(item!.location).toContain(path.join("skill", "test-skill", "SKILL.md"))
        }),
      { git: true },
    ),
  )

  it.live("returns skill directories from Skill.dirs", () =>
    provideTmpdirInstance(
      (dir) =>
        withHome(
          dir,
          Effect.gen(function* () {
            yield* Effect.promise(() =>
              Bun.write(
                path.join(dir, ".mimocode", "skill", "dir-skill", "SKILL.md"),
                `---
name: dir-skill
description: Skill for dirs test.
---

# Dir Skill
`,
              ),
            )

            const skill = yield* Skill.Service
            const dirs = yield* skill.dirs()
            expect(dirs).toContain(path.join(dir, ".mimocode", "skill", "dir-skill"))
            expect(dirs.length).toBe(1)
          }),
        ),
      { git: true },
    ),
  )

  it.live("discovers multiple skills from .mimocode/skill/ directory", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Promise.all([
              Bun.write(
                path.join(dir, ".mimocode", "skill", "skill-one", "SKILL.md"),
                `---
name: skill-one
description: First test skill.
---

# Skill One
`,
              ),
              Bun.write(
                path.join(dir, ".mimocode", "skill", "skill-two", "SKILL.md"),
                `---
name: skill-two
description: Second test skill.
---

# Skill Two
`,
              ),
            ]),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(2)
          expect(list.find((x) => x.name === "skill-one")).toBeDefined()
          expect(list.find((x) => x.name === "skill-two")).toBeDefined()
        }),
      { git: true },
    ),
  )

  it.live("keeps staged or hidden skill bodies out of every runtime surface", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".mimocode", "skill", "staged-remote", "SKILL.md"),
              `---
name: staged-remote
description: Untrusted remote instructions.
status: staged
hidden: true
source: remote
---

# This body must not load.
`,
            ),
          )
          const skill = yield* Skill.Service
          expect(yield* skill.get("staged-remote")).toBeUndefined()
          expect(yield* skill.all()).toEqual([])
          expect(yield* skill.available()).toEqual([])
        }),
      { git: true },
    ),
  )

  it.live("frontmatter-only Open Clank skills remain staged", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previous = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_DATA_DIR = dir
            process.env.OPEN_CLANK_OWNER = ""
            return { previous, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              yield* Effect.promise(() =>
                Promise.all([
                  Bun.write(
                    path.join(dir, "skills", "general", "draft-skill", "SKILL.md"),
                    `---
name: draft-skill
description: Staged Open Clank skill.
status: draft
---

# Draft Skill
`,
                  ),
                  Bun.write(
                    path.join(dir, "skills", "general", "published-skill", "SKILL.md"),
                    `---
name: published-skill
description: Published Open Clank skill.
status: published
---

# Published Skill
`,
                  ),
                ]),
              )
              const skill = yield* Skill.Service
              const names = (yield* skill.all()).map((item) => item.name)
              expect(names).toEqual([])
              expect((yield* skill.available()).map((item) => item.name)).toEqual([])
            }),
          ({ previous, previousOwner }) =>
            Effect.sync(() => {
              if (previous === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previous
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("uses the owner-scoped immutable published pointer", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousDir = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_DATA_DIR = dir
            process.env.OPEN_CLANK_OWNER = "alice"
            return { previousDir, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              const aliceDir = path.join(dir, "skills", "general", "alice-skill")
              const revisionDir = path.join(aliceDir, "_revisions")
              const bobDir = path.join(dir, "skills", "general", "bob-skill")
              const publishedSnapshot = `---
name: alice-skill
skill_id: stable-alice
revision: 1
content_hash: published-hash
description: Audited published revision.
owner: alice
status: published
source: imported
source_status: published
source_revision: upstream-r1
platforms: [linux]
requires_toolsets: [bash]
---
`
              const aliceHead = `---
name: alice-skill
skill_id: stable-alice
revision: 2
content_hash: head-hash
description: Unpublished head.
owner: alice
status: draft
---
`
              yield* Effect.promise(() =>
                Promise.all([
                  Bun.write(path.join(aliceDir, "SKILL.md"), aliceHead),
                  Bun.write(
                    path.join(revisionDir, "00000001-published-hash.md"),
                    publishedSnapshot,
                  ),
                  Bun.write(
                    path.join(bobDir, "SKILL.md"),
                    `---
name: bob-skill
skill_id: stable-bob
revision: 1
content_hash: bob-hash
description: Bob private skill.
owner: bob
status: published
---
`,
                  ),
                ]),
              )
              const bundle = yield* Effect.promise(() =>
                createRevisionBundle(aliceDir, 1, "published-hash", {
                  "SKILL.md": publishedSnapshot,
                  "references/guide.txt": "published reference",
                }),
              )
              yield* Effect.promise(() =>
                Bun.write(
                  path.join(aliceDir, "_lifecycle.json"),
                  JSON.stringify({
                    skill_id: "stable-alice",
                    owner: "alice",
                    head_revision: 2,
                    head_hash: "head-hash",
                    attestations: {
                      "published-hash": {
                        skill_id: "stable-alice",
                        revision: 1,
                        content_hash: "published-hash",
                        verdict: "pass",
                        compatible: true,
                        audited_at: 123,
                        ...bundle,
                      },
                    },
                    published: {
                      skill_id: "stable-alice",
                      owner: "alice",
                      revision: 1,
                      content_hash: "published-hash",
                      snapshot: "_revisions/00000001-published-hash.md",
                      snapshot_sha256: createHash("sha256").update(publishedSnapshot).digest("hex"),
                      ...bundle,
                    },
                  }),
                ),
              )
              yield* Effect.promise(() =>
                Bun.write(
                  path.join(aliceDir, "references", "guide.txt"),
                  "swapped live reference",
                ),
              )
              yield* Effect.promise(async () => {
                await createPublishedOpenClankSkill(
                  dir,
                  "foreign-snapshot",
                  "Bob's immutable revision.",
                  "bob",
                )
                const foreignDir = path.join(dir, "skills", "general", "foreign-snapshot")
                await Bun.write(
                  path.join(foreignDir, "SKILL.md"),
                  `---
name: foreign-snapshot
skill_id: stable-foreign-snapshot
revision: 2
content_hash: forged-head-hash
description: Forged Alice head over Bob's published revision.
owner: alice
status: draft
---
`,
                )
                const lifecyclePath = path.join(foreignDir, "_lifecycle.json")
                const lifecycle = JSON.parse(await fs.readFile(lifecyclePath, "utf-8"))
                lifecycle.owner = "bob"
                await Bun.write(lifecyclePath, JSON.stringify(lifecycle))
              })

              const skill = yield* Skill.Service
              const list = yield* skill.all()
              expect(list.map((item) => item.name)).toEqual(["alice-skill"])
              expect(yield* skill.get("foreign-snapshot")).toBeUndefined()
              expect(list[0]?.description).toBe("Audited published revision.")
              expect(list[0]?.skillID).toBe("stable-alice")
              expect(list[0]?.revision).toBe(1)
              expect(list[0]?.owner).toBe("alice")
              expect(list[0]?.status).toBe("published")
              expect(list[0]?.hidden).toBe(false)
              expect(list[0]?.trust).toBe("verified")
              expect(list[0]?.source).toBe("imported")
              expect(list[0]?.sourceStatus).toBe("published")
              expect(list[0]?.sourceRevision).toBe("upstream-r1")
              expect(list[0]?.lastAudit).toBe(123)
              expect(list[0]?.platforms).toEqual(["linux"])
              expect(list[0]?.requiresToolsets).toEqual(["bash"])
              expect(list[0]?.location).toContain(`${path.sep}_revisions${path.sep}`)
              expect(list[0]?.location.endsWith(path.join(".bundle", "SKILL.md"))).toBe(true)
              const pinnedReference = yield* Effect.promise(() =>
                fs.readFile(
                  path.join(
                    path.dirname(list[0]!.location),
                    "references",
                    "guide.txt",
                  ),
                  "utf-8",
                ),
              )
              expect(pinnedReference).toBe("published reference")
              const disclosure = Skill.fmt(list, { verbose: true })
              expect(disclosure).toContain("<source_status>published</source_status>")
              expect(disclosure).toContain("<source_revision>upstream-r1</source_revision>")
              expect(disclosure).toContain("<last_audit>123</last_audit>")
            }),
          ({ previousDir, previousOwner }) =>
            Effect.sync(() => {
              if (previousDir === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousDir
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("requires the complete Open Clank lifecycle identity contract", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousDir = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_DATA_DIR = dir
            process.env.OPEN_CLANK_OWNER = "alice"
            return { previousDir, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              const cases: Array<[string, (state: any) => void]> = [
                ["missing-state-owner", (state) => delete state.owner],
                ["wrong-state-owner", (state) => (state.owner = "bob")],
                ["missing-pointer-owner", (state) => delete state.published.owner],
                ["wrong-pointer-owner", (state) => (state.published.owner = "bob")],
                ["wrong-head-revision", (state) => (state.head_revision = 2)],
                ["wrong-head-hash", (state) => (state.head_hash = "other")],
                ["missing-pointer-id", (state) => delete state.published.skill_id],
                ["wrong-pointer-id", (state) => (state.published.skill_id = "other")],
              ]
              yield* Effect.promise(async () => {
                for (const [name, mutate] of cases) {
                  await createPublishedOpenClankSkill(dir, name, "Must stay inactive.", "alice")
                  const lifecyclePath = path.join(
                    dir,
                    "skills",
                    "general",
                    name,
                    "_lifecycle.json",
                  )
                  const state = JSON.parse(await fs.readFile(lifecyclePath, "utf-8"))
                  mutate(state)
                  await Bun.write(lifecyclePath, JSON.stringify(state))
                }
              })

              const skill = yield* Skill.Service
              expect(yield* skill.all()).toEqual([])
            }),
          ({ previousDir, previousOwner }) =>
            Effect.sync(() => {
              if (previousDir === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousDir
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("refuses a central Open Clank catalogue without an owner binding", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousRoot = process.env.OPEN_CLANK_SKILLS_DIR
            const previousData = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_SKILLS_DIR = path.join(dir, "skills")
            delete process.env.OPEN_CLANK_DATA_DIR
            delete process.env.OPEN_CLANK_OWNER
            return { previousRoot, previousData, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              yield* Effect.promise(() =>
                createPublishedOpenClankSkill(dir, "alice-private", "Must stay owner-bound.", "alice"),
              )
              const skill = yield* Skill.Service
              expect(yield* skill.all()).toEqual([])
            }),
          ({ previousRoot, previousData, previousOwner }) =>
            Effect.sync(() => {
              if (previousRoot === undefined) delete process.env.OPEN_CLANK_SKILLS_DIR
              else process.env.OPEN_CLANK_SKILLS_DIR = previousRoot
              if (previousData === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousData
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("refuses symlink escapes from the explicit Open Clank catalogue", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousRoot = process.env.OPEN_CLANK_SKILLS_DIR
            const previousData = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_SKILLS_DIR = path.join(dir, "skills")
            delete process.env.OPEN_CLANK_DATA_DIR
            delete process.env.OPEN_CLANK_OWNER
            return { previousRoot, previousData, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              yield* Effect.promise(async () => {
                const outside = path.join(dir, "outside")
                await createPublishedOpenClankSkill(outside, "escaped", "Must stay outside.")
                const target = path.join(outside, "skills", "general", "escaped")
                const link = path.join(dir, "skills", "general", "escaped")
                await fs.mkdir(path.dirname(link), { recursive: true })
                await fs.symlink(target, link, "dir")
              })

              const skill = yield* Skill.Service
              expect(yield* skill.all()).toEqual([])
            }),
          ({ previousRoot, previousData, previousOwner }) =>
            Effect.sync(() => {
              if (previousRoot === undefined) delete process.env.OPEN_CLANK_SKILLS_DIR
              else process.env.OPEN_CLANK_SKILLS_DIR = previousRoot
              if (previousData === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousData
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("fails closed for cleared or byte-mismatched Open Clank activation", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousDir = process.env.OPEN_CLANK_DATA_DIR
            process.env.OPEN_CLANK_DATA_DIR = dir
            return previousDir
          }),
          () =>
            Effect.gen(function* () {
              const clearedDir = path.join(dir, "skills", "general", "cleared")
              const changedDir = path.join(dir, "skills", "general", "changed")
              const changedSnapshot = `---
name: changed
skill_id: changed-id
revision: 1
content_hash: changed-hash
description: Changed snapshot.
status: draft
---
`
              yield* Effect.promise(() =>
                Promise.all([
                  Bun.write(
                    path.join(clearedDir, "SKILL.md"),
                    `---
name: cleared
skill_id: cleared-id
revision: 1
content_hash: cleared-hash
description: Must remain inactive.
status: published
---
`,
                  ),
                  Bun.write(
                    path.join(clearedDir, "_lifecycle.json"),
                    JSON.stringify({ skill_id: "cleared-id", published: null }),
                  ),
                  Bun.write(
                    path.join(changedDir, "SKILL.md"),
                    `---
name: changed
skill_id: changed-id
revision: 1
content_hash: changed-hash
description: Changed head.
status: published
---
`,
                  ),
                  Bun.write(
                    path.join(changedDir, "_revisions", "snapshot.md"),
                    changedSnapshot,
                  ),
                  Bun.write(
                    path.join(changedDir, "_lifecycle.json"),
                    JSON.stringify({
                      skill_id: "changed-id",
                      published: {
                        revision: 1,
                        content_hash: "changed-hash",
                        snapshot: "_revisions/snapshot.md",
                        snapshot_sha256: "not-the-snapshot-hash",
                      },
                    }),
                  ),
                ]),
              )
              yield* Effect.promise(() =>
                createPublishedOpenClankSkill(
                  dir,
                  "tampered-bundle",
                  "Bundle bytes must stay immutable.",
                ),
              )
              yield* Effect.promise(async () => {
                const tamperedDir = path.join(
                  dir,
                  "skills",
                  "general",
                  "tampered-bundle",
                )
                const lifecycle = JSON.parse(
                  await fs.readFile(
                    path.join(tamperedDir, "_lifecycle.json"),
                    "utf-8",
                  ),
                )
                await Bun.write(
                  path.join(
                    tamperedDir,
                    lifecycle.published.bundle_root,
                    "SKILL.md",
                  ),
                  "tampered bundle bytes",
                )
              })

              const skill = yield* Skill.Service
              expect(yield* skill.all()).toEqual([])
            }),
          (previousDir) =>
            Effect.sync(() => {
              if (previousDir === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousDir
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("chooses the same duplicate winner regardless of scan order", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          const root = path.join(dir, "duplicates")
          yield* Effect.promise(() =>
            Promise.all(
              [5, 1, 7, 3, 0, 6, 2, 4].map((index) => {
                const leaf = String(index).padStart(3, "0")
                return Bun.write(
                  path.join(root, leaf, "SKILL.md"),
                  `---
name: deterministic
description: winner-${leaf}
---
`,
                )
              }),
            ),
          )
          const skill = yield* Skill.Service
          for (let run = 0; run < 100; run++) {
            yield* skill.reload()
            const item = (yield* skill.all()).find((row) => row.name === "deterministic")
            expect(item?.description).toBe("winner-000")
          }
        }),
      { git: true, config: { skills: { paths: ["duplicates"] } } },
    ),
  )

  it.live("prefers the explicit Open Clank owner catalogue over project duplicates", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previous = process.env.OPEN_CLANK_DATA_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_DATA_DIR = dir
            process.env.OPEN_CLANK_OWNER = ""
            return { previous, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              yield* Effect.promise(() =>
                Promise.all([
                  createPublishedOpenClankSkill(dir, "shared", "owner catalogue"),
                  Bun.write(
                    path.join(dir, ".agents", "skills", "shared", "SKILL.md"),
                    `---
name: shared
description: project duplicate
---
`,
                  ),
                ]),
              )
              const skill = yield* Skill.Service
              expect((yield* skill.get("shared"))?.description).toBe("owner catalogue")
            }),
          ({ previous, previousOwner }) =>
            Effect.sync(() => {
              if (previous === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previous
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )

  it.live("revalidates cached usage across demotion, transfer, and republish", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousDir = process.env.OPEN_CLANK_DATA_DIR
            process.env.OPEN_CLANK_DATA_DIR = dir
            return previousDir
          }),
          () =>
            Effect.promise(async () => {
              const clientName = "lifetools_skill_usage"
              const sessionID = "ses_skill_usage"
              let canonical = {
                active: true,
                name: "renamable",
                skillID: "stable-id",
                owner: "alice",
                revision: 7,
                contentHash: "hash-7",
              }
              let durableEvents = 0
              const client = {
                callTool: async (request: any) => {
                  const args = request.arguments
                  const ok =
                    canonical.active &&
                    canonical.owner === "alice" &&
                    args.name === canonical.name &&
                    args.skill_id === canonical.skillID &&
                    args.revision === canonical.revision &&
                    args.content_hash === canonical.contentHash
                  if (ok) durableEvents++
                  return {
                    content: [{
                      type: "text",
                      text: JSON.stringify(
                        ok
                          ? {
                              ok: true,
                              name: canonical.name,
                              skill_id: canonical.skillID,
                              owner: canonical.owner,
                              revision: canonical.revision,
                              content_hash: canonical.contentHash,
                            }
                          : { ok: false },
                      ),
                    }],
                  }
                },
              } as any
              registerManagedMcpClient(clientName, client)
              bindMemorySessionClient(sessionID, clientName, "alice", "global")
              const cached = {
                name: "renamable",
                skillID: "stable-id",
                revision: 7,
                contentHash: "hash-7",
                owner: "alice",
                location: path.join(dir, "skills", "general", "renamable", "SKILL.md"),
              }
              try {
                expect(await Skill._writeUsage(cached, sessionID)).toBe(true)
                expect(durableEvents).toBe(1)

                canonical = { ...canonical, active: false }
                expect(await Skill._writeUsage(cached, sessionID)).toBe(false)

                canonical = { ...canonical, active: true, owner: "bob" }
                expect(await Skill._writeUsage(cached, sessionID)).toBe(false)

                canonical = {
                  ...canonical,
                  owner: "alice",
                  revision: 8,
                  contentHash: "hash-8",
                }
                expect(await Skill._writeUsage(cached, sessionID)).toBe(false)
                expect(
                  await Skill._writeUsage(
                    {
                      ...cached,
                      revision: 8,
                      contentHash: "hash-8",
                    },
                    sessionID,
                  ),
                ).toBe(true)
                expect(durableEvents).toBe(2)
              } finally {
                unbindMemorySessionClient(sessionID)
                unregisterManagedMcpClient(clientName, client)
              }
            }),
          (previousDir) =>
            Effect.sync(() => {
              if (previousDir === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousDir
            }),
        ),
      { git: true },
    ),
  )

  it.live("skips skills with missing frontmatter", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".mimocode", "skill", "no-frontmatter", "SKILL.md"),
              `# No Frontmatter

Just some content without YAML frontmatter.
`,
            ),
          )

          const skill = yield* Skill.Service
          expect(yield* skill.all()).toEqual([])
        }),
      { git: true },
    ),
  )

  it.live("discovers skills from .claude/skills/ directory", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".claude", "skills", "claude-skill", "SKILL.md"),
              `---
name: claude-skill
description: A skill in the .claude/skills directory.
---

# Claude Skill
`,
            ),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(1)
          const item = list.find((x) => x.name === "claude-skill")
          expect(item).toBeDefined()
          expect(item!.location).toContain(path.join(".claude", "skills", "claude-skill", "SKILL.md"))
        }),
      { git: true },
    ),
  )

  it.live("discovers global skills from ~/.claude/skills/ directory", () =>
    Effect.gen(function* () {
      const tmp = yield* Effect.acquireRelease(
        Effect.promise(() => tmpdir({ git: true })),
        (tmp) => Effect.promise(() => tmp[Symbol.asyncDispose]()),
      )

      yield* withHome(
        tmp.path,
        Effect.gen(function* () {
          yield* Effect.promise(() => createGlobalSkill(tmp.path))
          yield* Effect.gen(function* () {
            const skill = yield* Skill.Service
            const list = yield* skill.all()
            expect(list.length).toBe(1)
            expect(list[0].name).toBe("global-test-skill")
            expect(list[0].description).toBe("A global skill from ~/.claude/skills for testing.")
            expect(list[0].location).toContain(path.join(".claude", "skills", "global-test-skill", "SKILL.md"))
          }).pipe(provideInstance(tmp.path))
        }),
      )
    }),
  )

  it.live("returns empty array when no skills exist", () =>
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const skill = yield* Skill.Service
          expect(yield* skill.all()).toEqual([])
        }),
      { git: true },
    ),
  )

  it.live("discovers skills from .agents/skills/ directory", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".agents", "skills", "agent-skill", "SKILL.md"),
              `---
name: agent-skill
description: A skill in the .agents/skills directory.
---

# Agent Skill
`,
            ),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(1)
          const item = list.find((x) => x.name === "agent-skill")
          expect(item).toBeDefined()
          expect(item!.location).toContain(path.join(".agents", "skills", "agent-skill", "SKILL.md"))
        }),
      { git: true },
    ),
  )

  it.live("discovers global skills from ~/.agents/skills/ directory", () =>
    Effect.gen(function* () {
      const tmp = yield* Effect.acquireRelease(
        Effect.promise(() => tmpdir({ git: true })),
        (tmp) => Effect.promise(() => tmp[Symbol.asyncDispose]()),
      )

      yield* withHome(
        tmp.path,
        Effect.gen(function* () {
          const skillDir = path.join(tmp.path, ".agents", "skills", "global-agent-skill")
          yield* Effect.promise(() => fs.mkdir(skillDir, { recursive: true }))
          yield* Effect.promise(() =>
            Bun.write(
              path.join(skillDir, "SKILL.md"),
              `---
name: global-agent-skill
description: A global skill from ~/.agents/skills for testing.
---

# Global Agent Skill

This skill is loaded from the global home directory.
`,
            ),
          )

          yield* Effect.gen(function* () {
            const skill = yield* Skill.Service
            const list = yield* skill.all()
            expect(list.length).toBe(1)
            expect(list[0].name).toBe("global-agent-skill")
            expect(list[0].description).toBe("A global skill from ~/.agents/skills for testing.")
            expect(list[0].location).toContain(path.join(".agents", "skills", "global-agent-skill", "SKILL.md"))
          }).pipe(provideInstance(tmp.path))
        }),
      )
    }),
  )

  it.live("discovers skills from .codex/skills/ directory", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Bun.write(
              path.join(dir, ".codex", "skills", "codex-skill", "SKILL.md"),
              `---
name: codex-skill
description: A skill in the .codex/skills directory.
---

# Codex Skill
`,
            ),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(1)
          const item = list.find((x) => x.name === "codex-skill")
          expect(item).toBeDefined()
          expect(item!.description).toBe("A skill in the .codex/skills directory.")
          expect(item!.location).toContain(path.join(".codex", "skills", "codex-skill", "SKILL.md"))
        }),
      { git: true },
    ),
  )

  it.live("discovers global skills from ~/.codex/skills/ directory", () =>
    Effect.gen(function* () {
      const tmp = yield* Effect.acquireRelease(
        Effect.promise(() => tmpdir({ git: true })),
        (tmp) => Effect.promise(() => tmp[Symbol.asyncDispose]()),
      )

      yield* withHome(
        tmp.path,
        Effect.gen(function* () {
          const skillDir = path.join(tmp.path, ".codex", "skills", "global-codex-skill")
          yield* Effect.promise(() => fs.mkdir(skillDir, { recursive: true }))
          yield* Effect.promise(() =>
            Bun.write(
              path.join(skillDir, "SKILL.md"),
              `---
name: global-codex-skill
description: A global skill from ~/.codex/skills for testing.
---

# Global Codex Skill

This skill is loaded from the global home directory.
`,
            ),
          )

          yield* Effect.gen(function* () {
            const skill = yield* Skill.Service
            const list = yield* skill.all()
            expect(list.length).toBe(1)
            expect(list[0].name).toBe("global-codex-skill")
            expect(list[0].description).toBe("A global skill from ~/.codex/skills for testing.")
            expect(list[0].location).toContain(path.join(".codex", "skills", "global-codex-skill", "SKILL.md"))
          }).pipe(provideInstance(tmp.path))
        }),
      )
    }),
  )

  it.live("discovers skills from both .claude/skills/ and .agents/skills/", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Promise.all([
              Bun.write(
                path.join(dir, ".claude", "skills", "claude-skill", "SKILL.md"),
                `---
name: claude-skill
description: A skill in the .claude/skills directory.
---

# Claude Skill
`,
              ),
              Bun.write(
                path.join(dir, ".agents", "skills", "agent-skill", "SKILL.md"),
                `---
name: agent-skill
description: A skill in the .agents/skills directory.
---

# Agent Skill
`,
              ),
            ]),
          )

          const skill = yield* Skill.Service
          const list = yield* skill.all()
          expect(list.length).toBe(2)
          expect(list.find((x) => x.name === "claude-skill")).toBeDefined()
          expect(list.find((x) => x.name === "agent-skill")).toBeDefined()
        }),
      { git: true },
    ),
  )

  it.live("properly resolves directories that skills live in", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() =>
            Promise.all([
              Bun.write(
                path.join(dir, ".claude", "skills", "claude-skill", "SKILL.md"),
                `---
name: claude-skill
description: A skill in the .claude/skills directory.
---

# Claude Skill
`,
              ),
              Bun.write(
                path.join(dir, ".agents", "skills", "agent-skill", "SKILL.md"),
                `---
name: agent-skill
description: A skill in the .agents/skills directory.
---

# Agent Skill
`,
              ),
              Bun.write(
                path.join(dir, ".mimocode", "skill", "agent-skill", "SKILL.md"),
                `---
name: opencode-skill
description: A skill in the .mimocode/skill directory.
---

# OpenCode Skill
`,
              ),
              Bun.write(
                path.join(dir, ".mimocode", "skills", "agent-skill", "SKILL.md"),
                `---
name: opencode-skill
description: A skill in the .mimocode/skills directory.
---

# OpenCode Skill
`,
              ),
            ]),
          )

          const skill = yield* Skill.Service
          expect((yield* skill.dirs()).length).toBe(4)
        }),
      { git: true },
    ),
  )
})
