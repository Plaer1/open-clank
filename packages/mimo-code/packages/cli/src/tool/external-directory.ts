import path from "path"
import { Effect, Option } from "effect"
import { EffectLogger } from "@/effect"
import { InstanceState } from "@/effect"
import { Global } from "@/global"
import { Config } from "@/config"
import { isMemoryWriteEnabled } from "@/memory/write-gate"
import type * as Tool from "./tool"
import { Instance } from "../project/instance"
import { ProjectID } from "../project/schema"
import { assertMemoryWriteAllowed, assertAgentWriteSandbox } from "./memory-path-guard"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { existsSync, realpathSync } from "node:fs"

type Kind = "file" | "directory"

type Options = {
  bypass?: boolean
  kind?: Kind
}

function canonicalPath(target: string) {
  let current = AppFileSystem.resolve(target)
  const tail: string[] = []
  while (!existsSync(current)) {
    const parent = path.dirname(current)
    if (parent === current) break
    tail.unshift(path.basename(current))
    current = parent
  }
  try {
    return path.resolve(realpathSync(current), ...tail)
  } catch {
    return AppFileSystem.resolve(target)
  }
}

function assertControlPathAllowed(target: string) {
  const allowed = [
    path.join(Global.Path.data, "memory"),
    path.join(Global.Path.data, "plans"),
    path.join(Global.Path.data, "worktree"),
  ].map(canonicalPath)
  if (allowed.some((root) => AppFileSystem.contains(root, target))) return

  const protectedRoots = [
    Global.Path.data,
    Global.Path.config,
    Global.Path.state,
    Global.Path.cache,
    process.env.OPENCLANK_HOME,
    process.env.OPEN_CLANK_CONTROL_DATA_DIR,
    process.env.OPEN_CLANK_SKILLS_DIR,
  ]
    .filter((root): root is string => Boolean(root))
    .map(canonicalPath)
  if (protectedRoots.some((root) => AppFileSystem.contains(root, target))) {
    throw new Error("Open Clank control data is not accessible through model file tools")
  }
}

export const assertExternalDirectoryEffect = Effect.fn("Tool.assertExternalDirectory")(function* (
  ctx: Tool.Context,
  target?: string,
  options?: Options,
) {
  if (!target) return
  const full = canonicalPath(target)
  if (options?.bypass) return full
  assertControlPathAllowed(full)

  const ins = yield* InstanceState.context
  const directory = AppFileSystem.resolve(ins.directory)
  if (directory === path.parse(directory).root) {
    throw new Error("filesystem root cannot be the active workspace")
  }
  if (Instance.containsPath(full, ins)) return full

  // Memory tree has its own finer authority (memory-path-guard), which the write
  // tools invoke right after this call. Defer to it: asking external_directory here
  // is redundant and, in headless run mode (no permission replier), deadlocks on a
  // never-resolved Deferred. memory-path-guard allows a task-bound subagent its own
  // tasks/<taskId>/*.md and rejects cross-task / wrong-agent writes.
  if (AppFileSystem.contains(path.join(Global.Path.data, "memory"), full)) return full

  // App-managed worktrees live under <data>/worktree/<projectID>/<name>. They are
  // TRUSTED workspaces created and owned by the app itself (workflow isolation and
  // related tooling), not foreign user paths. A subagent may inherit the spawner's
  // (main-checkout) Instance boundary, which does not contain the worktree path —
  // every in-worktree write would then hit external_directory:ask, and a background
  // child has no interactive replier so the ask fails closed. Trust the managed
  // base here, exactly as the memory subtree above. Genuinely external user paths
  // are unaffected and still prompt.
  if (AppFileSystem.contains(path.join(Global.Path.data, "worktree"), full)) return full

  const kind = options?.kind ?? "file"
  const dir = kind === "directory" ? full : path.dirname(full)
  const glob =
    process.platform === "win32"
      ? AppFileSystem.normalizePathPattern(path.join(dir, "*"))
      : path.join(dir, "*").replaceAll("\\", "/")

  yield* ctx.ask({
    permission: "external_directory",
    patterns: [glob],
    always: [glob],
    metadata: {
      filepath: full,
      parentDir: dir,
    },
  })
  return full
})

export async function assertExternalDirectory(ctx: Tool.Context, target?: string, options?: Options) {
  return Effect.runPromise(assertExternalDirectoryEffect(ctx, target, options).pipe(Effect.provide(EffectLogger.layer)))
}

/**
 * Whether new memory may be written (see memory/write-gate.ts for the field).
 *
 * Resolved with `Effect.serviceOption` rather than `yield* Config.Service` on
 * purpose: `Tool.Def.execute` is typed `Effect<ExecuteResult>` with NO
 * requirements, so every helper a write tool calls must keep R = never.
 * serviceOption reads the service out of the ambient runtime when present
 * (always, in-app) without adding it to the requirement set.
 *
 * Fails OPEN — no Config service (unit tests, detached fibers) means writing
 * stays enabled. A config we cannot read must never silently block memory writes.
 */
const memoryWriteEnabled = Effect.gen(function* () {
  const svc = yield* Effect.serviceOption(Config.Service)
  if (Option.isNone(svc)) return true
  return isMemoryWriteEnabled(yield* svc.value.get())
})

/**
 * The single write-permission gate for file-mutating tools (edit, write,
 * apply_patch, notebook_edit). Runs every write check in order:
 *   1. external_directory — asks before touching paths outside the worktree
 *      (defers the memory subtree to the memory guard; see the early return above).
 *   2. memory-path-guard — finer authority over the memory tree.
 *
 * Collapsing into one call makes "call one gate but forget another"
 * unrepresentable — a new write tool that calls this cannot drift into
 * leaving memory unguarded.
 */
export const assertWriteAllowed = Effect.fn("Tool.assertWriteAllowed")(function* (
  ctx: Tool.Context,
  target?: string,
  options?: Options,
) {
  const canonical = yield* assertExternalDirectoryEffect(ctx, target, options)
  if (!target) return
  const full = canonical ?? AppFileSystem.resolve(target)

  // Instance.current is a getter that THROWS when no instance is ALS-bound
  // (detached fibers, tests without a project fixture). The optional chain runs
  // only after the getter returns, so it cannot save us — the try/catch is
  // load-bearing, not defensive dead code. Fall back to ProjectID.global so the
  // guard can still resolve a canonical memory path. Mirrors session/checkpoint.ts.
  const projectID = (() => {
    try {
      return (Instance.current?.project?.id as ProjectID | undefined) ?? ProjectID.global
    } catch {
      return ProjectID.global
    }
  })()

  // System-agent write sandbox: checkpoint-writer is memory-only, while
  // dream/distill may also write <worktree>/.openclank.
  assertAgentWriteSandbox({
    target: full,
    agentName: ctx.agent,
    memoryRoot: path.join(Global.Path.data, "memory"),
    worktree: (yield* InstanceState.context).worktree,
  })

  assertMemoryWriteAllowed({
    target: full,
    agentName: ctx.agent,
    memoryRoot: path.join(Global.Path.data, "memory"),
    projectID,
    sessionID: ctx.sessionID,
    taskId: ctx.taskId,
    writeEnabled: yield* memoryWriteEnabled,
  })
  return full
})

/**
 * Perform the per-write `edit` permission ask, EXCEPT for targets under
 * <data>/memory/. The memory tree's authority is memory-path-guard (invoked by
 * assertWriteAllowed, which every write tool calls first): it already allows the
 * checkpoint-writer / task-bound subagent their canonical paths and rejects
 * everything else. Asking `edit` there is redundant and — for a background fork
 * inheriting a parent's `edit:ask`/`deny` — would deny/skip the checkpoint write.
 * Outside the memory tree, ask exactly as the write tools did inline before.
 *
 * Mirrors the external_directory memory-region deferral added in the 2026-06-04
 * poststop-progress-permission-deadlock fix (see assertExternalDirectoryEffect).
 */
export const askEditUnlessMemory = Effect.fn("Tool.askEditUnlessMemory")(function* (
  ctx: Tool.Context,
  filepath: string,
  input: { patterns: string[]; diff: string; files?: unknown },
) {
  const full = process.platform === "win32" ? AppFileSystem.normalizePath(filepath) : filepath
  if (AppFileSystem.contains(path.join(Global.Path.data, "memory"), full)) return
  yield* ctx.ask({
    permission: "edit",
    patterns: input.patterns,
    always: ["*"],
    metadata: { filepath, diff: input.diff, ...(input.files !== undefined ? { files: input.files } : {}) },
  })
})
