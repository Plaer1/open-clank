import { Cause, Duration, Effect, Layer, Schedule, Semaphore, Context, Stream, Option } from "effect"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import { formatPatch, structuredPatch } from "diff"
import path from "path"
import z from "zod"
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import { InstanceState } from "@/effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Hash } from "@mimo-ai/shared/util/hash"
import { Config } from "../config"
import { Global } from "../global"
import { Log } from "../util"
import { assertGitSnapshotRef } from "./managed-history"
import { Instance } from "@/project/instance"
import os from "node:os"

export const Patch = z.object({
  hash: z.string(),
  files: z.string().array(),
})
export type Patch = z.infer<typeof Patch>

export class CaptureError extends Error {
  constructor(readonly stage: string) {
    super(`Snapshot capture failed during ${stage}`)
    this.name = "SnapshotCaptureError"
  }
}

export type CaptureOptions = { strict?: boolean }

export const FileDiff = z
  .object({
    file: z.string(),
    patch: z.string(),
    additions: z.number(),
    deletions: z.number(),
    status: z.enum(["added", "deleted", "modified"]).optional(),
  })
  .meta({
    ref: "SnapshotFileDiff",
  })
export type FileDiff = z.infer<typeof FileDiff>

const log = Log.create({ service: "snapshot" })
const prune = "7.days"
const limit = 2 * 1024 * 1024
const core = ["-c", "core.longpaths=true", "-c", "core.symlinks=true"]
const cfg = ["-c", "core.autocrlf=false", ...core]
const quote = [...cfg, "-c", "core.quotepath=false"]
interface GitResult {
  readonly code: ChildProcessSpawner.ExitCode
  readonly text: string
  readonly stderr: string
}

type State = Omit<Interface, "init">

export interface Interface {
  readonly init: () => Effect.Effect<void>
  readonly cleanup: () => Effect.Effect<void>
  readonly track: (options?: CaptureOptions) => Effect.Effect<string | undefined>
  readonly patch: (hash: string, options?: CaptureOptions) => Effect.Effect<Patch>
  readonly restore: (snapshot: string) => Effect.Effect<void>
  readonly revert: (patches: Patch[]) => Effect.Effect<void>
  readonly diff: (hash: string) => Effect.Effect<string>
  readonly diffFull: (from: string, to: string) => Effect.Effect<FileDiff[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Snapshot") {}

/** A turn-scoped barrier installed by SessionProcessor. Plugin dispatch calls
 * it immediately before invoking a handler, using the same resolved handler
 * generation it is about to execute. Outside a managed processor it is absent.
 */
export class CaptureBarrier extends Context.Service<CaptureBarrier, () => Effect.Effect<void, unknown>>()(
  "@opencode/SnapshotCaptureBarrier",
) {}

export class ChildSettlement extends Context.Service<ChildSettlement, { begin: () => void; settled: () => void }>()(
  "@opencode/SnapshotChildSettlement",
) {}

export const layer: Layer.Layer<
  Service,
  never,
  AppFileSystem.Service | ChildProcessSpawner.ChildProcessSpawner | Config.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service
    const spawner = yield* ChildProcessSpawner.ChildProcessSpawner
    const config = yield* Config.Service
    const locks = new Map<string, Semaphore.Semaphore>()

    const lock = (key: string) => {
      const hit = locks.get(key)
      if (hit) return hit

      const next = Semaphore.makeUnsafe(1)
      locks.set(key, next)
      return next
    }

    // Old Git trees are readable only by explicit selection. This path never
    // constructs Snapshot.state, creates an index, or schedules Git cleanup.
    const legacyDiffFull = Effect.fnUntraced(function* (from: string, to: string) {
      if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(from) || !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(to))
        return yield* Effect.fail(new Error("Legacy snapshot reference unavailable"))
      const gitdir = path.join(Global.Path.data, "snapshot", Instance.project.id, Hash.fast(Instance.worktree))
      const read = (args: string[]) => Effect.gen(function* () {
        const observer = Option.getOrUndefined(yield* Effect.serviceOption(ChildSettlement))
        observer?.begin()
        const command = ChildProcess.make("git", ["-c", "core.quotepath=false", "--git-dir", gitdir, ...args], {
          cwd: Instance.directory,
          env: { PATH: process.env.PATH ?? "", GIT_OPTIONAL_LOCKS: "0", GIT_CONFIG_NOSYSTEM: "1",
            GIT_CONFIG_GLOBAL: os.devNull, GIT_TERMINAL_PROMPT: "0", ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}) },
          extendEnv: false,
        })
        const child = yield* spawner.spawn(command)
        const [text, stderr] = yield* Effect.all([Stream.mkString(Stream.decodeText(child.stdout)), Stream.mkString(Stream.decodeText(child.stderr))], {concurrency:2})
        const code = yield* child.exitCode
        observer?.settled()
        if (code !== 0) return yield* Effect.fail(new Error(`Legacy snapshot read unavailable: ${stderr.slice(0, 256)}`))
        return text
      }).pipe(Effect.scoped)
      const statusText = yield* read(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", from, to, "--", "."])
      const statusItems = statusText.split("\0"); if (statusItems.at(-1) === "") statusItems.pop()
      if (statusItems.length % 2 !== 0 || statusItems.length > 2048) return yield* Effect.fail(new Error("Legacy snapshot diff exceeds supported bounds"))
      const statuses = new Map<string, "added" | "deleted" | "modified">()
      for (let i=0; i<statusItems.length; i+=2) {
        const code=statusItems[i]!, name=statusItems[i+1]!
        if (!name || !/^[A-Z]$/.test(code) || statuses.has(name)) return yield* Effect.fail(new Error("Legacy snapshot diff unavailable"))
        statuses.set(name,code === "A" ? "added" : code === "D" ? "deleted" : "modified")
      }
      const countsText = yield* read(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", from, to, "--", "."])
      const entries = countsText.split("\0"); if (entries.at(-1) === "") entries.pop()
      if (entries.length !== statuses.size) return yield* Effect.fail(new Error("Legacy snapshot diff changed"))
      const result: FileDiff[] = []
      for (const entry of entries) {
        const match=/^(\d+|-)\t(\d+|-)\t([\s\S]+)$/.exec(entry)
        if (!match || !statuses.has(match[3]!)) return yield* Effect.fail(new Error("Legacy snapshot diff unavailable"))
        const file=match[3]!, binary=match[1] === "-" || match[2] === "-"
        const patch = binary ? "" : yield* read(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", from, to, "--", file])
        result.push({file,patch,additions:binary ? 0 : Number(match[1]),deletions:binary ? 0 : Number(match[2]),status:statuses.get(file)!})
      }
      return result
    })

    const state = yield* InstanceState.make<State>(
      Effect.fn("Snapshot.state")(function* (ctx) {
        const state = {
          directory: ctx.directory,
          worktree: ctx.worktree,
          gitdir: path.join(Global.Path.data, "snapshot", ctx.project.id, Hash.fast(ctx.worktree)),
          vcs: ctx.project.vcs,
        }

        const args = (cmd: string[]) => ["--git-dir", state.gitdir, "--work-tree", state.worktree, ...cmd]

        const enc = new TextEncoder()
        const feed = (list: string[]) => Stream.make(enc.encode(list.join("\0") + "\0"))

        const git = Effect.fnUntraced(
          function* (
            cmd: string[],
            opts?: { cwd?: string; env?: Record<string, string>; stdin?: ChildProcess.CommandInput },
          ) {
            const proc = ChildProcess.make("git", cmd, {
              cwd: opts?.cwd,
              env: opts?.env,
              extendEnv: true,
              stdin: opts?.stdin,
            })
            const observer = Option.getOrUndefined(yield* Effect.serviceOption(ChildSettlement))
            observer?.begin()
            const handle = yield* spawner.spawn(proc)
            const [text, stderr] = yield* Effect.all(
              [Stream.mkString(Stream.decodeText(handle.stdout)), Stream.mkString(Stream.decodeText(handle.stderr))],
              { concurrency: 2 },
            )
            const code = yield* handle.exitCode
            observer?.settled()
            return { code, text, stderr } satisfies GitResult
          },
          Effect.scoped,
          Effect.catch((err) =>
            Effect.succeed({
              code: ChildProcessSpawner.ExitCode(1),
              text: "",
              stderr: err instanceof Error ? err.message : String(err),
            }),
          ),
        )

        const ignore = Effect.fnUntraced(function* (files: string[], strict = false) {
          if (!files.length) return new Set<string>()
          const check = yield* git(
            [
              ...quote,
              "--git-dir",
              path.join(state.worktree, ".git"),
              "--work-tree",
              state.worktree,
              "check-ignore",
              "--no-index",
              "--stdin",
              "-z",
            ],
            {
              cwd: state.directory,
              stdin: feed(files),
            },
          )
          if (check.code !== 0 && (check.code !== 1 || (strict && check.stderr.trim()))) {
            if (strict) return yield* Effect.die(new CaptureError("ignore_check"))
            return new Set<string>()
          }
          return new Set(check.text.split("\0").filter(Boolean))
        })

        const drop = Effect.fnUntraced(function* (files: string[], strict = false) {
          if (!files.length) return
          const result = yield* git(
            [
              ...cfg,
              ...args(["rm", "--cached", "-f", "--ignore-unmatch", "--pathspec-from-file=-", "--pathspec-file-nul"]),
            ],
            {
              cwd: state.directory,
              stdin: feed(files),
            },
          )
          if (strict && result.code !== 0) return yield* Effect.die(new CaptureError("index_drop"))
        })

        const stage = Effect.fnUntraced(function* (files: string[], strict = false) {
          if (!files.length) return
          const result = yield* git(
            [...cfg, ...args(["add", "--all", "--sparse", "--pathspec-from-file=-", "--pathspec-file-nul"])],
            {
              cwd: state.directory,
              stdin: feed(files),
            },
          )
          if (result.code === 0) return
          if (strict) return yield* Effect.die(new CaptureError("index_stage"))
          log.warn("failed to add snapshot files", {
            exitCode: result.code,
            stderr: result.stderr,
          })
        })

        const exists = (file: string) => fs.exists(file).pipe(Effect.orDie)
        const read = (file: string) => fs.readFileString(file).pipe(Effect.catch(() => Effect.succeed("")))
        const remove = (file: string) => fs.remove(file).pipe(Effect.catch(() => Effect.void))
        const locked = <A, E, R>(fx: Effect.Effect<A, E, R>) => lock(state.gitdir).withPermits(1)(fx)

        const enabled = Effect.fnUntraced(function* () {
          if (state.vcs !== "git") return false
          return (yield* config.get()).snapshot !== false
        })

        const excludes = Effect.fnUntraced(function* (strict = false) {
          const result = yield* git(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], {
            cwd: state.worktree,
          })
          if (strict && result.code !== 0) return yield* Effect.die(new CaptureError("exclude_lookup"))
          const file = result.text.trim()
          if (strict && !file) return yield* Effect.die(new CaptureError("exclude_lookup"))
          if (!file) return
          if (!(yield* exists(file))) return
          return file
        })

        const sync = Effect.fnUntraced(function* (list: string[] = [], strict = false) {
          const file = yield* excludes(strict)
          const target = path.join(state.gitdir, "info", "exclude")
          const text = [
            file ? (yield* read(file)).trimEnd() : "",
            ...list.map((item) => `/${item.replaceAll("\\", "/")}`),
          ]
            .filter(Boolean)
            .join("\n")
          yield* fs.ensureDir(path.join(state.gitdir, "info")).pipe(Effect.orDie)
          yield* fs.writeFileString(target, text ? `${text}\n` : "").pipe(Effect.orDie)
        })

        const add = Effect.fnUntraced(function* (strict = false) {
          yield* sync([], strict)
          const [diff, other] = yield* Effect.all(
            [
              git([...quote, ...args(["diff-files", "--name-only", "-z", "--", "."])], {
                cwd: state.directory,
              }),
              git([...quote, ...args(["ls-files", "--others", "--exclude-standard", "-z", "--", "."])], {
                cwd: state.directory,
              }),
            ],
            { concurrency: 2 },
          )
          if (diff.code !== 0 || other.code !== 0) {
            if (strict) return yield* Effect.die(new CaptureError("index_list"))
            log.warn("failed to list snapshot files", {
              diffCode: diff.code,
              diffStderr: diff.stderr,
              otherCode: other.code,
              otherStderr: other.stderr,
            })
            return
          }

          const tracked = diff.text.split("\0").filter(Boolean)
          const untracked = other.text.split("\0").filter(Boolean)
          const all = Array.from(new Set([...tracked, ...untracked]))
          if (!all.length) return

          // Resolve source-repo ignore rules against the exact candidate set.
          // --no-index keeps this pattern-based even when a path is already tracked.
          const ignored = yield* ignore(all, strict)

          // Remove newly-ignored files from snapshot index to prevent re-adding
          if (ignored.size > 0) {
            const ignoredFiles = Array.from(ignored)
            log.info("removing gitignored files from snapshot", { count: ignoredFiles.length })
            yield* drop(ignoredFiles, strict)
          }

          const allow = all.filter((item) => !ignored.has(item))
          if (!allow.length) return

          const large = new Set(
            (yield* Effect.all(
              allow.map((item) =>
                fs
                  .stat(path.join(state.directory, item))
                  .pipe(Effect.catch(() => Effect.void))
                  .pipe(
                    Effect.map((stat) => {
                      if (!stat || stat.type !== "File") return
                      const size = typeof stat.size === "bigint" ? Number(stat.size) : stat.size
                      return size > limit ? item : undefined
                    }),
                  ),
              ),
              { concurrency: 8 },
            )).filter((item): item is string => Boolean(item)),
          )
          const block = new Set(untracked.filter((item) => large.has(item)))
          yield* sync(Array.from(block), strict)
          // Stage only the allowed candidate paths so snapshot updates stay scoped.
          yield* stage(allow.filter((item) => !block.has(item)), strict)
        })

        const cleanup = Effect.fnUntraced(function* () {
          return yield* locked(
            Effect.gen(function* () {
              if (!(yield* enabled())) return
              if (!(yield* exists(state.gitdir))) return
              const result = yield* git(args(["gc", `--prune=${prune}`]), { cwd: state.directory })
              if (result.code !== 0) {
                log.warn("cleanup failed", {
                  exitCode: result.code,
                  stderr: result.stderr,
                })
                return
              }
              log.info("cleanup", { prune })
            }),
          )
        })

        const track = Effect.fnUntraced(function* (options: CaptureOptions = {}) {
          const strict = options.strict === true
          return yield* locked(
            Effect.gen(function* () {
              if (!(yield* enabled())) return
              const existed = yield* exists(state.gitdir)
              yield* fs.ensureDir(state.gitdir).pipe(Effect.orDie)
              if (!existed) {
                const init = yield* git(["init"], {
                  env: { GIT_DIR: state.gitdir, GIT_WORK_TREE: state.worktree },
                })
                const settings = yield* Effect.all(
                  [
                    git(["--git-dir", state.gitdir, "config", "core.autocrlf", "false"]),
                    git(["--git-dir", state.gitdir, "config", "core.longpaths", "true"]),
                    git(["--git-dir", state.gitdir, "config", "core.symlinks", "true"]),
                    git(["--git-dir", state.gitdir, "config", "core.fsmonitor", "false"]),
                  ],
                  { concurrency: 1 },
                )
                if (strict && (init.code !== 0 || settings.some((setting) => setting.code !== 0))) {
                  return yield* Effect.die(new CaptureError("index_init"))
                }
                log.info("initialized")
              }
              yield* add(strict)
              const result = yield* git(args(["write-tree"]), { cwd: state.directory })
              if (strict && result.code !== 0) return yield* Effect.die(new CaptureError("write_tree"))
              const hash = result.text.trim()
              if (strict && !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(hash)) {
                return yield* Effect.die(new CaptureError("write_tree"))
              }
              log.info("tracking", { hash, cwd: state.directory, git: state.gitdir })
              return hash
            }),
          )
        })

        const patch = Effect.fnUntraced(function* (hash: string, options: CaptureOptions = {}) {
          const strict = options.strict === true
          return yield* locked(
            Effect.gen(function* () {
              yield* add(strict)
              const result = yield* git(
                [...quote, ...args(["diff", "--cached", "--no-ext-diff", "--name-only", hash, "--", "."])],
                {
                  cwd: state.directory,
                },
              )
              if (result.code !== 0) {
                if (strict) return yield* Effect.die(new CaptureError("diff_index"))
                log.warn("failed to get diff", { hash, exitCode: result.code })
                return { hash, files: [] }
              }
              const files = result.text
                .trim()
                .split("\n")
                .map((x) => x.trim())
                .filter(Boolean)

              // Hide ignored-file removals from the user-facing patch output.
              const ignored = yield* ignore(files, strict)

              return {
                hash,
                files: files
                  .filter((item) => !ignored.has(item))
                  .map((x) => path.join(state.worktree, x).replaceAll("\\", "/")),
              }
            }),
          )
        })

        const restore = Effect.fnUntraced(function* (snapshot: string) {
          return yield* locked(
            Effect.gen(function* () {
              log.info("restore", { commit: snapshot })
              const result = yield* git([...core, ...args(["read-tree", snapshot])], { cwd: state.worktree })
              if (result.code === 0) {
                const checkout = yield* git([...core, ...args(["checkout-index", "-a", "-f"])], {
                  cwd: state.worktree,
                })
                if (checkout.code === 0) return
                log.error("failed to restore snapshot", {
                  snapshot,
                  exitCode: checkout.code,
                  stderr: checkout.stderr,
                })
                return
              }
              log.error("failed to restore snapshot", {
                snapshot,
                exitCode: result.code,
                stderr: result.stderr,
              })
            }),
          )
        })

        const revert = Effect.fnUntraced(function* (patches: Patch[]) {
          return yield* locked(
            Effect.gen(function* () {
              const ops: { hash: string; file: string; rel: string }[] = []
              const seen = new Set<string>()
              for (const item of patches) {
                for (const file of item.files) {
                  if (seen.has(file)) continue
                  seen.add(file)
                  ops.push({
                    hash: item.hash,
                    file,
                    rel: path.relative(state.worktree, file).replaceAll("\\", "/"),
                  })
                }
              }

              const single = Effect.fnUntraced(function* (op: (typeof ops)[number]) {
                log.info("reverting", { file: op.file, hash: op.hash })
                const result = yield* git([...core, ...args(["checkout", op.hash, "--", op.file])], {
                  cwd: state.worktree,
                })
                if (result.code === 0) return
                const tree = yield* git([...core, ...args(["ls-tree", op.hash, "--", op.rel])], {
                  cwd: state.worktree,
                })
                if (tree.code === 0 && tree.text.trim()) {
                  log.info("file existed in snapshot but checkout failed, keeping", { file: op.file, hash: op.hash })
                  return
                }
                log.info("file did not exist in snapshot, deleting", { file: op.file, hash: op.hash })
                yield* remove(op.file)
              })

              const clash = (a: string, b: string) => a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`)

              for (let i = 0; i < ops.length; ) {
                const first = ops[i]!
                const run = [first]
                let j = i + 1
                // Only batch adjacent files when their paths cannot affect each other.
                while (j < ops.length && run.length < 100) {
                  const next = ops[j]!
                  if (next.hash !== first.hash) break
                  if (run.some((item) => clash(item.rel, next.rel))) break
                  run.push(next)
                  j += 1
                }

                if (run.length === 1) {
                  yield* single(first)
                  i = j
                  continue
                }

                const tree = yield* git(
                  [...core, ...args(["ls-tree", "--name-only", first.hash, "--", ...run.map((item) => item.rel)])],
                  {
                    cwd: state.worktree,
                  },
                )

                if (tree.code !== 0) {
                  log.info("batched ls-tree failed, falling back to single-file revert", {
                    hash: first.hash,
                    files: run.length,
                  })
                  for (const op of run) {
                    yield* single(op)
                  }
                  i = j
                  continue
                }

                const have = new Set(
                  tree.text
                    .trim()
                    .split("\n")
                    .map((item) => item.trim())
                    .filter(Boolean),
                )
                const list = run.filter((item) => have.has(item.rel))
                if (list.length) {
                  log.info("reverting", { hash: first.hash, files: list.length })
                  const result = yield* git(
                    [...core, ...args(["checkout", first.hash, "--", ...list.map((item) => item.file)])],
                    {
                      cwd: state.worktree,
                    },
                  )
                  if (result.code !== 0) {
                    log.info("batched checkout failed, falling back to single-file revert", {
                      hash: first.hash,
                      files: list.length,
                    })
                    for (const op of run) {
                      yield* single(op)
                    }
                    i = j
                    continue
                  }
                }

                for (const op of run) {
                  if (have.has(op.rel)) continue
                  log.info("file did not exist in snapshot, deleting", { file: op.file, hash: op.hash })
                  yield* remove(op.file)
                }

                i = j
              }
            }),
          )
        })

        const diff = Effect.fnUntraced(function* (hash: string) {
          return yield* locked(
            Effect.gen(function* () {
              yield* add()
              const result = yield* git([...quote, ...args(["diff", "--cached", "--no-ext-diff", hash, "--", "."])], {
                cwd: state.worktree,
              })
              if (result.code !== 0) {
                log.warn("failed to get diff", {
                  hash,
                  exitCode: result.code,
                  stderr: result.stderr,
                })
                return ""
              }
              return result.text.trim()
            }),
          )
        })

        const diffFull = Effect.fnUntraced(function* (from: string, to: string) {
          return yield* locked(
            Effect.gen(function* () {
              type Row = {
                file: string
                status: "added" | "deleted" | "modified"
                binary: boolean
                additions: number
                deletions: number
              }

              type Ref = {
                file: string
                side: "before" | "after"
                ref: string
              }

              const show = Effect.fnUntraced(function* (row: Row) {
                if (row.binary) return ["", ""]
                if (row.status === "added") {
                  return [
                    "",
                    yield* git([...cfg, ...args(["show", `${to}:${row.file}`])]).pipe(Effect.map((item) => item.text)),
                  ]
                }
                if (row.status === "deleted") {
                  return [
                    yield* git([...cfg, ...args(["show", `${from}:${row.file}`])]).pipe(
                      Effect.map((item) => item.text),
                    ),
                    "",
                  ]
                }
                return yield* Effect.all(
                  [
                    git([...cfg, ...args(["show", `${from}:${row.file}`])]).pipe(Effect.map((item) => item.text)),
                    git([...cfg, ...args(["show", `${to}:${row.file}`])]).pipe(Effect.map((item) => item.text)),
                  ],
                  { concurrency: 2 },
                )
              })

              const load = Effect.fnUntraced(
                function* (rows: Row[]) {
                  const refs = rows.flatMap((row) => {
                    if (row.binary) return []
                    if (row.status === "added")
                      return [{ file: row.file, side: "after", ref: `${to}:${row.file}` } satisfies Ref]
                    if (row.status === "deleted") {
                      return [{ file: row.file, side: "before", ref: `${from}:${row.file}` } satisfies Ref]
                    }
                    return [
                      { file: row.file, side: "before", ref: `${from}:${row.file}` } satisfies Ref,
                      { file: row.file, side: "after", ref: `${to}:${row.file}` } satisfies Ref,
                    ]
                  })
                  if (!refs.length) return new Map<string, { before: string; after: string }>()

                  const proc = ChildProcess.make("git", [...cfg, ...args(["cat-file", "--batch"])], {
                    cwd: state.directory,
                    extendEnv: true,
                    stdin: Stream.make(new TextEncoder().encode(refs.map((item) => item.ref).join("\n") + "\n")),
                  })
                  const observer = Option.getOrUndefined(yield* Effect.serviceOption(ChildSettlement))
                  observer?.begin()
                  const handle = yield* spawner.spawn(proc)
                  const [out, err] = yield* Effect.all(
                    [Stream.mkUint8Array(handle.stdout), Stream.mkString(Stream.decodeText(handle.stderr))],
                    { concurrency: 2 },
                  )
                  const code = yield* handle.exitCode
                  observer?.settled()
                  if (code !== 0) {
                    log.info("git cat-file --batch failed during snapshot diff, falling back to per-file git show", {
                      stderr: err,
                      refs: refs.length,
                    })
                    return
                  }

                  const fail = (msg: string, extra?: Record<string, string>) => {
                    log.info(msg, { ...extra, refs: refs.length })
                    return undefined
                  }

                  const map = new Map<string, { before: string; after: string }>()
                  const dec = new TextDecoder()
                  let i = 0
                  for (const ref of refs) {
                    let end = i
                    while (end < out.length && out[end] !== 10) end += 1
                    if (end >= out.length) {
                      return fail(
                        "git cat-file --batch returned a truncated header during snapshot diff, falling back to per-file git show",
                      )
                    }

                    const head = dec.decode(out.slice(i, end))
                    i = end + 1
                    const hit = map.get(ref.file) ?? { before: "", after: "" }
                    if (head.endsWith(" missing")) {
                      map.set(ref.file, hit)
                      continue
                    }

                    const match = head.match(/^[0-9a-f]+ blob (\d+)$/)
                    if (!match) {
                      return fail(
                        "git cat-file --batch returned an unexpected header during snapshot diff, falling back to per-file git show",
                        { head },
                      )
                    }

                    const size = Number(match[1])
                    if (!Number.isInteger(size) || size < 0 || i + size >= out.length || out[i + size] !== 10) {
                      return fail(
                        "git cat-file --batch returned truncated content during snapshot diff, falling back to per-file git show",
                        { head },
                      )
                    }

                    const text = dec.decode(out.slice(i, i + size))
                    if (ref.side === "before") hit.before = text
                    if (ref.side === "after") hit.after = text
                    map.set(ref.file, hit)
                    i += size + 1
                  }

                  if (i !== out.length) {
                    return fail(
                      "git cat-file --batch returned trailing data during snapshot diff, falling back to per-file git show",
                    )
                  }

                  return map
                },
                Effect.scoped,
                Effect.catch(() =>
                  Effect.succeed<Map<string, { before: string; after: string }> | undefined>(undefined),
                ),
              )

              const result: FileDiff[] = []
              const status = new Map<string, "added" | "deleted" | "modified">()

              const statuses = yield* git(
                [...quote, ...args(["diff", "--no-ext-diff", "--name-status", "--no-renames", from, to, "--", "."])],
                { cwd: state.directory },
              )

              for (const line of statuses.text.trim().split("\n")) {
                if (!line) continue
                const [code, file] = line.split("\t")
                if (!code || !file) continue
                status.set(file, code.startsWith("A") ? "added" : code.startsWith("D") ? "deleted" : "modified")
              }

              const numstat = yield* git(
                [...quote, ...args(["diff", "--no-ext-diff", "--no-renames", "--numstat", from, to, "--", "."])],
                {
                  cwd: state.directory,
                },
              )

              const rows = numstat.text
                .trim()
                .split("\n")
                .filter(Boolean)
                .flatMap((line) => {
                  const [adds, dels, file] = line.split("\t")
                  if (!file) return []
                  const binary = adds === "-" && dels === "-"
                  const additions = binary ? 0 : parseInt(adds)
                  const deletions = binary ? 0 : parseInt(dels)
                  return [
                    {
                      file,
                      status: status.get(file) ?? "modified",
                      binary,
                      additions: Number.isFinite(additions) ? additions : 0,
                      deletions: Number.isFinite(deletions) ? deletions : 0,
                    } satisfies Row,
                  ]
                })

              // Hide ignored-file removals from the user-facing diff output.
              const ignored = yield* ignore(rows.map((r) => r.file))
              if (ignored.size > 0) {
                const filtered = rows.filter((r) => !ignored.has(r.file))
                rows.length = 0
                rows.push(...filtered)
              }

              const step = 100
              const patch = (file: string, before: string, after: string) =>
                formatPatch(structuredPatch(file, file, before, after, "", "", { context: Number.MAX_SAFE_INTEGER }))

              for (let i = 0; i < rows.length; i += step) {
                const run = rows.slice(i, i + step)
                const text = yield* load(run)

                for (const row of run) {
                  const hit = text?.get(row.file) ?? { before: "", after: "" }
                  const [before, after] = row.binary ? ["", ""] : text ? [hit.before, hit.after] : yield* show(row)
                  result.push({
                    file: row.file,
                    patch: row.binary ? "" : patch(row.file, before, after),
                    additions: row.additions,
                    deletions: row.deletions,
                    status: row.status,
                  })
                }
              }

              return result
            }),
          )
        })

        yield* cleanup().pipe(
          Effect.catchCause((cause) => {
            log.error("cleanup loop failed", { cause: Cause.pretty(cause) })
            return Effect.void
          }),
          Effect.repeat(Schedule.spaced(Duration.hours(1))),
          Effect.delay(Duration.minutes(1)),
          Effect.forkScoped,
        )

        return { cleanup, track, patch, restore, revert, diff, diffFull }
      }),
    )

    return Service.of({
      init: Effect.fn("Snapshot.init")(() => Effect.void),
      cleanup: Effect.fn("Snapshot.cleanup")(() => Effect.void),
      // Automatic capture is owned by Lore, not a Git object store.
      track: Effect.fn("Snapshot.track")(() => Effect.succeed(undefined)),
      patch: Effect.fn("Snapshot.patch")(() => Effect.die(new Error("Git snapshot capture is unavailable"))),
      restore: Effect.fn("Snapshot.restore")(() => Effect.die(new Error("Legacy Git snapshot restore requires Lore recovery"))),
      revert: Effect.fn("Snapshot.revert")(() => Effect.die(new Error("Legacy Git snapshot restore requires Lore recovery"))),
      diff: Effect.fn("Snapshot.diff")(() => Effect.die(new Error("Live Git snapshot diff is unavailable"))),
      diffFull: Effect.fn("Snapshot.diffFull")((from: string, to: string) => {
        assertGitSnapshotRef(from); assertGitSnapshotRef(to)
        return legacyDiffFull(from, to).pipe(Effect.orDie)
      }),
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(CrossSpawnSpawner.defaultLayer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(Config.defaultLayer),
)

export * as Snapshot from "."
