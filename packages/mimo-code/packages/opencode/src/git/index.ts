import { isDeepStrictEqual } from 'node:util'
import { gitExecutable } from './index-physical'
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import { Effect, Layer, Context, Stream, Cause } from "effect"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import type { ChildProcessHandle } from "effect/unstable/process/ChildProcessSpawner"
import { previewPhysicalIndex, inspectPhysicalIndex, previewPhysicalCommit, applyPhysicalCommit, applyPhysicalIndex, type PhysicalIndexPreview, type PhysicalIndexInspection, type PhysicalCommitPreview, type CommitAction, type CommitPhase, type CommitResult, type IndexAction, type IndexPhase, type IndexResult } from './index-physical'

const cfg = [
  "--no-optional-locks",
  "-c",
  "core.autocrlf=false",
  "-c",
  "core.fsmonitor=false",
  "-c",
  "core.longpaths=true",
  "-c",
  "core.symlinks=true",
  "-c",
  "core.quotepath=false",
] as const

const out = (result: { text(): string }) => result.text().trim()
const nuls = (text: string) => text.split("\0").filter(Boolean)
export function inventoryPrefix(bytes: Buffer): { data: Buffer; truncated: boolean } {
  let end = bytes.length, count = 0
  for (let i = 0; i < bytes.length; i++) if (bytes[i] === 0 && ++count === 32) { end = i + 1; break }
  return { data: bytes.subarray(0, end), truncated: end < bytes.length }
}
const fail = (err: unknown) =>
  ({
    exitCode: 1,
    text: () => "",
    stdout: Buffer.alloc(0),
    stderr: Buffer.from(err instanceof Error ? err.message : String(err)),
  }) satisfies Result

export type Kind = "added" | "deleted" | "modified"

export type Base = {
  readonly name: string
  readonly ref: string
}

export type Item = {
  readonly file: string
  readonly code: string
  readonly status: Kind
}

export type Stat = {
  readonly file: string
  readonly additions: number
  readonly deletions: number
}

export interface Result {
  readonly exitCode: number
  readonly text: () => string
  readonly stdout: Buffer
  readonly stderr: Buffer
}

export interface Options {
  readonly cwd: string
  readonly env?: Record<string, string>
}

export interface Interface {
  readonly run: (args: string[], opts: Options) => Effect.Effect<Result>
  readonly removeCleanup: (input:{cwd:string;target:string;expectedExecutable:{path:string;device:number;inode:number;size:number;sha256:string}}) => Effect.Effect<{issued:boolean;child:"settled"|"unknown";exitCode?:number}>
  readonly readCleanup: (input:{cwd:string;operation:"config"|"registered"|"status"|"tracked"|"untracked"|"common"|"branch"|"head"|"headOID"|"direct";executable?:string;target?:string}) => Effect.Effect<Result>
  readonly readWorkbench: (input: { cwd: string; operation: "root" | "objectFormat" | "head" | "index" | "indexPath" | "tree" | "indexEntry" | "treeEntry" | "blob" | "others" | "branches" | "log" | "worktrees" | "status" | "worktree" | "staged" | "headDiff"; path?: string }) => Effect.Effect<Result>
  readonly applyPhysicalCommit: (root:string, expected:PhysicalCommitPreview, action:CommitAction, candidateToken:string, recheck:(phase:CommitPhase)=>Promise<boolean>) => Effect.Effect<CommitResult>
  readonly previewPhysicalCommit: (root:string) => Effect.Effect<PhysicalCommitPreview | undefined>
  readonly inspectPhysicalIndex: (root:string) => Effect.Effect<PhysicalIndexInspection | undefined>
  readonly previewPhysicalIndex: (root:string) => Effect.Effect<PhysicalIndexPreview | undefined>
  readonly applyPhysicalIndex: (root:string, expected:PhysicalIndexPreview, action:IndexAction, candidateToken:string, recheck:(phase:IndexPhase)=>Promise<boolean>) => Effect.Effect<IndexResult>
  readonly branch: (cwd: string) => Effect.Effect<string | undefined>
  readonly prefix: (cwd: string) => Effect.Effect<string>
  readonly defaultBranch: (cwd: string) => Effect.Effect<Base | undefined>
  readonly hasHead: (cwd: string) => Effect.Effect<boolean>
  readonly mergeBase: (cwd: string, base: string, head?: string) => Effect.Effect<string | undefined>
  readonly show: (cwd: string, ref: string, file: string, prefix?: string) => Effect.Effect<string>
  readonly status: (cwd: string) => Effect.Effect<Item[]>
  readonly diff: (cwd: string, ref: string) => Effect.Effect<Item[]>
  readonly stats: (cwd: string, ref: string) => Effect.Effect<Stat[]>
}

const kind = (code: string): Kind => {
  if (code === "??") return "added"
  if (code.includes("U")) return "modified"
  if (code.includes("A") && !code.includes("D")) return "added"
  if (code.includes("D") && !code.includes("A")) return "deleted"
  return "modified"
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Git") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const spawner = yield* ChildProcessSpawner.ChildProcessSpawner

    const run = Effect.fn("Git.run")(
      function* (args: string[], opts: Options) {
        const proc = ChildProcess.make("git", [...cfg, ...args], {
          cwd: opts.cwd,
          env: opts.env,
          extendEnv: true,
          stdin: "ignore",
          stdout: "pipe",
          stderr: "pipe",
        })
        const handle = yield* spawner.spawn(proc)
        const [stdout, stderr] = yield* Effect.all(
          [Stream.mkString(Stream.decodeText(handle.stdout)), Stream.mkString(Stream.decodeText(handle.stderr))],
          { concurrency: 2 },
        )
        return {
          exitCode: yield* handle.exitCode,
          text: () => stdout,
          stdout: Buffer.from(stdout),
          stderr: Buffer.from(stderr),
        } satisfies Result
      },
      Effect.scoped,
      Effect.catch((err) => Effect.succeed(fail(err))),
    )

    // Closed read-only workbench commands. Scoped interruption terminates the
    // child; neither external diff drivers nor user hooks may be invoked.
    const readWorkbench = Effect.fn("Git.readWorkbench")(
      function* (input: { cwd: string; operation: "root" | "objectFormat" | "head" | "index" | "indexPath" | "tree" | "indexEntry" | "treeEntry" | "blob" | "others" | "branches" | "log" | "worktrees" | "status" | "worktree" | "staged" | "headDiff"; path?: string }) {
        if (process.platform !== "linux") return fail(new Error("unavailable"))
        const path = input.path
        // Worktree status/diff can invoke repository-configured clean filters.
        // They remain unavailable until a raw, filter-free comparison exists.
        if (new Set<string>(["status", "worktree", "staged", "headDiff"]).has(input.operation)) return fail(new Error("unavailable"))
        if (path !== undefined && (!path || path.startsWith("/") || path.includes("\0") || path.split("/").some((p) => !p || p === "." || p === ".." || p === ".git" || p === ".clanker" || p === ".references"))) return fail(new Error("unavailable"))
        const args = input.operation === "root" ? ["rev-parse", "--show-toplevel", "--git-common-dir"]
          : input.operation === "objectFormat" ? ["rev-parse", "--show-object-format"]
          : input.operation === "head" ? ["rev-parse", "--verify", "HEAD"]
          : input.operation === "index" ? ["ls-files", "--stage", "-z"]
          : input.operation === "indexPath" ? ["rev-parse", "--git-path", "index"]
          : input.operation === "tree" ? ["ls-tree", "-r", "-z", "--full-tree", "HEAD"]
          : input.operation === "indexEntry" && path ? ["ls-files", "--stage", "-z", "--", path]
          : input.operation === "treeEntry" && path ? ["ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", path]
          : input.operation === "others" ? ["ls-files", "--others", "--exclude-standard", "-z"]
          : input.operation === "branches" ? ["for-each-ref", "--count=101", "--sort=refname", "--format=%(refname)%00%(objectname)%00%(HEAD)%00", "refs/heads/"]
          : input.operation === "log" ? ["log", "-z", "-101", "--format=%H%x00%P%x00%ct%x00%s", "--no-show-signature"]
          : input.operation === "worktrees" ? ["worktree", "list", "--porcelain", "-z"]
          : input.operation === "blob" && path && /^[0-9a-f]{40}([0-9a-f]{24})?$/.test(path) ? ["cat-file", "blob", path]
          : input.operation === "status" ? ["status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "-z", "--", "."]
          : path ? ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", ...(input.operation === "staged" ? ["--cached"] : input.operation === "headDiff" ? ["HEAD"] : []), "--", path] : []
        if (!args.length) return fail(new Error("unavailable"))
        const proc = ChildProcess.make("git", [...cfg, "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", "-c", "credential.helper=", ...args], {
          cwd: input.cwd, env: { PATH: process.env.PATH ?? "/usr/bin:/bin",
            GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null", GIT_ATTR_NOSYSTEM: "1", GIT_TERMINAL_PROMPT: "0",
            GIT_OPTIONAL_LOCKS: "0", GIT_LITERAL_PATHSPECS: "1", GIT_NO_LAZY_FETCH: "1", GIT_NO_REPLACE_OBJECTS: "1", GIT_PAGER: "cat", PAGER: "cat", LC_ALL: "C" }, extendEnv: false,
          stdin: "ignore", stdout: "pipe", stderr: "pipe",
        })
        const handle = yield* spawner.spawn(proc)
        const collect = (stream: typeof handle.stdout) => stream.pipe(Stream.runFold((): Buffer => Buffer.alloc(0), (acc, chunk) => {
          if (acc.length + chunk.length > (input.operation === "index" || input.operation === "tree" || input.operation === "others" ? 4 * 1024 * 1024 : 262144)) throw new Error("output limit")
          return Buffer.concat([acc, Buffer.from(chunk)])
        }))
        const [stdout] = yield* Effect.all([collect(handle.stdout), collect(handle.stderr)], { concurrency: 2 })
        const exitCode = yield* handle.exitCode
        return { exitCode, stdout, stderr: Buffer.alloc(0), text: () => stdout.toString("utf8") } satisfies Result
      },
      Effect.scoped,
      Effect.timeout(3000),
      Effect.catch(() => Effect.succeed(fail(new Error("unavailable")))),
    )

    // Both private operations share this bounded scoped child owner.
    const cleanupChild = Effect.fn("Git.cleanupChild")(function* (
      cwd: string,
      args: readonly string[],
      executable: string,
      onIssued?: () => void,
      onHandle?: (handle:ChildProcessHandle) => void,
    ) {
      onIssued?.();
      const handle = yield* spawner.spawn(
        ChildProcess.make(
          executable,
          [
            ...cfg,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "credential.helper=",
            ...args,
          ],
          {
            cwd,
            env: {
              PATH: process.env.PATH ?? "/usr/bin:/bin",
              GIT_CONFIG_NOSYSTEM: "1",
              GIT_CONFIG_GLOBAL: "/dev/null",
              GIT_ATTR_NOSYSTEM: "1",
              GIT_TERMINAL_PROMPT: "0",
              GIT_OPTIONAL_LOCKS: "0",
              GIT_LITERAL_PATHSPECS: "1",
              GIT_NO_LAZY_FETCH: "1",
              GIT_NO_REPLACE_OBJECTS: "1",
              GIT_PAGER: "cat",
              PAGER: "cat",
              LC_ALL: "C",
            },
            extendEnv: false,
            stdin: "ignore",
            stdout: "pipe",
            stderr: "pipe",
          },
        ),
      );
      onHandle?.(handle)
      const collect = (stream: typeof handle.stdout) =>
        stream.pipe(
          Stream.runFold(
            (): Buffer => Buffer.alloc(0),
            (acc, chunk) => {
              if (acc.length + chunk.length > 4 * 1024 * 1024)
                throw Error("output limit");
              return Buffer.concat([acc, Buffer.from(chunk)]);
            },
          ),
        );
      const [stdout] = yield* Effect.all(
        [collect(handle.stdout), collect(handle.stderr)],
        { concurrency: 2 },
      );
      const exitCode = yield* handle.exitCode;
      return {
        exitCode,
        stdout,
        stderr: Buffer.alloc(0),
        text: () => stdout.toString("utf8"),
      } satisfies Result;
    }, Effect.scoped);
    const readCleanup = Effect.fn("Git.readCleanup")(
      function* (input: {
        cwd: string;
        operation:
          | "config"
          | "registered"
          | "status"
          | "tracked"
          | "untracked"
          | "common"
          | "branch"
          | "head"
          | "headOID"
          | "direct";
        executable?: string;
        target?: string;
      }) {
        if (process.platform !== "linux" || !input.cwd.startsWith("/"))
          return fail(new Error("unavailable"));
        const commands = {
          config: ["config", "--local", "--no-includes", "--null", "--list"],
          registered: ["worktree", "list", "--porcelain", "-z"],
          status: [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
          ],
          tracked: ["ls-files", "--stage", "-z"],
          untracked: ["ls-files", "--others", "-z"],
          common: ["rev-parse", "--git-common-dir"],
          branch: ["show-ref", "--verify", "--quiet", input.target ?? ""],
          head: ["symbolic-ref", "--quiet", "--no-recurse", "HEAD"],
          headOID: ["rev-parse", "--verify", "HEAD"],
          direct: [
            "for-each-ref",
            "--format=%(refname)%00%(symref)",
            input.target ?? "",
          ],
        } as const;
        const args = commands[input.operation];
        if (
          !args ||
          ((input.operation === "branch" || input.operation === "direct") &&
            (!input.target || input.target.includes("\0"))) ||
          (input.executable !== undefined && !input.executable.startsWith("/"))
        )
          return fail(new Error("unavailable"));
        return yield* cleanupChild(input.cwd, args, input.executable ?? "git");
      },
      Effect.timeout(3000),
      Effect.catchCause((cause) =>
        Cause.hasInterrupts(cause)
          ? Effect.interrupt
          : Effect.succeed(fail(new Error("unavailable"))),
      ),
    );
    const removeCleanup = (input: {
      cwd: string;
      target: string;
      expectedExecutable: {
        path: string;
        device: number;
        inode: number;
        size: number;
        sha256: string;
      };
    }): Effect.Effect<{
      issued: boolean;
      child: "settled" | "unknown";
      exitCode?: number;
    }> => {
      let issued = false;
      const observed:{handle?:ChildProcessHandle} = {};
      const unknown = () => ({ issued, child: "unknown" as const });
      return Effect.uninterruptibleMask(restore => Effect.gen(function* () {
        const outcome = yield* restore(Effect.gen(function* () {
          if (process.platform !== "linux" || !input.cwd.startsWith("/") || !input.target.startsWith("/") || input.target.includes("\0")) return unknown()
          const executable = yield* Effect.tryPromise({try:()=>gitExecutable(),catch:()=>new Error("unavailable")})
          if (!isDeepStrictEqual(executable,input.expectedExecutable)) return unknown()
          const result = yield* cleanupChild(input.cwd,["worktree","remove","--",input.target],executable.path,()=>{issued=true},value=>{observed.handle=value})
          return {issued,child:"settled" as const,exitCode:result.exitCode}
        }).pipe(Effect.timeout(30000))).pipe(Effect.exit)
        if (outcome._tag === "Success") return outcome.value
        // cleanupChild's scoped finalizer has completed. The original handle's
        // isRunning reads the spawner's exact child-exit Deferred, not a PID.
        if (observed.handle) {
          const running = yield* observed.handle.isRunning.pipe(Effect.orElseSucceed(()=>true))
          if (!running) return {issued,child:"settled" as const}
        }
        return unknown()
      }))
    };

    const text = Effect.fn("Git.text")(function* (args: string[], opts: Options) {
      return (yield* run(args, opts)).text()
    })

    const lines = Effect.fn("Git.lines")(function* (args: string[], opts: Options) {
      return (yield* text(args, opts))
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean)
    })

    const refs = Effect.fnUntraced(function* (cwd: string) {
      return yield* lines(["for-each-ref", "--format=%(refname:short)", "refs/heads"], { cwd })
    })

    const configured = Effect.fnUntraced(function* (cwd: string, list: string[]) {
      const result = yield* run(["config", "init.defaultBranch"], { cwd })
      const name = out(result)
      if (!name || !list.includes(name)) return
      return { name, ref: name } satisfies Base
    })

    const primary = Effect.fnUntraced(function* (cwd: string) {
      const list = yield* lines(["remote"], { cwd })
      if (list.includes("origin")) return "origin"
      if (list.length === 1) return list[0]
      if (list.includes("upstream")) return "upstream"
      return list[0]
    })

    const branch = Effect.fn("Git.branch")(function* (cwd: string) {
      const result = yield* run(["symbolic-ref", "--quiet", "--short", "HEAD"], { cwd })
      if (result.exitCode !== 0) return
      const text = out(result)
      return text || undefined
    })

    const prefix = Effect.fn("Git.prefix")(function* (cwd: string) {
      const result = yield* run(["rev-parse", "--show-prefix"], { cwd })
      if (result.exitCode !== 0) return ""
      return out(result)
    })

    const defaultBranch = Effect.fn("Git.defaultBranch")(function* (cwd: string) {
      const remote = yield* primary(cwd)
      if (remote) {
        const head = yield* run(["symbolic-ref", `refs/remotes/${remote}/HEAD`], { cwd })
        if (head.exitCode === 0) {
          const ref = out(head).replace(/^refs\/remotes\//, "")
          const name = ref.startsWith(`${remote}/`) ? ref.slice(`${remote}/`.length) : ""
          if (name) return { name, ref } satisfies Base
        }
      }

      const list = yield* refs(cwd)
      const next = yield* configured(cwd, list)
      if (next) return next
      if (list.includes("main")) return { name: "main", ref: "main" } satisfies Base
      if (list.includes("master")) return { name: "master", ref: "master" } satisfies Base
    })

    const hasHead = Effect.fn("Git.hasHead")(function* (cwd: string) {
      const result = yield* run(["rev-parse", "--verify", "HEAD"], { cwd })
      return result.exitCode === 0
    })

    const mergeBase = Effect.fn("Git.mergeBase")(function* (cwd: string, base: string, head = "HEAD") {
      const result = yield* run(["merge-base", base, head], { cwd })
      if (result.exitCode !== 0) return
      const text = out(result)
      return text || undefined
    })

    const show = Effect.fn("Git.show")(function* (cwd: string, ref: string, file: string, prefix = "") {
      const target = prefix ? `${prefix}${file}` : file
      const result = yield* run(["show", `${ref}:${target}`], { cwd })
      if (result.exitCode !== 0) return ""
      if (result.stdout.includes(0)) return ""
      return result.text()
    })

    const status = Effect.fn("Git.status")(function* (cwd: string) {
      return nuls(
        yield* text(["status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "-z", "--", "."], {
          cwd,
        }),
      ).flatMap((item) => {
        const file = item.slice(3)
        if (!file) return []
        const code = item.slice(0, 2)
        return [{ file, code, status: kind(code) } satisfies Item]
      })
    })

    const diff = Effect.fn("Git.diff")(function* (cwd: string, ref: string) {
      const list = nuls(
        yield* text(["diff", "--no-ext-diff", "--no-renames", "--name-status", "-z", ref, "--", "."], { cwd }),
      )
      return list.flatMap((code, idx) => {
        if (idx % 2 !== 0) return []
        const file = list[idx + 1]
        if (!code || !file) return []
        return [{ file, code, status: kind(code) } satisfies Item]
      })
    })

    const stats = Effect.fn("Git.stats")(function* (cwd: string, ref: string) {
      return nuls(
        yield* text(["diff", "--no-ext-diff", "--no-renames", "--numstat", "-z", ref, "--", "."], { cwd }),
      ).flatMap((item) => {
        const a = item.indexOf("\t")
        const b = item.indexOf("\t", a + 1)
        if (a === -1 || b === -1) return []
        const file = item.slice(b + 1)
        if (!file) return []
        const adds = item.slice(0, a)
        const dels = item.slice(a + 1, b)
        const additions = adds === "-" ? 0 : Number.parseInt(adds || "0", 10)
        const deletions = dels === "-" ? 0 : Number.parseInt(dels || "0", 10)
        return [
          {
            file,
            additions: Number.isFinite(additions) ? additions : 0,
            deletions: Number.isFinite(deletions) ? deletions : 0,
          } satisfies Stat,
        ]
      })
    })

    return Service.of({
      run,
      readWorkbench,
      readCleanup,
      removeCleanup,
      applyPhysicalCommit: (root,expected,action,candidateToken,recheck) => Effect.uninterruptible(Effect.promise(()=>applyPhysicalCommit(root,expected,action,candidateToken,recheck).catch(()=>({state:'unknown' as const,tree:'unknown' as const,commit:'unknown' as const,ref:'unknown' as const,candidateRetained:true,cleanup:'unknown' as const})))),
      previewPhysicalCommit: (root) => Effect.promise(()=>previewPhysicalCommit(root).catch(()=>undefined)),
      inspectPhysicalIndex: (root) => Effect.promise(()=>inspectPhysicalIndex(root).catch(()=>undefined)),
      previewPhysicalIndex: (root) => Effect.promise(()=>previewPhysicalIndex(root).catch(()=>undefined)),
      applyPhysicalIndex: (root,expected,action,candidateToken,recheck) => Effect.uninterruptible(Effect.promise(()=>applyPhysicalIndex(root,expected,action,candidateToken,recheck).catch(()=>({state:'unknown' as const,object:'unknown' as const,candidateRetained:true})))),
      branch,
      prefix,
      defaultBranch,
      hasHead,
      mergeBase,
      show,
      status,
      diff,
      stats,
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(CrossSpawnSpawner.defaultLayer))

export * as Git from "."
