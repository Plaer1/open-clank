import { NodeFileSystem } from "@effect/platform-node"
import { basename, dirname, isAbsolute, join, relative, resolve as pathResolve, sep } from "path"
import { realpathSync } from "fs"
import * as NFS from "fs/promises"
import { createHash, randomUUID } from "crypto"
import { createConnection } from "net"
import os from "os"
import { lookup } from "mime-types"
import { Effect, FileSystem, Layer, Schema, Context } from "effect"
import type { PlatformError } from "effect/PlatformError"
import { Glob } from "./util/glob"
import { Flock } from "./util/flock"

export namespace AppFileSystem {
  export class FileSystemError extends Schema.TaggedErrorClass<FileSystemError>()("FileSystemError", {
    method: Schema.String,
    cause: Schema.optional(Schema.Defect),
  }) {}

  export interface DirEntry {
    readonly name: string
    readonly type: "file" | "directory" | "symlink" | "other"
  }

  export class AtomicConflict extends Error {}

  export class AtomicRollbackError extends Error {
    constructor(
      readonly failures: string[],
      readonly backups: string[],
      options?: ErrorOptions,
    ) {
      super(`atomic batch rollback failed; recoverable backups retained: ${backups.join(", ")}`, options)
    }
  }

  export type Error = PlatformError | FileSystemError | AtomicConflict | AtomicRollbackError

  export type TextSnapshot = {
    readonly text: string
    readonly fingerprint: string
    readonly encoding: "utf-8" | "utf-16-le" | "utf-16-be"
    readonly bom: Uint8Array
    readonly newline: "\n" | "\r\n" | "\r"
    readonly mode: number
    readonly size: number
  }

  export type AtomicChange = {
    readonly path: string
    readonly content: string | Uint8Array | null
    /** One id is propagated across every target in a tool-level batch. */
    readonly actionId?: string
    /** Authenticated owner context for durable history capture. */
    readonly history?: HistoryContext
    readonly expectedFingerprint?: string
    readonly expectedMode?: number
    readonly requireMissing?: boolean
    readonly mode?: number
  }

  export type HistoryStatus = {
    status?: "prepared" | "complete" | "paused" | "failed" | "aborted" | "unconfigured"
    action_id?: string
    history_status: "prepared" | "complete" | "paused" | "failed" | "aborted"
    capture_phase: string
    error?: string
    durable?: boolean
    coverage?: string
  }

  export type HistoryContext = {
    readonly actorId: string
    readonly accountId: string
    readonly workspaceId: string
    readonly workspaceRoot: string
    readonly socketPath: string
    readonly token?: string
    status: HistoryStatus
  }

  /**
   * Build owner-bound capture context from the trusted tool dispatcher.  Tool
   * arguments never supply these values; `extra.historyCapture` is populated
   * by the authenticated session boundary.
   */
  export function historyContextFromTool(ctx: {
    actorID?: string
    extra?: { [key: string]: unknown }
  }, target: string): HistoryContext | undefined {
    const configured = ctx.extra?.historyCapture
    if (!configured || typeof configured !== "object") return undefined
    const value = configured as Record<string, unknown>
    const actorId = typeof ctx.actorID === "string" ? ctx.actorID.trim() : ""
    const accountId = typeof value.accountId === "string" ? value.accountId.trim() : ""
    const workspaceId = typeof value.workspaceId === "string" ? value.workspaceId.trim() : ""
    const workspaceRoot = typeof value.workspaceRoot === "string" ? value.workspaceRoot.trim() : ""
    const socketPath = typeof value.socketPath === "string" ? value.socketPath.trim() : ""
    const token = typeof value.token === "string" ? value.token.trim() : ""
    if (!actorId || !accountId || !workspaceId || !workspaceRoot || !socketPath || !token) return undefined
    if (!isAbsolute(workspaceRoot) || !isAbsolute(socketPath) || /[\\/](?:\.env|credentials?|secrets?)(?:[\\/]|$)/i.test(workspaceRoot)) return undefined
    const normalized = resolve(target)
    const root = resolve(workspaceRoot)
    if (!(normalized === root || normalized.startsWith(`${root}${sep}`))) return undefined
    return {
      actorId,
      accountId,
      workspaceId,
      workspaceRoot: root,
      socketPath,
      token,
      status: { status: "paused", history_status: "paused", capture_phase: "unavailable", durable: false, coverage: "NoCapture" },
    }
  }

  export type FileResources = {
    readonly reads?: readonly string[]
    readonly writes?: readonly string[]
  }

  export type FileResourceLease = {
    readonly release: () => void
  }

  type CanonicalFileResources = {
    readonly reads: readonly string[]
    readonly writes: readonly string[]
  }

  type FileResourceRequest = {
    readonly resources: CanonicalFileResources
    readonly resolve: (lease: FileResourceLease) => void
    readonly reject: (error: globalThis.Error) => void
    readonly signal?: AbortSignal
    abort?: () => void
    granted: boolean
  }

  const fileResourceState: {
    active: FileResourceRequest[]
    waiting: FileResourceRequest[]
  } = {
    active: [],
    waiting: [],
  }

  const canonicalFileResources = (resources: FileResources): CanonicalFileResources => ({
    reads: [...new Set((resources.reads ?? []).filter(Boolean).map(resolve))].sort(),
    writes: [...new Set((resources.writes ?? []).filter(Boolean).map(resolve))].sort(),
  })

  const fileResourcesConflict = (left: CanonicalFileResources, right: CanonicalFileResources) =>
    left.writes.some((write) => [...right.reads, ...right.writes].some((other) => overlaps(write, other))) ||
    left.reads.some((read) => right.writes.some((write) => overlaps(read, write)))

  const drainFileResourceQueue = () => {
    for (const request of [...fileResourceState.waiting]) {
      const index = fileResourceState.waiting.indexOf(request)
      if (index < 0) continue
      if (fileResourceState.active.some((active) => fileResourcesConflict(request.resources, active.resources))) {
        continue
      }
      if (
        fileResourceState.waiting
          .slice(0, index)
          .some((earlier) => fileResourcesConflict(request.resources, earlier.resources))
      ) {
        continue
      }

      fileResourceState.waiting.splice(index, 1)
      fileResourceState.active.push(request)
      request.granted = true
      if (request.abort && request.signal) request.signal.removeEventListener("abort", request.abort)
      let released = false
      request.resolve({
        release: () => {
          if (released) return
          released = true
          const active = fileResourceState.active.indexOf(request)
          if (active >= 0) fileResourceState.active.splice(active, 1)
          drainFileResourceQueue()
        },
      })
    }
  }

  export function acquireFileResources(
    resources: FileResources,
    signal?: AbortSignal,
  ): Promise<FileResourceLease> {
    const canonical = canonicalFileResources(resources)
    if (canonical.reads.length === 0 && canonical.writes.length === 0) {
      return Promise.resolve({ release: () => undefined })
    }
    return new Promise<FileResourceLease>((resolveLease, reject) => {
      const request: FileResourceRequest = {
        resources: canonical,
        resolve: resolveLease,
        reject,
        signal,
        granted: false,
      }
      request.abort = () => {
        if (request.granted) return
        const index = fileResourceState.waiting.indexOf(request)
        if (index >= 0) fileResourceState.waiting.splice(index, 1)
        request.reject(new globalThis.Error("file resource wait aborted"))
        drainFileResourceQueue()
      }
      if (signal?.aborted) {
        request.abort()
        return
      }
      if (signal) signal.addEventListener("abort", request.abort, { once: true })
      fileResourceState.waiting.push(request)
      drainFileResourceQueue()
    })
  }

  export function scheduleFileResources<A, E, R>(
    resources: FileResources,
    effect: Effect.Effect<A, E, R>,
    signal?: AbortSignal,
  ) {
    return Effect.acquireUseRelease(
      Effect.tryPromise({
        try: (effectSignal) =>
          acquireFileResources(
            resources,
            signal ? AbortSignal.any([effectSignal, signal]) : effectSignal,
          ),
        catch: (cause) => (cause instanceof globalThis.Error ? cause : new globalThis.Error(String(cause))),
      }),
      () => effect,
      (lease) => Effect.sync(lease.release),
    )
  }

  export interface Interface extends FileSystem.FileSystem {
    readonly isDir: (path: string) => Effect.Effect<boolean>
    readonly isFile: (path: string) => Effect.Effect<boolean>
    readonly existsSafe: (path: string) => Effect.Effect<boolean>
    readonly readJson: (path: string) => Effect.Effect<unknown, Error>
    readonly writeJson: (path: string, data: unknown, mode?: number) => Effect.Effect<void, Error>
    readonly ensureDir: (path: string) => Effect.Effect<void, Error>
    readonly writeWithDirs: (path: string, content: string | Uint8Array, mode?: number) => Effect.Effect<void, Error>
    readonly canonicalTarget: (path: string) => Effect.Effect<string, Error>
    readonly readTextSnapshot: (path: string) => Effect.Effect<TextSnapshot, Error>
    readonly atomicWrite: (change: AtomicChange) => Effect.Effect<string, Error>
    readonly atomicBatch: (changes: AtomicChange[]) => Effect.Effect<void, Error>
    readonly readDirectoryEntries: (path: string) => Effect.Effect<DirEntry[], Error>
    readonly findUp: (target: string, start: string, stop?: string) => Effect.Effect<string[], Error>
    readonly up: (options: { targets: string[]; start: string; stop?: string }) => Effect.Effect<string[], Error>
    readonly globUp: (pattern: string, start: string, stop?: string) => Effect.Effect<string[], Error>
    readonly glob: (pattern: string, options?: Glob.Options) => Effect.Effect<string[], Error>
    readonly globMatch: (pattern: string, filepath: string) => boolean
  }

  export class Service extends Context.Service<Service, Interface>()("@opencode/FileSystem") {}

  export const fingerprintBytes = (data: Uint8Array) =>
    `sha256:${createHash("sha256").update(data).digest("hex")}:${data.byteLength}`

  const historyRequest = (socketPath: string, payload: unknown): Promise<any> =>
    new Promise((resolveRequest, rejectRequest) => {
      const socket = createConnection(socketPath)
      const chunks: Buffer[] = []
      let settled = false
      const finish = (error?: Error, value?: any) => {
        if (settled) return
        settled = true
        if (error) rejectRequest(error)
        else resolveRequest(value)
      }
      socket.setTimeout(10_000, () => {
        socket.destroy()
        finish(new Error("history IPC timeout"))
      })
      socket.on("error", (error) => finish(error))
      socket.on("data", (chunk) => chunks.push(Buffer.from(chunk)))
      socket.on("end", () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"))
          if (value?.Error || value?.error || value?.code === "error") {
            throw new Error(String(value.Error ?? value.error ?? value.code))
          }
          finish(undefined, value)
        } catch (error) {
          finish(error instanceof Error ? error : new Error(String(error)))
        }
      })
      socket.on("connect", () => socket.end(`${JSON.stringify(payload)}\n`))
    })

  const historyAuth = (context: HistoryContext) => ({
    actor_id: context.actorId,
    account_id: context.accountId,
    token: context.token ?? "",
  })

  const historyEnvelope = (context: HistoryContext, actionId: string, path: string, operation: string, before: Uint8Array | undefined, paths: string[]) => {
    const resource = (value: string) => ({
      account_id: context.accountId,
      workspace_id: context.workspaceId,
      provider: "mimo-filesystem",
      // Keep host paths out of the service record. A trusted owner can later
      // replace this locator hash with a registry identity at restore time.
      resource_id: `file:sha256:${createHash("sha256").update(`${context.accountId}:${context.workspaceId}:${resolve(value)}`).digest("hex")}`,
    })
    const modified = [path, ...paths].map(resource)
    return {
      schema_version: 1,
      action_id: actionId,
      actor_account_id: context.accountId,
      resource_key: modified[0],
      guard_resource_ids: [],
      modified_resource_ids: modified,
      operation,
      expected_revision: null,
      actor_id: context.actorId,
      actor_kind: "agent",
      session_id: null,
      run_id: null,
      task_id: null,
      tool_id: "mimo-filesystem",
      before_revision: before ? { Opaque: { kind: "fingerprint", value: fingerprintBytes(before) } } : null,
      expected_after_revision: null,
      original_locator: { display_name: basename(path), location_label: resolve(path), opaque_ref: null },
      destination_locator: null,
      timestamp_millis: Date.now(),
      coverage: {
        kind: paths.length ? "ObservedAfterOnly" : "KnownMutationHooks",
        roots: [{ display_name: basename(context.workspaceRoot), location_label: context.workspaceRoot, opaque_ref: null }],
        exclusions: paths.length ? ["non-primary batch targets lack exact byte payloads"] : [],
      },
      per_resource_outcomes: [path, ...paths].map((value, index) => ({
        resource_id: resource(value).resource_id,
        status: index === 0 ? "ExactBeforeAndAfter" : "ObservedAfterOnly",
        revision: null,
      })),
    }
  }

  const beginHistoryCapture = async (change: AtomicChange, canonicalTargets: string[]) => {
    const context = change.history
    const actionId = change.actionId
    if (!context || !actionId) return undefined
    const before = await NFS.readFile(canonicalTargets[0]!).catch((error: any) => {
      if (error?.code === "ENOENT") return undefined
      throw error
    })
    const operation = before ? "replace" : "create"
    try {
      await historyRequest(context.socketPath, {
        Prepare: {
          envelope: { protocol_version: 1, auth: historyAuth(context), claimed_digest: "", request: historyEnvelope(context, actionId, canonicalTargets[0]!, operation, before, canonicalTargets.slice(1)) },
          content: before ? Buffer.from(before).toString("base64") : null,
          fingerprint: before ? fingerprintBytes(before) : "missing",
        },
      })
      context.status = Object.assign(context.status, { status: "prepared", action_id: actionId, history_status: "prepared", capture_phase: "before_durable", durable: true, coverage: canonicalTargets.length > 1 ? "ObservedAfterOnly" : "KnownMutationHooks" })
      return { context, actionId, before }
    } catch (error) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "before_failed", durable: false, coverage: "NoCapture", error: String(error) })
      return undefined
    }
  }

  const finishHistoryCapture = async (capture: { context: HistoryContext; actionId: string }, canonicalTargets: string[], committed: boolean) => {
    const { context, actionId } = capture
    let after: Buffer | undefined
    try {
      after = await NFS.readFile(canonicalTargets[0]!).catch((error: any) => {
        if (error?.code === "ENOENT") return undefined
        throw error
      })
    } catch (error) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "after_failed", durable: false, error: String(error) })
      return
    }
    if (!committed) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "live_not_committed", durable: false, coverage: "BeforeOnly" })
      await historyRequest(context.socketPath, { RecordLive: { envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId }, receipt: { action_id: actionId, status: "NotCommitted", fingerprint: null, after_unavailable: false } } }).catch(() => undefined)
      return
    }
    try {
      await historyRequest(context.socketPath, { RecordLive: { envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId }, receipt: { action_id: actionId, status: "Committed", fingerprint: after ? fingerprintBytes(after) : "missing", after_unavailable: false } } })
      await historyRequest(context.socketPath, { Complete: { envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId }, content: after ? Buffer.from(after).toString("base64") : null, fingerprint: after ? fingerprintBytes(after) : "missing" } })
      context.status = Object.assign(context.status, { status: "complete", action_id: actionId, history_status: "complete", capture_phase: "complete", durable: true })
    } catch (error) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "after_failed", durable: false, error: String(error) })
    }
  }

  const fileFingerprint = async (target: string) => {
    try {
      return fingerprintBytes(await NFS.readFile(target))
    } catch (error: any) {
      if (error?.code === "ENOENT") return undefined
      throw error
    }
  }

  const dominantNewline = (text: string): "\n" | "\r\n" | "\r" => {
    const crlf = text.match(/\r\n/g)?.length ?? 0
    const lf = (text.match(/\n/g)?.length ?? 0) - crlf
    const cr = (text.match(/\r/g)?.length ?? 0) - crlf
    if (crlf && crlf >= lf && crlf >= cr) return "\r\n"
    if (cr > lf) return "\r"
    return "\n"
  }

  const readSnapshot = async (target: string): Promise<TextSnapshot> => {
    const raw = await NFS.readFile(target)
    let bom: Uint8Array = new Uint8Array()
    let encoding: TextSnapshot["encoding"] = "utf-8"
    let body: Uint8Array = raw
    if (raw.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf]))) {
      bom = raw.subarray(0, 3)
      body = raw.subarray(3)
    } else if (raw.subarray(0, 2).equals(Buffer.from([0xff, 0xfe]))) {
      bom = raw.subarray(0, 2)
      body = raw.subarray(2)
      encoding = "utf-16-le"
    } else if (raw.subarray(0, 2).equals(Buffer.from([0xfe, 0xff]))) {
      bom = raw.subarray(0, 2)
      body = Buffer.from(raw.subarray(2))
      if (body.byteLength % 2 !== 0) throw new Error(`${target}: invalid UTF-16BE byte length`)
      body = Buffer.from(Array.from(body).map((_, index, all) => all[index ^ 1] ?? 0))
      encoding = "utf-16-be"
    }
    if (encoding === "utf-16-le" && body.byteLength % 2 !== 0) {
      throw new Error(`${target}: invalid UTF-16LE byte length`)
    }
    const text = new TextDecoder(encoding === "utf-8" ? "utf-8" : "utf-16", { fatal: true }).decode(body)
    if (text.includes("\0")) throw new Error(`${target}: NUL byte marks a binary file`)
    return {
      text,
      fingerprint: fingerprintBytes(raw),
      encoding,
      bom,
      newline: dominantNewline(text),
      mode: (await NFS.stat(target)).mode,
      size: raw.byteLength,
    }
  }

  export function encodeText(snapshot: TextSnapshot, text: string): Uint8Array {
    let body = Buffer.from(text, snapshot.encoding === "utf-8" ? "utf8" : "utf16le")
    if (snapshot.encoding === "utf-16-be") {
      body = Buffer.from(Array.from(body).map((_, index, all) => all[index ^ 1] ?? 0))
    }
    return Buffer.concat([Buffer.from(snapshot.bom), body])
  }

  export function preserveNewlines(text: string, newline: TextSnapshot["newline"]) {
    const normalized = text.replaceAll("\r\n", "\n").replaceAll("\r", "\n")
    return newline === "\n" ? normalized : normalized.replaceAll("\n", newline)
  }

  const stage = async (change: AtomicChange, mode?: number) => {
    const dir = dirname(change.path)
    await NFS.mkdir(dir, { recursive: true })
    const temp = join(dir, `.${basename(change.path)}.tmp.${randomUUID()}`)
    const handle = await NFS.open(temp, "wx", mode ?? 0o666)
    try {
      const content = typeof change.content === "string" ? Buffer.from(change.content) : change.content!
      await handle.writeFile(content)
      await handle.sync()
      await handle.close()
      return temp
    } catch (error) {
      await handle.close().catch(() => undefined)
      await NFS.unlink(temp).catch(() => undefined)
      throw error
    }
  }

  const commitAtomicBatch = async (changes: AtomicChange[], canonicalTargets: string[]) => {
    if (new Set(canonicalTargets.map(normalizePath)).size !== changes.length) {
      throw new AtomicConflict("atomic batch contains duplicate target paths")
    }
    const actionIds = new Set(changes.map((change) => change.actionId).filter(Boolean))
    if (actionIds.size > 1) throw new AtomicConflict("atomic batch contains multiple action ids")
    if (actionIds.size === 1 && changes.some((change) => !change.actionId)) {
      throw new AtomicConflict("atomic batch contains an unbound target")
    }
    const historyContexts = new Set(changes.map((change) => change.history).filter(Boolean))
    if (historyContexts.size > 1 || (historyContexts.size === 1 && changes.some((change) => !change.history))) {
      throw new AtomicConflict("atomic batch contains multiple history owners")
    }
    const staged = new Map<string, string>()
    const backups = new Map<string, string>()
    const installed = new Set<string>()
    const retainedBackups = new Set<string>()
    let historyCapture: { context: HistoryContext; actionId: string } | undefined
    let committed = false
    const syncDirectories = async () => {
      if (process.platform === "win32") return
      for (const directory of new Set(canonicalTargets.map(dirname))) {
        const handle = await NFS.open(directory, "r").catch(() => undefined)
        if (!handle) continue
        try {
          await handle.sync().catch(() => undefined)
        } finally {
          await handle.close()
        }
      }
    }
    const assertCanonicalTarget = (target: string, expected: string) => {
      if (normalizePath(resolve(target)) !== normalizePath(expected)) {
        throw new AtomicConflict(`${target}: canonical target changed during write`)
      }
    }
    try {
      for (const [index, change] of changes.entries()) {
        const target = canonicalTargets[index]!
        assertCanonicalTarget(change.path, target)
        const before = await fileFingerprint(target)
        if (change.requireMissing && before !== undefined) {
          throw new AtomicConflict(`${change.path}: expected a missing file`)
        }
        if (change.expectedFingerprint !== undefined && before !== change.expectedFingerprint) {
          throw new AtomicConflict(`${change.path}: changed since it was read`)
        }
        const beforeMode = before === undefined ? undefined : (await NFS.stat(target)).mode
        if (change.expectedMode !== undefined && beforeMode !== change.expectedMode) {
          throw new AtomicConflict(`${change.path}: mode changed since it was read`)
        }
        const mode = change.mode ?? beforeMode
        if (change.content !== null) staged.set(target, await stage({ ...change, path: target }, mode))
      }

      historyCapture = await beginHistoryCapture(changes[0]!, canonicalTargets)

      for (const [index, change] of changes.entries()) {
        const target = canonicalTargets[index]!
        assertCanonicalTarget(change.path, target)
        const current = await fileFingerprint(target)
        if (change.requireMissing && current !== undefined) {
          throw new AtomicConflict(`${change.path}: created by another writer`)
        }
        if (change.expectedFingerprint !== undefined && current !== change.expectedFingerprint) {
          throw new AtomicConflict(`${change.path}: changed during the batch`)
        }
        const currentMode = current === undefined ? undefined : (await NFS.stat(target)).mode
        if (change.expectedMode !== undefined && currentMode !== change.expectedMode) {
          throw new AtomicConflict(`${change.path}: mode changed during the batch`)
        }
        if (current !== undefined) {
          const backup = join(dirname(target), `.${basename(target)}.bak.${randomUUID()}`)
          await NFS.rename(target, backup)
          backups.set(target, backup)
        }
        if (change.content !== null) {
          const temp = staged.get(target)!
          if (change.requireMissing) {
            try {
              await NFS.link(temp, target)
            } catch (error: any) {
              if (error?.code === "EEXIST") {
                throw new AtomicConflict(`${change.path}: created by another writer`)
              }
              throw error
            }
            installed.add(target)
            await NFS.unlink(temp)
              .then(() => staged.delete(target))
              .catch(() => undefined)
          } else {
            await NFS.rename(temp, target)
            staged.delete(target)
            installed.add(target)
          }
        }
      }
      await syncDirectories()
      committed = true
    } catch (error) {
      const failures: string[] = []
      for (let index = changes.length - 1; index >= 0; index--) {
        const change = changes[index]!
        const target = canonicalTargets[index]!
        const backup = backups.get(target)
        try {
          if (installed.has(target)) await NFS.unlink(target)
          if (backup) await NFS.rename(backup, target)
        } catch (rollbackError) {
          failures.push(`${change.path}: ${String(rollbackError)}`)
          if (backup && (await NFS.stat(backup).then(() => true).catch(() => false))) {
            retainedBackups.add(backup)
          }
        }
      }
      await syncDirectories()
      if (historyCapture) await finishHistoryCapture(historyCapture, canonicalTargets, false)
      if (failures.length) {
        throw new AtomicRollbackError(failures, [...retainedBackups].sort(), { cause: error })
      }
      throw error
    } finally {
      if (committed && historyCapture) await finishHistoryCapture(historyCapture, canonicalTargets, true)
      await Promise.all(
        [...staged.values(), ...(committed ? backups.values() : [])]
          .filter((target) => !retainedBackups.has(target))
          .map((target) => NFS.unlink(target).catch(() => undefined)),
      )
    }
  }

  const atomicBatchImpl = async (changes: AtomicChange[]) => {
    const canonicalTargets = changes.map((change) => resolve(change.path))
    const keys = [...new Set(canonicalTargets.map(normalizePath))].sort()
    const leases: Flock.Lease[] = []
    try {
      for (const key of keys) {
        leases.push(
          await Flock.acquire(key, {
            dir: join(os.tmpdir(), "open-clank-file-locks"),
            timeoutMs: 30_000,
          }),
        )
      }
      return await commitAtomicBatch(changes, canonicalTargets)
    } finally {
      for (const lease of leases.reverse()) await lease.release()
    }
  }

  export const layer = Layer.effect(
    Service,
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem

      const existsSafe = Effect.fn("FileSystem.existsSafe")(function* (path: string) {
        return yield* fs.exists(path).pipe(Effect.orElseSucceed(() => false))
      })

      const isDir = Effect.fn("FileSystem.isDir")(function* (path: string) {
        const info = yield* fs.stat(path).pipe(Effect.catch(() => Effect.void))
        return info?.type === "Directory"
      })

      const isFile = Effect.fn("FileSystem.isFile")(function* (path: string) {
        const info = yield* fs.stat(path).pipe(Effect.catch(() => Effect.void))
        return info?.type === "File"
      })

      const readDirectoryEntries = Effect.fn("FileSystem.readDirectoryEntries")(function* (dirPath: string) {
        return yield* Effect.tryPromise({
          try: async () => {
            const entries = await NFS.readdir(dirPath, { withFileTypes: true })
            return entries.map(
              (e): DirEntry => ({
                name: e.name,
                type: e.isDirectory() ? "directory" : e.isSymbolicLink() ? "symlink" : e.isFile() ? "file" : "other",
              }),
            )
          },
          catch: (cause) => new FileSystemError({ method: "readDirectoryEntries", cause }),
        })
      })

      const readJson = Effect.fn("FileSystem.readJson")(function* (path: string) {
        const text = yield* fs.readFileString(path)
        return JSON.parse(text)
      })

      const writeJson = Effect.fn("FileSystem.writeJson")(function* (path: string, data: unknown, mode?: number) {
        const content = JSON.stringify(data, null, 2)
        yield* fs.writeFileString(path, content)
        if (mode) yield* fs.chmod(path, mode)
      })

      const ensureDir = Effect.fn("FileSystem.ensureDir")(function* (path: string) {
        yield* fs.makeDirectory(path, { recursive: true })
      })

      const writeWithDirs = Effect.fn("FileSystem.writeWithDirs")(function* (
        path: string,
        content: string | Uint8Array,
        mode?: number,
      ) {
        const write = typeof content === "string" ? fs.writeFileString(path, content) : fs.writeFile(path, content)

        yield* write.pipe(
          Effect.catchIf(
            (e) => e.reason._tag === "NotFound",
            () =>
              Effect.gen(function* () {
                yield* fs.makeDirectory(dirname(path), { recursive: true })
                yield* write
              }),
          ),
        )
        if (mode) yield* fs.chmod(path, mode)
      })

      const canonicalTarget = Effect.fn("FileSystem.canonicalTarget")(function* (target: string) {
        return yield* Effect.try({
          try: () => resolve(target),
          catch: (cause) => new FileSystemError({ method: "canonicalTarget", cause }),
        })
      })

      const readTextSnapshot = Effect.fn("FileSystem.readTextSnapshot")(function* (target: string) {
        return yield* Effect.tryPromise({
          try: () => readSnapshot(target),
          catch: (cause) => new FileSystemError({ method: "readTextSnapshot", cause }),
        })
      })

      const atomicBatch = Effect.fn("FileSystem.atomicBatch")(function* (changes: AtomicChange[]) {
        yield* Effect.tryPromise({
          try: () => atomicBatchImpl(changes),
          catch: (cause) =>
            cause instanceof AtomicConflict || cause instanceof AtomicRollbackError
              ? cause
              : new FileSystemError({ method: "atomicBatch", cause }),
        })
      })

      const atomicWrite = Effect.fn("FileSystem.atomicWrite")(function* (change: AtomicChange) {
        yield* atomicBatch([change])
        return (yield* Effect.tryPromise({
          try: () => fileFingerprint(change.path),
          catch: (cause) => new FileSystemError({ method: "atomicWrite", cause }),
        }))!
      })

      const glob = Effect.fn("FileSystem.glob")(function* (pattern: string, options?: Glob.Options) {
        return yield* Effect.tryPromise({
          try: () => Glob.scan(pattern, options),
          catch: (cause) => new FileSystemError({ method: "glob", cause }),
        })
      })

      const findUp = Effect.fn("FileSystem.findUp")(function* (target: string, start: string, stop?: string) {
        const result: string[] = []
        let current = start
        while (true) {
          const search = join(current, target)
          if (yield* fs.exists(search)) result.push(search)
          if (stop === current) break
          const parent = dirname(current)
          if (parent === current) break
          current = parent
        }
        return result
      })

      const up = Effect.fn("FileSystem.up")(function* (options: { targets: string[]; start: string; stop?: string }) {
        const result: string[] = []
        let current = options.start
        while (true) {
          for (const target of options.targets) {
            const search = join(current, target)
            if (yield* fs.exists(search)) result.push(search)
          }
          if (options.stop === current) break
          const parent = dirname(current)
          if (parent === current) break
          current = parent
        }
        return result
      })

      const globUp = Effect.fn("FileSystem.globUp")(function* (pattern: string, start: string, stop?: string) {
        const result: string[] = []
        let current = start
        while (true) {
          const matches = yield* glob(pattern, { cwd: current, absolute: true, include: "file", dot: true }).pipe(
            Effect.catch(() => Effect.succeed([] as string[])),
          )
          result.push(...matches)
          if (stop === current) break
          const parent = dirname(current)
          if (parent === current) break
          current = parent
        }
        return result
      })

      return Service.of({
        ...fs,
        existsSafe,
        isDir,
        isFile,
        readDirectoryEntries,
        readJson,
        writeJson,
        ensureDir,
        writeWithDirs,
        canonicalTarget,
        readTextSnapshot,
        atomicWrite,
        atomicBatch,
        findUp,
        up,
        globUp,
        glob,
        globMatch: Glob.match,
      })
    }),
  )

  export const defaultLayer = layer.pipe(Layer.provide(NodeFileSystem.layer))

  // Pure helpers that don't need Effect (path manipulation, sync operations)
  export function mimeType(p: string): string {
    return lookup(p) || "application/octet-stream"
  }

  export function normalizePath(p: string): string {
    if (process.platform !== "win32") return p
    const resolved = pathResolve(windowsPath(p))
    try {
      return realpathSync.native(resolved)
    } catch {
      return resolved
    }
  }

  export function normalizePathPattern(p: string): string {
    if (process.platform !== "win32") return p
    if (p === "*") return p
    const match = p.match(/^(.*)[\\/]\*$/)
    if (!match) return normalizePath(p)
    const dir = /^[A-Za-z]:$/.test(match[1]) ? match[1] + "\\" : match[1]
    return join(normalizePath(dir), "*")
  }

  export function resolve(p: string): string {
    const resolved = pathResolve(windowsPath(p))
    const suffix: string[] = []
    let cursor = resolved
    while (true) {
      try {
        return normalizePath(join(realpathSync(cursor), ...suffix))
      } catch (e: any) {
        if (e?.code !== "ENOENT") throw e
        const parent = dirname(cursor)
        if (parent === cursor) return normalizePath(resolved)
        suffix.unshift(basename(cursor))
        cursor = parent
      }
    }
  }

  export function windowsPath(p: string): string {
    if (process.platform !== "win32") return p
    return p
      .replace(/^\/([a-zA-Z]):(?:[\\/]|$)/, (_, drive) => `${drive.toUpperCase()}:/`)
      .replace(/^\/([a-zA-Z])(?:\/|$)/, (_, drive) => `${drive.toUpperCase()}:/`)
      .replace(/^\/cygdrive\/([a-zA-Z])(?:\/|$)/, (_, drive) => `${drive.toUpperCase()}:/`)
      .replace(/^\/mnt\/([a-zA-Z])(?:\/|$)/, (_, drive) => `${drive.toUpperCase()}:/`)
  }

  export function overlaps(a: string, b: string) {
    const relA = relative(resolve(a), resolve(b))
    const relB = relative(resolve(b), resolve(a))
    const contains = (rel: string) =>
      rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`))
    return contains(relA) || contains(relB)
  }

  export function contains(parent: string, child: string) {
    const rel = relative(resolve(parent), resolve(child))
    return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`))
  }
}
