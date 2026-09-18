import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Cause, Effect, Layer } from "effect"
import { afterEach, describe, expect } from "bun:test"
import path from "path"
import { pathToFileURL } from "url"
import fs from "fs/promises"
import { createHash } from "crypto"
import type { Permission } from "../../src/permission"
import type { Tool } from "../../src/tool"
import { Instance } from "../../src/project/instance"
import { SkillTool } from "../../src/tool/skill"
import { ToolRegistry } from "../../src/tool"
import { provideTmpdirInstance } from "../fixture/fixture"
import { SessionID, MessageID } from "../../src/session/schema"
import { testEffect } from "../lib/effect"
import {
  bindMemorySessionClient,
  registerManagedMcpClient,
  unbindMemorySessionClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"

const baseCtx: Omit<Tool.Context, "ask"> = {
  sessionID: SessionID.make("ses_test"),
  messageID: MessageID.make(""),
  callID: "",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [],
  metadata: () => Effect.void,
}

afterEach(async () => {
  await Instance.disposeAll()
})

const node = CrossSpawnSpawner.defaultLayer

const it = testEffect(Layer.mergeAll(ToolRegistry.defaultLayer, node))

async function createPublishedOpenClankSkill(dir: string) {
  const skillDir = path.join(dir, "skills", "general", "cached-openclank")
  const snapshot = `---
name: cached-openclank
skill_id: cached-id
revision: 1
content_hash: cached-hash-1
description: Cached Open Clank skill.
owner: alice
status: published
---

# Cached body
`
  const revision = "_revisions/00000001-cached-hash-1.md"
  const mode = 0o644
  const manifest = {
    version: 2,
    files: {
      "SKILL.md": {
        sha256: createHash("sha256").update(snapshot).digest("hex"),
        size: Buffer.byteLength(snapshot),
        mode,
      },
    },
  }
  const manifestText = JSON.stringify(manifest)
  const bundleSHA = createHash("sha256").update(manifestText).digest("hex")
  const bundleRoot = `_revisions/00000001-cached-hash-1-${bundleSHA}.bundle`
  await Promise.all([
    Bun.write(path.join(skillDir, "SKILL.md"), snapshot),
    Bun.write(path.join(skillDir, revision), snapshot),
    Bun.write(path.join(skillDir, bundleRoot, "SKILL.md"), snapshot),
  ])
  await fs.chmod(path.join(skillDir, bundleRoot, "SKILL.md"), mode)
  await Bun.write(path.join(skillDir, bundleRoot, "_manifest.json"), manifestText)
  await Bun.write(
    path.join(skillDir, "_lifecycle.json"),
    JSON.stringify({
      skill_id: "cached-id",
      owner: "alice",
      head_revision: 1,
      head_hash: "cached-hash-1",
      published: {
        skill_id: "cached-id",
        owner: "alice",
        revision: 1,
        content_hash: "cached-hash-1",
        snapshot: revision,
        snapshot_sha256: createHash("sha256").update(snapshot).digest("hex"),
        bundle_root: bundleRoot,
        bundle_manifest: `${bundleRoot}/_manifest.json`,
        bundle_sha256: bundleSHA,
        bundle_version: 2,
      },
    }),
  )
}

describe("tool.skill", () => {
  it.live("execute returns skill content block with files", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          const skill = path.join(dir, ".mimocode", "skill", "tool-skill")
          yield* Effect.promise(() =>
            Bun.write(
              path.join(skill, "SKILL.md"),
              `---
name: tool-skill
description: Skill for tool tests.
---

# Tool Skill

Use this skill.
`,
            ),
          )
          yield* Effect.promise(() => Bun.write(path.join(skill, "scripts", "demo.txt"), "demo"))

          const home = process.env.HOME
          const userProfile = process.env.USERPROFILE
          process.env.HOME = dir
          process.env.USERPROFILE = dir
          yield* Effect.addFinalizer(() =>
            Effect.sync(() => {
              process.env.HOME = home
              process.env.USERPROFILE = userProfile
            }),
          )

          const registry = yield* ToolRegistry.Service
          const agent = { name: "build", mode: "primary" as const, permission: [], options: {} }
          const tool = (yield* registry.tools({
            providerID: "opencode" as any,
            modelID: "gpt-5" as any,
            agent,
          })).find((tool) => tool.id === SkillTool.id)
          if (!tool) throw new Error("Skill tool not found")

          const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
          const ctx: Tool.Context = {
            ...baseCtx,
            ask: (req) =>
              Effect.sync(() => {
                requests.push(req)
              }),
          }

          const result = yield* tool.execute({ name: "tool-skill" }, ctx)
          const file = path.resolve(skill, "scripts", "demo.txt")

          expect(requests.length).toBe(1)
          expect(requests[0].permission).toBe("skill")
          expect(requests[0].patterns).toContain("tool-skill")
          expect(requests[0].always).toContain("tool-skill")
          expect(result.metadata.dir).toBe(skill)
          expect(result.output).toContain(`<skill_content name="tool-skill">`)
          expect(result.output).toContain(`Base directory for this skill: ${pathToFileURL(skill).href}`)
          expect(result.output).toContain(`<file>${file}</file>`)
        }),
      { git: true },
    ),
  )

  it.live("a built-in workflow name redirects to the workflow tool, not a dead-end error", () =>
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const registry = yield* ToolRegistry.Service
          const agent = { name: "build", mode: "primary" as const, permission: [], options: {} }
          const tool = (yield* registry.tools({
            providerID: "opencode" as any,
            modelID: "gpt-5" as any,
            agent,
          })).find((tool) => tool.id === SkillTool.id)
          if (!tool) throw new Error("Skill tool not found")
          const ctx: Tool.Context = { ...baseCtx, ask: () => Effect.void }
          const exit = yield* Effect.exit(tool.execute({ name: "fact-check" }, ctx))
          expect(exit._tag).toBe("Failure")
          const msg = exit._tag === "Failure" ? Cause.pretty(exit.cause) : ""
          expect(msg).toContain("built-in WORKFLOW")
          expect(msg).toContain("workflow tool")
          expect(msg).toContain('name: "fact-check"')
        }),
      { git: true },
    ),
  )

  it.live("refuses a cached Open Clank skill after demotion, transfer, or republish", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.acquireUseRelease(
          Effect.sync(() => {
            const previousDir = process.env.OPEN_CLANK_DATA_DIR
            const previousSkills = process.env.OPEN_CLANK_SKILLS_DIR
            const previousOwner = process.env.OPEN_CLANK_OWNER
            process.env.OPEN_CLANK_DATA_DIR = path.join(dir, "owner-runtime")
            process.env.OPEN_CLANK_SKILLS_DIR = path.join(dir, "skills")
            process.env.OPEN_CLANK_OWNER = "alice"
            return { previousDir, previousSkills, previousOwner }
          }),
          () =>
            Effect.gen(function* () {
              yield* Effect.promise(() => createPublishedOpenClankSkill(dir))
              let canonical = {
                active: true,
                owner: "alice",
                revision: 1,
                contentHash: "cached-hash-1",
              }
              const clientName = "lifetools_cached_skill"
              const client = {
                callTool: async (request: any) => {
                  const args = request.arguments
                  const ok =
                    canonical.active &&
                    canonical.owner === "alice" &&
                    args.skill_id === "cached-id" &&
                    args.revision === canonical.revision &&
                    args.content_hash === canonical.contentHash
                  return {
                    content: [{
                      type: "text",
                      text: JSON.stringify(
                        ok
                          ? {
                              ok: true,
                              name: "cached-openclank",
                              skill_id: "cached-id",
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
              bindMemorySessionClient(baseCtx.sessionID, clientName, "alice", "global")
              yield* Effect.addFinalizer(() =>
                Effect.sync(() => {
                  unbindMemorySessionClient(baseCtx.sessionID)
                  unregisterManagedMcpClient(clientName, client)
                }),
              )

              const registry = yield* ToolRegistry.Service
              const agent = { name: "build", mode: "primary" as const, permission: [], options: {} }
              const tool = (yield* registry.tools({
                providerID: "opencode" as any,
                modelID: "gpt-5" as any,
                agent,
              })).find((item) => item.id === SkillTool.id)
              if (!tool) throw new Error("Skill tool not found")
              const ctx: Tool.Context = { ...baseCtx, ask: () => Effect.void }

              const first = yield* tool.execute({ name: "cached-openclank" }, ctx)
              expect(first.output).toContain("# Cached body")

              canonical = { ...canonical, active: false }
              let exit = yield* Effect.exit(tool.execute({ name: "cached-openclank" }, ctx))
              expect(exit._tag).toBe("Failure")
              expect(exit._tag === "Failure" ? Cause.pretty(exit.cause) : "").toContain(
                "could not be revalidated",
              )

              canonical = { ...canonical, active: true, owner: "bob" }
              exit = yield* Effect.exit(tool.execute({ name: "cached-openclank" }, ctx))
              expect(exit._tag).toBe("Failure")

              canonical = {
                ...canonical,
                owner: "alice",
                revision: 2,
                contentHash: "cached-hash-2",
              }
              exit = yield* Effect.exit(tool.execute({ name: "cached-openclank" }, ctx))
              expect(exit._tag).toBe("Failure")
            }),
          ({ previousDir, previousSkills, previousOwner }) =>
            Effect.sync(() => {
              if (previousDir === undefined) delete process.env.OPEN_CLANK_DATA_DIR
              else process.env.OPEN_CLANK_DATA_DIR = previousDir
              if (previousSkills === undefined) delete process.env.OPEN_CLANK_SKILLS_DIR
              else process.env.OPEN_CLANK_SKILLS_DIR = previousSkills
              if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
              else process.env.OPEN_CLANK_OWNER = previousOwner
            }),
        ),
      { git: true, config: { skills: { paths: ["skills"] } } },
    ),
  )
})
