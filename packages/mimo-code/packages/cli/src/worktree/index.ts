import z from "zod"
import { NamedError } from "@mimo-ai/shared/util/error"
import { Global } from "../global"
import { Instance } from "../project/instance"
import { InstanceBootstrap } from "../project/bootstrap"
import { Project } from "../project"
import { Database, eq } from "../storage"
import { ProjectTable } from "../project/project.sql"
import type { ProjectID } from "../project/schema"
import { Log } from "../util"
import { Slug } from "@mimo-ai/shared/util/slug"
import { errorMessage } from "../util/error"
import { BusEvent } from "@/bus/bus-event"
import { GlobalBus } from "@/bus/global"
import { Git } from "@/git"
import { ActorRegistry, type DirectoryRetirementSnapshot } from "@/actor/registry"
import { gitExecutable } from "@/git/index-physical"
import { isDeepStrictEqual } from "node:util"
import { Effect, Layer, Path, Scope, Context, Semaphore, Stream, Cause } from "effect"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import { NodePath } from "@effect/platform-node"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { BootstrapRuntime } from "@/effect/bootstrap-runtime"
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import { InstanceState } from "@/effect"

const log = Log.create({ service: "worktree" })

export const Event = {
  Ready: BusEvent.define(
    "worktree.ready",
    z.object({
      name: z.string(),
      branch: z.string(),
    }),
  ),
  Failed: BusEvent.define(
    "worktree.failed",
    z.object({
      message: z.string(),
    }),
  ),
}

export const Info = z
  .object({
    name: z.string(),
    branch: z.string(),
    directory: z.string(),
  })
  .meta({
    ref: "Worktree",
  })

export type Info = z.infer<typeof Info>

export const CreateInput = z
  .object({
    name: z.string().optional(),
    startCommand: z.string().optional().describe("Additional startup script to run after the project's start command"),
  })
  .meta({
    ref: "WorktreeCreateInput",
  })

export type CreateInput = z.infer<typeof CreateInput>

export const RemoveInput = z
  .object({
    directory: z.string(),
  })
  .meta({
    ref: "WorktreeRemoveInput",
  })

export type RemoveInput = z.infer<typeof RemoveInput>

export const ResetInput = z
  .object({
    directory: z.string(),
  })
  .meta({
    ref: "WorktreeResetInput",
  })

export type ResetInput = z.infer<typeof ResetInput>

export const NotGitError = NamedError.create(
  "WorktreeNotGitError",
  z.object({
    message: z.string(),
  }),
)

export const NameGenerationFailedError = NamedError.create(
  "WorktreeNameGenerationFailedError",
  z.object({
    message: z.string(),
  }),
)

export const CreateFailedError = NamedError.create(
  "WorktreeCreateFailedError",
  z.object({
    message: z.string(),
  }),
)

export const StartCommandFailedError = NamedError.create(
  "WorktreeStartCommandFailedError",
  z.object({
    message: z.string(),
  }),
)

export const RemoveFailedError = NamedError.create(
  "WorktreeRemoveFailedError",
  z.object({
    message: z.string(),
  }),
)

export const ResetFailedError = NamedError.create(
  "WorktreeResetFailedError",
  z.object({
    message: z.string(),
  }),
)

function slugify(input: string) {
  return input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .replace(/-+$/, "")
}

function failedRemoves(...chunks: string[]) {
  return chunks.filter(Boolean).flatMap((chunk) =>
    chunk
      .split("\n")
      .map((line) => line.trim())
      .flatMap((line) => {
        const match = line.match(/^warning:\s+failed to remove\s+(.+):\s+/i)
        if (!match) return []
        const value = match[1]?.trim().replace(/^['"]|['"]$/g, "")
        if (!value) return []
        return [value]
      }),
  )
}

// ---------------------------------------------------------------------------
// Effect service
// ---------------------------------------------------------------------------

export type CleanupWitness = { state:"ready"; project:string; directory:string; branch:string; headOID:string; trackedSHA256:string; device:number; inode:number; commonDir:string; commonDevice:number; commonInode:number; adminDir:string; adminDevice:number; adminInode:number; gitfile:{device:number;inode:number;sha256:string}; reciprocal:{device:number;inode:number;sha256:string}; config:{device:number;inode:number;sha256:string}; executable:{path:string;device:number;inode:number;size:number;sha256:string}; scopes:DirectoryRetirementSnapshot }
export type CleanupResult = {state:"applied"|"refused"|"unknown";retirement:"not_issued"|"applied"|"unknown";removal:"not_issued"|"applied"|"unknown";child:"not_issued"|"settled"|"unknown";branch:"present"|"unknown"}

export interface Interface {
  readonly makeWorktreeInfo: (name?: string) => Effect.Effect<Info>
  readonly createFromInfo: (info: Info, startCommand?: string) => Effect.Effect<void>
  readonly create: (input?: CreateInput) => Effect.Effect<Info>
  readonly previewCleanup: (directory:string) => Effect.Effect<CleanupWitness | undefined>
  readonly removeReviewed: (witness:CleanupWitness,digest:string,recheck:(phase:"before_close"|"before_retire"|"before_remove")=>Effect.Effect<boolean>) => Effect.Effect<CleanupResult>
  readonly remove: (input: RemoveInput) => Effect.Effect<boolean>
  readonly reset: (input: ResetInput) => Effect.Effect<boolean>
  readonly head: (directory: string) => Effect.Effect<string>
  readonly isPristine: (directory: string, base: string) => Effect.Effect<boolean>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Worktree") {}

type GitResult = { code: number; text: string; stderr: string }

export const layer: Layer.Layer<
  Service,
  never,
  AppFileSystem.Service | Path.Path | ChildProcessSpawner.ChildProcessSpawner | Git.Service | Project.Service | ActorRegistry.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const scope = yield* Scope.Scope
    const fs = yield* AppFileSystem.Service
    const pathSvc = yield* Path.Path
    const spawner = yield* ChildProcessSpawner.ChildProcessSpawner
    const gitSvc = yield* Git.Service
    const actors = yield* ActorRegistry.Service
    const project = yield* Project.Service

    // Per-parent-repo lock around `git worktree add` + the `candidate` scan that
    // picks its name. Two concurrent isolated agents on the same repo would
    // otherwise race on .git/index.lock / .git/worktrees admin, and one's
    // `git worktree add` would fail nondeterministically.
    const locks = new Map<string, Semaphore.Semaphore>()
    const lock = (key: string) => {
      const hit = locks.get(key)
      if (hit) return hit
      const next = Semaphore.makeUnsafe(1)
      locks.set(key, next)
      return next
    }

    const git = Effect.fnUntraced(
      function* (args: string[], opts?: { cwd?: string }) {
        const handle = yield* spawner.spawn(
          ChildProcess.make("git", args, { cwd: opts?.cwd, extendEnv: true, stdin: "ignore" }),
        )
        const [text, stderr] = yield* Effect.all(
          [Stream.mkString(Stream.decodeText(handle.stdout)), Stream.mkString(Stream.decodeText(handle.stderr))],
          { concurrency: 2 },
        )
        const code = yield* handle.exitCode
        return { code, text, stderr } satisfies GitResult
      },
      Effect.scoped,
      Effect.catch((e) =>
        Effect.succeed({ code: 1, text: "", stderr: e instanceof Error ? e.message : String(e) } satisfies GitResult),
      ),
    )

    const MAX_NAME_ATTEMPTS = 26
    const candidate = Effect.fn("Worktree.candidate")(function* (root: string, base?: string) {
      const ctx = yield* InstanceState.context
      for (const attempt of Array.from({ length: MAX_NAME_ATTEMPTS }, (_, i) => i)) {
        const name = base ? (attempt === 0 ? base : `${base}-${Slug.create()}`) : Slug.create()
        const branch = `openclank/${name}`
        const directory = pathSvc.join(root, name)

        if (yield* fs.exists(directory).pipe(Effect.orDie)) continue

        const ref = `refs/heads/${branch}`
        const branchCheck = yield* git(["show-ref", "--verify", "--quiet", ref], { cwd: ctx.worktree })
        if (branchCheck.code === 0) continue

        return Info.parse({ name, branch, directory })
      }
      throw new NameGenerationFailedError({ message: "Failed to generate a unique worktree name" })
    })

    const makeWorktreeInfo = Effect.fn("Worktree.makeWorktreeInfo")(function* (name?: string) {
      const ctx = yield* InstanceState.context
      if (ctx.project.vcs !== "git") {
        throw new NotGitError({ message: "Worktrees are only supported for git projects" })
      }

      const root = pathSvc.join(Global.Path.data, "worktree", ctx.project.id)
      yield* fs.makeDirectory(root, { recursive: true }).pipe(Effect.orDie)

      const base = name ? slugify(name) : ""
      return yield* candidate(root, base || undefined)
    })

    const setup = Effect.fnUntraced(function* (info: Info) {
      const ctx = yield* InstanceState.context
      // Hold the per-parent-repo lock across the whole atomic creation: the
      // checked-out `worktree add -b` AND the HEAD-attachment assertion. All
      // child worktrees share one ref store (via --git-common-dir), so a
      // concurrent creator mutating packed-refs could otherwise clobber this
      // branch ref advance while the commit object survives -> merge-by-hash.
      yield* lock(ctx.worktree).withPermits(1)(
        Effect.gen(function* () {
          // Atomic: create the worktree already checked out onto a new branch
          // so HEAD is attached to refs/heads/<branch> from the start. This
          // replaces the old non-atomic two-step (`--no-checkout -b` here then
          // a separate `git reset --hard` in boot), which could leave an
          // unborn/empty checkout onto which later commits landed off-branch.
          const created = yield* git(["worktree", "add", "-b", info.branch, info.directory], {
            cwd: ctx.worktree,
          })
          if (created.code !== 0) {
            throw new CreateFailedError({ message: created.stderr || created.text || "Failed to create git worktree" })
          }

          // Assert HEAD is attached to the expected branch. If git ever hands
          // back a detached or wrong HEAD, fail loudly here rather than letting
          // the child commit onto a detached HEAD (the root cause of the ref
          // not advancing in the outer repo).
          const symbolic = yield* git(["symbolic-ref", "--quiet", "HEAD"], { cwd: info.directory })
          const head = symbolic.text.trim()
          const expected = `refs/heads/${info.branch}`
          if (symbolic.code !== 0 || head !== expected) {
            throw new CreateFailedError({
              message: `Worktree HEAD is not attached to ${expected} (got ${head || "detached HEAD"})`,
            })
          }

          // A separate worktree checkout shares the object/ref store but has its
          // own config, so it does NOT inherit the parent repo's LOCAL identity.
          // Copy the parent's resolved identity (`git config` walks
          // local->global->system) into the new worktree's own local config, so a
          // commit made here is attributed exactly as a commit in the parent repo
          // would be. Reading an unset key exits non-zero / empty, which the
          // `git()` runner returns as empty text.
          //
          // When the parent has no identity we pin NOTHING, deliberately. A
          // hardcoded substitute would attribute the user's commits to an address
          // they never chose, and `git config` cannot see the rest of git's own
          // resolution chain anyway (`EMAIL`, then git's `user@hostname`
          // autodetect), so substituting here would pre-empt a value git could
          // still resolve. Abstaining leaves the worktree resolving authorship
          // exactly as the parent repo does, and leaves the fallback to git.
          const parentName = (yield* git(["config", "user.name"], { cwd: ctx.worktree })).text.trim()
          const parentEmail = (yield* git(["config", "user.email"], { cwd: ctx.worktree })).text.trim()
          if (parentName) yield* git(["config", "user.name", parentName], { cwd: info.directory })
          if (parentEmail) yield* git(["config", "user.email", parentEmail], { cwd: info.directory })
          if (!parentName || !parentEmail)
            log.warn("worktree created without a fully pinned git identity; git resolves authorship itself", {
              directory: info.directory,
              parent: ctx.worktree,
              name: parentName ? "inherited" : "unset",
              email: parentEmail ? "inherited" : "unset",
            })
        }),
      )

      yield* project.addSandbox(ctx.project.id, info.directory).pipe(Effect.catch(() => Effect.void))
    })

    const boot = Effect.fnUntraced(function* (info: Info, startCommand?: string) {
      const ctx = yield* InstanceState.context
      const workspaceID = yield* InstanceState.workspaceID
      const projectID = ctx.project.id
      const extra = startCommand?.trim()

      // Population is no longer a separate step: setup() creates the worktree
      // already checked out on the branch, so the working tree is populated and
      // HEAD is verified-attached before we get here.
      const booted = yield* Effect.uninterruptible(Effect.promise(() =>
        Instance.provide({
          directory: info.directory,
          init: () => BootstrapRuntime.runPromise(InstanceBootstrap),
          fn: () => undefined,
        })
          .then(() => true)
          .catch((error) => {
            const message = errorMessage(error)
            log.error("worktree bootstrap failed", { directory: info.directory, message })
            GlobalBus.emit("event", {
              directory: info.directory,
              project: ctx.project.id,
              workspace: workspaceID,
              payload: { type: Event.Failed.type, properties: { message } },
            })
            return false
          }),
      ))
      if (!booted) return

      GlobalBus.emit("event", {
        directory: info.directory,
        project: ctx.project.id,
        workspace: workspaceID,
        payload: {
          type: Event.Ready.type,
          properties: { name: info.name, branch: info.branch },
        },
      })

      yield* runStartScripts(info.directory, { projectID, extra })
    })

    // This is cooperative exclusion, not a Files grant or a birth identity.
    const directoryAdmission = (directory: string, mayNotExist: boolean) => Effect.gen(function* () {
      const fsNative = yield* Effect.promise(() => import("node:fs/promises"))
      const nativePath = yield* Effect.promise(() => import("node:path"))
      const requested = nativePath.resolve(directory)
      const target = yield* Effect.tryPromise({try: async () => {
        if (!mayNotExist) return await fsNative.realpath(requested)
        const name = nativePath.basename(requested)
        const parent = await fsNative.realpath(nativePath.dirname(requested))
        if (!name || name === "." || name === "..") throw new Error("directory unavailable")
        return nativePath.join(parent, name)
      }, catch: () => new Error("Worktree directory unavailable")}).pipe(Effect.orDie)
      const lease = yield* actors.admitExecutionDirectory(target)
      if (!lease) return yield* Effect.die(new Error("Worktree directory is closing"))
      return lease
    })

    const createFromInfo = Effect.fn("Worktree.createFromInfo")(function* (info: Info, startCommand?: string) {
      yield* Effect.acquireUseRelease(directoryAdmission(info.directory, true),
        () => Effect.gen(function* () { yield* setup(info); yield* boot(info, startCommand) }),
        (lease) => Effect.sync(() => { lease.release() }))
    })

    const create = Effect.fn("Worktree.create")(function* (input?: CreateInput) {
      const info = yield* makeWorktreeInfo(input?.name)
      return yield* Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
        const lease = yield* directoryAdmission(info.directory, true)
        let transferred = false
        return yield* Effect.gen(function* () {
          yield* restore(setup(info))
          yield* boot(info, input?.startCommand).pipe(
            Effect.interruptible,
            Effect.catchCause((cause) => Effect.sync(() => log.error("worktree bootstrap failed", { cause }))),
            Effect.ensuring(Effect.sync(() => { lease.release() })),
            Effect.forkIn(scope),
          )
          transferred = true
          return info
        }).pipe(Effect.ensuring(Effect.sync(() => { if (!transferred) lease.release() })))
      }))
    })

    const canonical = Effect.fnUntraced(function* (input: string) {
      const abs = pathSvc.resolve(input)
      const real = yield* fs.realPath(abs).pipe(Effect.catch(() => Effect.succeed(abs)))
      const normalized = pathSvc.normalize(real)
      return process.platform === "win32" ? normalized.toLowerCase() : normalized
    })

    function parseWorktreeList(text: string) {
      return text
        .split("\n")
        .map((line) => line.trim())
        .reduce<{ path?: string; branch?: string }[]>((acc, line) => {
          if (!line) return acc
          if (line.startsWith("worktree ")) {
            acc.push({ path: line.slice("worktree ".length).trim() })
            return acc
          }
          const current = acc[acc.length - 1]
          if (!current) return acc
          if (line.startsWith("branch ")) {
            current.branch = line.slice("branch ".length).trim()
          }
          return acc
        }, [])
    }

    const locateWorktree = Effect.fnUntraced(function* (
      entries: { path?: string; branch?: string }[],
      directory: string,
    ) {
      for (const item of entries) {
        if (!item.path) continue
        const key = yield* canonical(item.path)
        if (key === directory) return item
      }
      return undefined
    })

    // Private observation only. It grants neither cleanup nor Files authority.
    const observeCleanup: (directory:string, continuation?: (witness:CleanupWitness) => Effect.Effect<void>, retired?:{snapshot:DirectoryRetirementSnapshot;digest:string}) => Effect.Effect<CleanupWitness | undefined> = Effect.fn("Worktree.observeCleanup")( 
      function* (directory: string, continuation?: (witness:CleanupWitness) => Effect.Effect<void>, retired?:{snapshot:DirectoryRetirementSnapshot;digest:string}) {
        const io = <T,>(fn: () => Promise<T>) =>
          Effect.uninterruptible(
            Effect.tryPromise({ try: fn, catch: (error) => error }),
          );
        if (
          process.platform !== "linux" ||
          typeof directory !== "string" ||
          !directory.startsWith("/")
        )
          return undefined;
        const ctx = yield* InstanceState.context;
        const fsp = yield* io(() => import("node:fs/promises")),
          nodefs = yield* io(() => import("node:fs")),
          crypto = yield* io(() => import("node:crypto")),
          path = yield* io(() => import("node:path"));
        const owned = (
          fn: () => Promise<import("node:fs/promises").FileHandle>,
        ) =>
          Effect.acquireRelease(io(fn), (fd) =>
            Effect.promise(() => fd.close().catch(() => {})),
          );
        const small = async (
          fd: import("node:fs/promises").FileHandle,
          max: number,
        ) => {
          const a = await fd.stat();
          if (!a.isFile() || a.size < 0 || a.size > max)
            throw Error("file unavailable");
          const out = Buffer.alloc(max + 1);
          let n = 0;
          while (n < out.length) {
            const r = await fd.read(out, n, out.length - n, n);
            if (!r.bytesRead) break;
            n += r.bytesRead;
          }
          const b = await fd.stat();
          if (
            n !== a.size ||
            a.dev !== b.dev ||
            a.ino !== b.ino ||
            a.size !== b.size ||
            a.mtimeMs !== b.mtimeMs ||
            a.ctimeMs !== b.ctimeMs
          )
            throw Error("file changed");
          return { bytes: out.subarray(0, n), dev: a.dev, ino: a.ino };
        };
        const executable = yield* io(() => gitExecutable())
        const started = Date.now(),
          limit = 4 * 1024 * 1024;
        const read = (
          cwd: string,
          operation:
            | "config"
            | "registered"
            | "status"
            | "tracked"
            | "untracked"
            | "common",
        ) => gitSvc.readCleanup({ cwd, operation, executable:executable.path });
        try {
          const root = yield* io(() => fsp.realpath(directory));
          if (root !== directory || root === ctx.worktree) return undefined;
          const rootfd = yield* owned(() =>
            fsp.open(
              root,
              nodefs.constants.O_RDONLY |
                nodefs.constants.O_DIRECTORY |
                nodefs.constants.O_NOFOLLOW,
            ),
          );
          {
            const rootstat = yield* io(() => rootfd.stat());
            if (
              !rootstat.isDirectory() ||
              !Number.isSafeInteger(rootstat.dev) ||
              !Number.isSafeInteger(rootstat.ino)
            )
              return undefined;
            const gitfile = yield* owned(() =>
              fsp.open(
                `/proc/self/fd/${rootfd.fd}/.git`,
                nodefs.constants.O_RDONLY |
                  nodefs.constants.O_NOFOLLOW |
                  nodefs.constants.O_NONBLOCK,
              ),
            );
            let gitdata: string, gitIdentity: { dev: number; ino: number };
            {
              const st = yield* io(() => small(gitfile, 4096));
              gitIdentity = { dev: st.dev, ino: st.ino };
              gitdata = new TextDecoder("utf-8", { fatal: true }).decode(
                st.bytes,
              );
            }
            const m = /^gitdir: ([^\n\0]+)\n$/.exec(gitdata);
            if (!m) return undefined;
            const admin = yield* io(() =>
              fsp.realpath(path.resolve(root, m[1]!)),
            );
            const commonResult = yield* read(ctx.worktree, "common"),
              selectedCommon = yield* read(root, "common");
            if (commonResult.exitCode !== 0 || selectedCommon.exitCode !== 0)
              return undefined;
            const line = (b: Buffer) => {
              const x = new TextDecoder("utf-8", { fatal: true }).decode(b);
              if (!x.endsWith("\n") || x.slice(0, -1).includes("\n"))
                throw Error("invalid output");
              return x.slice(0, -1);
            };
            const common = yield* io(() =>
                fsp.realpath(
                  path.resolve(ctx.worktree, line(commonResult.stdout)),
                ),
              ),
              selected = yield* io(() =>
                fsp.realpath(path.resolve(root, line(selectedCommon.stdout))),
              );
            if (
              common !== selected ||
              path.dirname(path.dirname(admin)) !== common ||
              path.basename(path.dirname(admin)) !== "worktrees"
            )
              return undefined;
            const adminfd = yield* owned(() =>
                fsp.open(
                  admin,
                  nodefs.constants.O_RDONLY |
                    nodefs.constants.O_DIRECTORY |
                    nodefs.constants.O_NOFOLLOW,
                ),
              ),
              commonfd = yield* owned(() =>
                fsp.open(
                  common,
                  nodefs.constants.O_RDONLY |
                    nodefs.constants.O_DIRECTORY |
                    nodefs.constants.O_NOFOLLOW,
                ),
              );
            {
              const commonstat = yield* io(() => commonfd.stat()),
                astat = yield* io(() => adminfd.stat());
              const reciprocalFile = yield* owned(() =>
                fsp.open(
                  `/proc/self/fd/${adminfd.fd}/gitdir`,
                  nodefs.constants.O_RDONLY |
                    nodefs.constants.O_NOFOLLOW |
                    nodefs.constants.O_NONBLOCK,
                ),
              );
              let reciprocal: string, reciprocalIdentity:{dev:number;ino:number};
              {
                const st = yield* io(() => small(reciprocalFile, 4096));
                reciprocalIdentity={dev:st.dev,ino:st.ino};
                reciprocal = new TextDecoder("utf-8", { fatal: true }).decode(
                  st.bytes,
                );
              }
              if (reciprocal !== `${root}/.git\n`) return undefined;
              if (
                !Number.isSafeInteger(commonstat.dev) ||
                commonstat.dev < 0 ||
                !Number.isSafeInteger(commonstat.ino) ||
                commonstat.ino <= 0 ||
                !Number.isSafeInteger(astat.dev) ||
                astat.dev < 0 ||
                !Number.isSafeInteger(astat.ino) ||
                astat.ino <= 0
              )
                return undefined;
              const configFile = yield* owned(() =>
                fsp.open(
                  `/proc/self/fd/${commonfd.fd}/config`,
                  nodefs.constants.O_RDONLY |
                    nodefs.constants.O_NOFOLLOW |
                    nodefs.constants.O_NONBLOCK,
                ),
              );
              const configWitness = yield* io(() => small(configFile, 131072));
              const config = yield* read(ctx.worktree, "config");
              if (config.exitCode !== 0 || config.stdout.length > 131072)
                return undefined;
              const cfgtext = new TextDecoder("utf-8", { fatal: true }).decode(
                config.stdout,
              );
              if (cfgtext && !cfgtext.endsWith("\0")) return undefined;
              const allowed =
                /^(?:core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)|extensions\.objectformat|user\.(?:name|email)|remote\.[^\n]+\.(?:url|fetch|pushurl)|branch\.[^\n]+\.(?:remote|merge))$/;
              let format = "sha1";
              for (const row of cfgtext.split("\0").filter(Boolean)) {
                const i = row.indexOf("\n"),
                  key = (i < 0 ? row : row.slice(0, i)).toLowerCase(),
                  value = i < 0 ? "" : row.slice(i + 1);
                if (
                  !allowed.test(key) ||
                  (key === "core.bare" && value.toLowerCase() !== "false")
                )
                  return undefined;
                if (key === "extensions.objectformat") format = value;
              }
              if (!["sha1", "sha256"].includes(format)) return undefined;
              const worktreeConfig = yield* io(() =>
                fsp.lstat(`${admin}/config.worktree`).then(
                  () => true,
                  (e: any) => {
                    if (e?.code === "ENOENT") return false;
                    throw e;
                  },
                ),
              );
              if (worktreeConfig) return undefined;
              for (const forbidden of [
                "config.worktree",
                "shallow",
                "objects/info/alternates",
                "info/grafts",
              ]) {
                const found = yield* io(() =>
                  fsp.lstat(`${common}/${forbidden}`).then(
                    () => true,
                    (e: any) => {
                      if (e?.code === "ENOENT") return false;
                      throw e;
                    },
                  ),
                );
                if (found) return undefined;
              }
              const registered = yield* read(ctx.worktree, "registered"),
                status = yield* read(root, "status"),
                others = yield* read(root, "untracked"),
                tracked = yield* read(root, "tracked");
              if (
                [registered, status, others, tracked].some(
                  (v) => v.exitCode !== 0,
                ) ||
                status.stdout.length ||
                others.stdout.length ||
                tracked.stdout.length > limit
              )
                return undefined;
              const reg = new TextDecoder("utf-8", { fatal: true }).decode(
                registered.stdout,
              );
              if (!reg.endsWith("\0")) return undefined;
              const blocks = reg.split("\0\0"),
                matches = blocks
                  .map((b) => b.split("\0"))
                  .filter((lines) => lines.includes(`worktree ${root}`));
              if (
                matches.length !== 1 ||
                matches[0]!.some(
                  (x) => x.startsWith("locked") || x.startsWith("prunable"),
                )
              )
                return undefined;
              const branchLine = matches[0]!.find((x) =>
                x.startsWith("branch refs/heads/"),
              );
              if (!branchLine) return undefined;
              const headValue=yield* gitSvc.readCleanup({cwd:root,operation:'headOID',executable:executable.path})
              const headText=new TextDecoder('utf-8',{fatal:true}).decode(headValue.stdout)
              const width=format==='sha1'?40:64
              if(headValue.exitCode!==0 || !(new RegExp(`^[0-9a-f]{${width}}\\n$`)).test(headText))return undefined
              const headOID=headText.slice(0,-1), trackedSHA256=crypto.createHash('sha256').update(tracked.stdout).digest('hex')
              const literalHead=yield* gitSvc.readCleanup({cwd:root,operation:'head',executable:executable.path})
              const directBranch=yield* gitSvc.readCleanup({cwd:ctx.worktree,operation:'direct',target:branchLine.slice(7),executable:executable.path})
              if(literalHead.exitCode!==0 || directBranch.exitCode!==0 || !literalHead.stdout.equals(Buffer.from(branchLine.slice(7)+'\n')) || !directBranch.stdout.equals(Buffer.from(branchLine.slice(7)+'\0\n')))return undefined
              const rows = new TextDecoder("utf-8", { fatal: true }).decode(
                tracked.stdout,
              );
              if (rows && !rows.endsWith("\0")) return undefined;
              const files = new Map<string, { mode: number; oid: string }>(),
                dirs = new Set<string>([""]);
              for (const row of rows.split("\0").filter(Boolean)) {
                const r = /^(100644|100755) ([0-9a-f]+) 0\t([^\0]+)$/.exec(row);
                if (
                  !r ||
                  r[2]!.length !== (format === "sha1" ? 40 : 64) ||
                  r[3]!
                    .split("/")
                    .some(
                      (v) => !v || v === "." || v === ".." || v === ".git",
                    ) ||
                  files.has(r[3]!)
                )
                  return undefined;
                files.set(r[3]!, { mode: parseInt(r[1]!, 8), oid: r[2]! });
                let d = path.dirname(r[3]!);
                while (d !== ".") {
                  dirs.add(d);
                  d = path.dirname(d);
                }
              }
              let count = 0,
                total = 0;
              const seen = new Set<string>();
              const walk = async (
                fd: number,
                prefix: string,
                depth: number,
              ): Promise<void> => {
                if (depth > 64 || Date.now() - started > 30000)
                  throw Error("limit");
                const dir = await fsp.opendir(`/proc/self/fd/${fd}`);
                for await (const entry of dir) {
                  if (++count > 8192) throw Error("limit");
                  if (!prefix && entry.name === ".git") continue;
                  const relative = prefix
                    ? `${prefix}/${entry.name}`
                    : entry.name;
                  if (
                    entry.name.includes("\ufffd") ||
                    Buffer.from(entry.name).toString("utf8") !== entry.name
                  )
                    throw Error("name");
                  const child = `/proc/self/fd/${fd}/${entry.name}`;
                  if (entry.isDirectory()) {
                    if (!dirs.has(relative)) throw Error("extra directory");
                    const sub = await fsp.open(
                      child,
                      nodefs.constants.O_RDONLY |
                        nodefs.constants.O_DIRECTORY |
                        nodefs.constants.O_NOFOLLOW,
                    );
                    try {
                      await walk(sub.fd, relative, depth + 1);
                    } finally {
                      await sub.close();
                    }
                  } else if (entry.isFile()) {
                    const expected = files.get(relative);
                    if (!expected) throw Error("extra file");
                    const file = await fsp.open(
                      child,
                      nodefs.constants.O_RDONLY |
                        nodefs.constants.O_NOFOLLOW |
                        nodefs.constants.O_NONBLOCK,
                    );
                    try {
                      const a = await file.stat();
                      if (
                        !a.isFile() ||
                        a.size > 64 * 1024 * 1024 ||
                        (total += a.size) > 256 * 1024 * 1024 ||
                        ((a.mode & 0o111) !== 0) !==
                          (expected.mode === 0o100755)
                      )
                        throw Error("file");
                      const digest = crypto.createHash(format);
                      digest.update(`blob ${a.size}\0`);
                      let actual = 0;
                      for await (const chunk of file.createReadStream({
                        autoClose: false,
                      })) {
                        actual += chunk.length;
                        if (
                          actual > a.size ||
                          actual > 64 * 1024 * 1024 ||
                          Date.now() - started > 30000
                        )
                          throw Error("file limit");
                        digest.update(chunk);
                      }
                      if (actual !== a.size) throw Error("file changed");
                      const b = await file.stat(),
                        c = await fsp.lstat(child);
                      if (
                        !c.isFile() ||
                        c.dev !== a.dev ||
                        c.ino !== a.ino ||
                        a.dev !== b.dev ||
                        a.ino !== b.ino ||
                        a.size !== b.size ||
                        a.mtimeMs !== b.mtimeMs ||
                        a.ctimeMs !== b.ctimeMs ||
                        digest.digest("hex") !== expected.oid
                      )
                        throw Error("changed");
                      seen.add(relative);
                    } finally {
                      await file.close();
                    }
                  } else throw Error("unsupported entry");
                }
              };
              yield* io(() => walk(rootfd.fd, "", 0));
              if (seen.size !== files.size) return undefined;
              const trackedAgain = yield* read(root, "tracked"),
                statusAgain = yield* read(root, "status"),
                othersAgain = yield* read(root, "untracked"),
                registeredAgain = yield* read(ctx.worktree, "registered"),
                configAgain = yield* read(ctx.worktree, "config");
              const gitStat = yield* io(() =>
                fsp.lstat(`/proc/self/fd/${rootfd.fd}/.git`),
              );
              if (
                !gitStat.isFile() ||
                gitStat.dev !== gitIdentity.dev ||
                gitStat.ino !== gitIdentity.ino
              )
                return undefined;
              const gitAgain = yield* io(() => small(gitfile, 4096)),
                reciprocalAgain = yield* io(() => small(reciprocalFile, 4096)),
                configFinal = yield* io(() => small(configFile, 131072));
              if (
                new TextDecoder("utf-8", { fatal: true }).decode(
                  gitAgain.bytes,
                ) !== gitdata ||
                new TextDecoder("utf-8", { fatal: true }).decode(
                  reciprocalAgain.bytes,
                ) !== reciprocal ||
                configFinal.dev !== configWitness.dev ||
                configFinal.ino !== configWitness.ino ||
                !configFinal.bytes.equals(configWitness.bytes)
              )
                return undefined;
              const reciprocalPath = yield* io(() =>
                  fsp.lstat(`/proc/self/fd/${adminfd.fd}/gitdir`),
                ),
                configPath = yield* io(() =>
                  fsp.lstat(`/proc/self/fd/${commonfd.fd}/config`),
                );
              if (
                !reciprocalPath.isFile() ||
                reciprocalPath.dev !== reciprocalAgain.dev ||
                reciprocalPath.ino !== reciprocalAgain.ino ||
                !configPath.isFile() ||
                configPath.dev !== configFinal.dev ||
                configPath.ino !== configFinal.ino
              )
                return undefined;
              const rootAgain = yield* io(() => fsp.lstat(root)),
                adminAgain = yield* io(() => fsp.lstat(admin)),
                commonAgain = yield* io(() => fsp.lstat(common));
              if (
                [
                  trackedAgain,
                  statusAgain,
                  othersAgain,
                  registeredAgain,
                  configAgain,
                ].some((v) => v.exitCode !== 0) ||
                !tracked.stdout.equals(trackedAgain.stdout) ||
                statusAgain.stdout.length ||
                othersAgain.stdout.length ||
                !registered.stdout.equals(registeredAgain.stdout) ||
                !config.stdout.equals(configAgain.stdout) ||
                rootAgain.dev !== rootstat.dev ||
                rootAgain.ino !== rootstat.ino ||
                adminAgain.dev !== astat.dev ||
                adminAgain.ino !== astat.ino ||
                commonAgain.dev !== commonstat.dev ||
                commonAgain.ino !== commonstat.ino
              )
                return undefined;
              const finalOID=yield* gitSvc.readCleanup({cwd:root,operation:'headOID',executable:executable.path})
              if(finalOID.exitCode!==0 || !finalOID.stdout.equals(headValue.stdout))return undefined
              const finalHead=yield* gitSvc.readCleanup({cwd:root,operation:'head',executable:executable.path})
              const finalBranch=yield* gitSvc.readCleanup({cwd:ctx.worktree,operation:'direct',target:branchLine.slice(7),executable:executable.path})
              if(finalHead.exitCode!==0 || finalBranch.exitCode!==0 || !finalHead.stdout.equals(literalHead.stdout) || !finalBranch.stdout.equals(directBranch.stdout))return undefined
              const scopes=retired ? (yield* actors.verifyRetiredDirectoryScopes(retired.snapshot,retired.digest)) ? retired.snapshot : undefined : yield* actors.snapshotDirectoryScopes(root)
              if(!scopes || !isDeepStrictEqual(executable,yield* io(()=>gitExecutable())))return undefined
              const witness:CleanupWitness={state:"ready",project:ctx.project.id,directory:root,branch:branchLine.slice(7),headOID,trackedSHA256,device:rootstat.dev,inode:rootstat.ino,commonDir:common,commonDevice:commonstat.dev,commonInode:commonstat.ino,adminDir:admin,adminDevice:astat.dev,adminInode:astat.ino,
                gitfile:{device:gitIdentity.dev,inode:gitIdentity.ino,sha256:crypto.createHash('sha256').update(gitdata).digest('hex')},
                reciprocal:{device:reciprocalIdentity.dev,inode:reciprocalIdentity.ino,sha256:crypto.createHash('sha256').update(reciprocal).digest('hex')},
                config:{device:configWitness.dev,inode:configWitness.ino,sha256:crypto.createHash('sha256').update(configWitness.bytes).digest('hex')},executable,scopes}
              if(continuation)yield* continuation(witness)
              return witness;
            }
          }
        } catch {
          return undefined;
        }
      },
      Effect.scoped,
      Effect.catchCause((cause) =>
        Cause.hasInterrupts(cause)
          ? Effect.interrupt
          : Effect.succeed(undefined),
      ),
    );

    const previewCleanup = (directory: string) => observeCleanup(directory);

    const removeReviewed = (
      input: CleanupWitness,
      digest: string,
      recheck: (
        phase: "before_close" | "before_retire" | "before_remove",
      ) => Effect.Effect<boolean>,
    ): Effect.Effect<CleanupResult> => {
      let retirement: CleanupResult["retirement"] = "not_issued",
        removal: CleanupResult["removal"] = "not_issued",
        child: CleanupResult["child"] = "not_issued",
        branch: CleanupResult["branch"] = "unknown";
      const result = (): CleanupResult => ({
        state:
          removal === "applied"
            ? "applied"
            : removal === "unknown" || retirement === "unknown"
              ? "unknown"
              : "refused",
        retirement,
        removal,
        child,
        branch,
      });
      return Effect.gen(function* () {
        let expected: CleanupWitness;
        try {
          expected = JSON.parse(JSON.stringify(input));
        } catch {
          return result();
        }
        if (
          !expected ||
          expected.state !== "ready" ||
          !/^[0-9a-f]{64}$/.test(digest) ||
          typeof recheck !== "function" ||
          !(yield* recheck("before_close"))
        )
          return result();
        const identity = {
          project: expected.project,
          directory: expected.directory,
          commonDir: expected.commonDir,
          directoryDevice: expected.device,
          directoryInode: expected.inode,
          commonDevice: expected.commonDevice,
          commonInode: expected.commonInode,
        };
        const retention = { retain: false, globalIdle: true };
        const closed = yield* actors.withDirectoryClosing(
          identity,
          Effect.gen(function* () {
            const continuation = (current: CleanupWitness) =>
              Effect.gen(function* () {
                if (
                  !isDeepStrictEqual(current, expected) ||
                  !(yield* recheck("before_retire"))
                )
                  return;
                const paths = yield* actors.directoryScopePaths(digest);
                if (!paths) return;
                const aliases = (values: readonly string[]) =>
                  Effect.uninterruptible(
                    Effect.tryPromise({
                      try: async () => {
                        const fs = await import("node:fs/promises"),
                          path = await import("node:path"),
                          started = Date.now();
                        for (const value of values) {
                          if (
                            Date.now() - started > 30000 ||
                            !path.isAbsolute(value)
                          )
                            return false;
                          const canonical = await fs.realpath(value);
                          const st = await fs.stat(canonical);
                          if (!st.isDirectory()) return false;
                          if (
                            (canonical === expected.directory ||
                              canonical.startsWith(expected.directory + "/")) &&
                            value !== expected.directory &&
                            !value.startsWith(expected.directory + "/")
                          )
                            return false;
                        }
                        return true;
                      },
                      catch: () => new Error("directory paths unavailable"),
                    }),
                  ).pipe(Effect.orElseSucceed(() => false));
                if (!(yield* aliases(paths))) return;
                retirement = "unknown";
                if (
                  !(yield* actors.retireDirectoryScopes(
                    expected.scopes,
                    digest,
                  ))
                ) {
                  retirement = "not_issued";
                  return;
                }
                retirement = "applied";
                if (
                  !(yield* actors.verifyRetiredDirectoryScopes(
                    expected.scopes,
                    digest,
                  )) ||
                  !(yield* recheck("before_remove"))
                )
                  return;
                const pathsAgain = yield* actors.directoryScopePaths(digest);
                if (
                  !pathsAgain ||
                  !isDeepStrictEqual(paths, pathsAgain) ||
                  !(yield* aliases(pathsAgain))
                )
                  return;
                const currentAgain = yield* observeCleanup(
                  expected.directory,
                  undefined,
                  { snapshot: expected.scopes, digest },
                );
                if (!currentAgain || !isDeepStrictEqual(currentAgain, expected))
                  return;
                const ctx = yield* InstanceState.context;
                retention.retain = true;
                removal = "unknown";
                child = "unknown";
                const issued = yield* gitSvc
                  .removeCleanup({
                    cwd: ctx.worktree,
                    target: expected.directory,
                    expectedExecutable: expected.executable,
                  })
                  .pipe(Effect.interruptible, Effect.exit);
                if (issued._tag !== "Success") return;
                if (!issued.value.issued) {
                  retention.retain = false;
                  removal = "not_issued";
                  child = "not_issued";
                  return;
                }
                if (issued.value.child !== "settled") return;
                child = "settled";
                retention.retain = false;
                if (issued.value.exitCode !== 0) return;
                const registered = yield* gitSvc.readCleanup({
                  cwd: ctx.worktree,
                  operation: "registered",
                  executable: expected.executable.path,
                });
                const exists = yield* Effect.tryPromise({
                  try: async () => {
                    const fs = await import("node:fs/promises");
                    try {
                      await fs.lstat(expected.directory);
                      return true;
                    } catch (error) {
                      if ((error as NodeJS.ErrnoException).code === "ENOENT")
                        return false;
                      throw error;
                    }
                  },
                  catch: () => new Error("directory unavailable"),
                }).pipe(Effect.orElseSucceed(() => true));
                const ref = yield* gitSvc.readCleanup({
                  cwd: ctx.worktree,
                  operation: "branch",
                  target: expected.branch,
                  executable: expected.executable.path,
                });
                if (ref.exitCode === 0) branch = "present";
                if (registered.exitCode !== 0 || exists) return;
                const text = new TextDecoder("utf-8", { fatal: true }).decode(
                  registered.stdout,
                );
                if (
                  !text.endsWith("\0") ||
                  text.split("\0").includes(`worktree ${expected.directory}`)
                )
                  return;
                removal = "applied";
              });
            yield* observeCleanup(expected.directory, continuation);
          }),
          retention,
        );
        if (closed.kind !== "completed") return result();
        return result();
      }).pipe(
        Effect.catchCause((cause) =>
          Cause.hasInterrupts(cause)
            ? Effect.interrupt
            : Effect.succeed(result()),
        ),
      );
    };

    // Legacy callers have no reviewed Host authority or durable scope retirement.
    // They must never reach force removal, recursive deletion, or branch deletion.
    const remove = Effect.fn("Worktree.remove")(function* (_input: RemoveInput) {
      throw new RemoveFailedError({ message: "Worktree cleanup is unavailable until reviewed cleanup authority is present" })
    })

    const gitExpect = Effect.fnUntraced(function* (
      args: string[],
      opts: { cwd: string },
      error: (r: GitResult) => Error,
    ) {
      const result = yield* git(args, opts)
      if (result.code !== 0) throw error(result)
      return result
    })

    const runStartCommand = Effect.fnUntraced(
      function* (directory: string, cmd: string) {
        const [shell, args] = process.platform === "win32" ? ["cmd", ["/c", cmd]] : ["bash", ["-lc", cmd]]
        const handle = yield* spawner.spawn(
          ChildProcess.make(shell, args, { cwd: directory, extendEnv: true, stdin: "ignore" }),
        )
        // Drain stdout, capture stderr for error reporting
        const [, stderr] = yield* Effect.all(
          [Stream.runDrain(handle.stdout), Stream.mkString(Stream.decodeText(handle.stderr))],
          { concurrency: 2 },
        ).pipe(Effect.orDie)
        const code = yield* handle.exitCode
        return { code, stderr }
      },
      Effect.scoped,
      Effect.catch(() => Effect.succeed({ code: 1, stderr: "" })),
    )

    const runStartScript = Effect.fnUntraced(function* (directory: string, cmd: string, kind: string) {
      const text = cmd.trim()
      if (!text) return true
      const result = yield* runStartCommand(directory, text)
      if (result.code === 0) return true
      log.error("worktree start command failed", { kind, directory, message: result.stderr })
      return false
    })

    const runStartScripts = Effect.fnUntraced(function* (
      directory: string,
      input: { projectID: ProjectID; extra?: string },
    ) {
      const row = yield* Effect.sync(() =>
        Database.use((db) => db.select().from(ProjectTable).where(eq(ProjectTable.id, input.projectID)).get()),
      )
      const project = row ? Project.fromRow(row) : undefined
      const startup = project?.commands?.start?.trim() ?? ""
      const ok = yield* runStartScript(directory, startup, "project")
      if (!ok) return false
      yield* runStartScript(directory, input.extra ?? "", "worktree")
      return true
    })

    const prune = Effect.fnUntraced(function* (root: string, entries: string[]) {
      const base = yield* canonical(root)
      yield* Effect.forEach(
        entries,
        (entry) =>
          Effect.gen(function* () {
            const target = yield* canonical(pathSvc.resolve(root, entry))
            if (target === base) return
            if (!target.startsWith(`${base}${pathSvc.sep}`)) return
            yield* fs.remove(target, { recursive: true }).pipe(Effect.ignore)
          }),
        { concurrency: "unbounded" },
      )
    })

    const sweep = Effect.fnUntraced(function* (root: string) {
      const first = yield* git(["clean", "-ffdx"], { cwd: root })
      if (first.code === 0) return first

      const entries = failedRemoves(first.stderr, first.text)
      if (!entries.length) return first

      yield* prune(root, entries)
      return yield* git(["clean", "-ffdx"], { cwd: root })
    })

    const resetImpl = Effect.fn("Worktree.resetImpl")(function* (input: ResetInput) {
      const ctx = yield* InstanceState.context
      if (ctx.project.vcs !== "git") {
        throw new NotGitError({ message: "Worktrees are only supported for git projects" })
      }

      const directory = yield* canonical(input.directory)
      const primary = yield* canonical(ctx.worktree)
      if (directory === primary) {
        throw new ResetFailedError({ message: "Cannot reset the primary workspace" })
      }

      const list = yield* git(["worktree", "list", "--porcelain"], { cwd: ctx.worktree })
      if (list.code !== 0) {
        throw new ResetFailedError({ message: list.stderr || list.text || "Failed to read git worktrees" })
      }

      const entry = yield* locateWorktree(parseWorktreeList(list.text), directory)
      if (!entry?.path) {
        throw new ResetFailedError({ message: "Worktree not found" })
      }

      const worktreePath = entry.path

      const base = yield* gitSvc.defaultBranch(ctx.worktree)
      if (!base) {
        throw new ResetFailedError({ message: "Default branch not found" })
      }

      const sep = base.ref.indexOf("/")
      if (base.ref !== base.name && sep > 0) {
        const remote = base.ref.slice(0, sep)
        const branch = base.ref.slice(sep + 1)
        yield* gitExpect(
          ["fetch", remote, branch],
          { cwd: ctx.worktree },
          (r) => new ResetFailedError({ message: r.stderr || r.text || `Failed to fetch ${base.ref}` }),
        )
      }

      yield* gitExpect(
        ["reset", "--hard", base.ref],
        { cwd: worktreePath },
        (r) => new ResetFailedError({ message: r.stderr || r.text || "Failed to reset worktree to target" }),
      )

      const cleanResult = yield* sweep(worktreePath)
      if (cleanResult.code !== 0) {
        throw new ResetFailedError({ message: cleanResult.stderr || cleanResult.text || "Failed to clean worktree" })
      }

      yield* gitExpect(
        ["submodule", "update", "--init", "--recursive", "--force"],
        { cwd: worktreePath },
        (r) => new ResetFailedError({ message: r.stderr || r.text || "Failed to update submodules" }),
      )

      yield* gitExpect(
        ["submodule", "foreach", "--recursive", "git", "reset", "--hard"],
        { cwd: worktreePath },
        (r) => new ResetFailedError({ message: r.stderr || r.text || "Failed to reset submodules" }),
      )

      yield* gitExpect(
        ["submodule", "foreach", "--recursive", "git", "clean", "-fdx"],
        { cwd: worktreePath },
        (r) => new ResetFailedError({ message: r.stderr || r.text || "Failed to clean submodules" }),
      )

      const status = yield* git(["-c", "core.fsmonitor=false", "status", "--porcelain=v1"], { cwd: worktreePath })
      if (status.code !== 0) {
        throw new ResetFailedError({ message: status.stderr || status.text || "Failed to read git status" })
      }

      if (status.text.trim()) {
        throw new ResetFailedError({ message: `Worktree reset left local changes:\n${status.text.trim()}` })
      }

      return worktreePath
    })

    const reset = Effect.fn("Worktree.reset")(function* (input: ResetInput) {
      const ctx = yield* InstanceState.context
      return yield* Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
        const lease = yield* directoryAdmission(input.directory, false)
        let transferred = false
        return yield* Effect.gen(function* () {
          const worktreePath = yield* restore(resetImpl(input))
          yield* runStartScripts(worktreePath, { projectID: ctx.project.id }).pipe(
            Effect.interruptible,
            Effect.catchCause((cause) => Effect.sync(() => log.error("worktree start task failed", { cause }))),
            Effect.ensuring(Effect.sync(() => { lease.release() })),
            Effect.forkIn(scope),
          )
          transferred = true
          return true
        }).pipe(Effect.ensuring(Effect.sync(() => { if (!transferred) lease.release() })))
      }))
    })

    const head = Effect.fn("Worktree.head")(function* (directory: string) {
      const res = yield* git(["rev-parse", "HEAD"], { cwd: directory })
      return res.text.trim()
    })

    const isPristine = Effect.fn("Worktree.isPristine")(function* (directory: string, base: string) {
      const status = yield* git(["status", "--porcelain"], { cwd: directory })
      if (status.code !== 0) return false
      if (status.text.trim() !== "") return false
      const current = yield* git(["rev-parse", "HEAD"], { cwd: directory })
      return current.code === 0 && current.text.trim() === base
    })

    return Service.of({ makeWorktreeInfo, createFromInfo, create, remove, previewCleanup, removeReviewed, reset, head, isPristine })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Git.defaultLayer),
  Layer.provide(ActorRegistry.defaultLayer),
  Layer.provide(CrossSpawnSpawner.defaultLayer),
  Layer.provide(Project.defaultLayer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(NodePath.layer),
)

export * as Worktree from "."
