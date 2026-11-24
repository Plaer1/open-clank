import { NodeFileSystem } from "@effect/platform-node"
import { basename, dirname, isAbsolute, join, relative, resolve as pathResolve, sep, win32 } from "path"
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
    status?: "prepared" | "complete" | "paused" | "skipped" | "failed" | "aborted" | "unconfigured"
    action_id?: string
    history_status: "prepared" | "complete" | "paused" | "skipped" | "failed" | "aborted"
    capture_phase: string
    error?: string
    durable?: boolean
    coverage?: string
    managed_action_ref?: string
    observed_membership?: "pending"
  }

  export type ManagedToolOriginV1 = {
    version: 1; host_chat_id: string; engine_session_id: string; root_operation_id: string;
    actor_id: string; execution_revision: number; inner_turn_id: string; tool_call_id: string; assistant_message_id: string
  }

  /** Content-free facts from the authenticated Host's original Files reservation. */
  export type ManagedCaptureFact = {
    readonly source: { readonly accountID:string; readonly authorityWorkspaceID:string; readonly hostChatID:string;
      readonly sessionID:string; readonly actorID:string; readonly executionRevision:number; readonly rootOperationID:string;
      readonly innerTurnID:string; readonly toolCallID:string; readonly assistantMessageID:string }
    readonly capture: { readonly historyActorID:string; readonly historyWorkspaceID:string }
  }

  export type HistoryContext = {
    readonly actorId: string
    readonly accountId: string
    readonly workspaceId: string
    readonly workspaceRoot: string
    readonly rootId?: string
    readonly socketPath: string
    readonly token?: string
    readonly managedToolOrigin?: ManagedToolOriginV1
    readonly observedCaptureMembership?: {
      begin: (context: HistoryContext, actionID: string) => Promise<void>
      complete: (context: HistoryContext, actionID: string, actionRef: string) => Promise<void>
      beginManaged?: (fact: ManagedCaptureFact, actionID: string) => Promise<void>
      completeManaged?: (actionID: string, actionRef: string) => Promise<void>
      currentManaged?: () => boolean
    }
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
    const rootId = typeof value.rootId === "string" ? value.rootId.trim() : undefined
    const socketPath = typeof value.socketPath === "string" ? value.socketPath.trim() : ""
    const token = typeof value.token === "string" ? value.token.trim() : ""
    if (!actorId || !accountId || !workspaceId || !workspaceRoot || !socketPath || !token) return undefined
    if (!isAbsolute(workspaceRoot) || !isAbsolute(socketPath) || /[\\/](?:\.env|credentials?|secrets?)(?:[\\/]|$)/i.test(workspaceRoot)) return undefined
    const normalized = resolve(target)
    const root = resolve(workspaceRoot)
    if (!(normalized === root || normalized.startsWith(`${root}${sep}`))) return undefined
    const origin = value.managedToolOrigin
    const fields = ["version", "host_chat_id", "engine_session_id", "root_operation_id", "actor_id", "execution_revision", "inner_turn_id", "tool_call_id", "assistant_message_id"]
    const validOrigin = origin && typeof origin === "object" && !Array.isArray(origin) && Object.keys(origin).length === fields.length && fields.every((key) => Object.hasOwn(origin, key)) &&
      (origin as any).version === 1 && Number.isSafeInteger((origin as any).execution_revision) && (origin as any).execution_revision >= 0 &&
      fields.filter((key) => key !== "version" && key !== "execution_revision").every((key) => typeof (origin as any)[key] === "string" && Buffer.byteLength((origin as any)[key], "utf8") <= 256 && (origin as any)[key].length > 0 && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/.test((origin as any)[key]))
    return {
      actorId,
      accountId,
      workspaceId,
      workspaceRoot: root,
      rootId,
      socketPath,
      token,
      ...(validOrigin ? { managedToolOrigin: origin as ManagedToolOriginV1 } : {}),
      ...(validOrigin && ctx.extra?.observedCaptureMembership && typeof ctx.extra.observedCaptureMembership === "object"
        ? { observedCaptureMembership: ctx.extra.observedCaptureMembership as HistoryContext["observedCaptureMembership"] } : {}),
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
      let size = 0
      socket.on("data", (chunk) => { size += chunk.length; if (size > 2 * 1024 * 1024) { socket.destroy(); finish(new Error("history IPC response exceeds limit")); return } chunks.push(Buffer.from(chunk)) })
      socket.on("end", () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString("utf8"))
          if (value?.Error || value?.error || value?.code === "error") {
            throw new Error(String(value.Error?.code ?? value.Error ?? value.error ?? value.code))
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

  /** Internal bounded History reads; callers must revalidate their current host scope. */
  export const historyActionRead = (context: HistoryContext, actionId: string, request:
    | { kind: "resource"; resourceID: string }
    | { kind: "action" }
    | { kind: "info"; receipt: unknown }
    | { kind: "chunk"; receipt: unknown; offset: number; length: number }) => {
    const envelope = { protocol_version: 1, auth: historyAuth(context), action_id: actionId }
    const body = request.kind === "resource" ? { ResolveResource: { envelope, resource_id: request.resourceID } }
      : request.kind === "action" ? { GetCaptureAction: envelope }
      : request.kind === "info" ? { ReadVersionInfo: { envelope, receipt: request.receipt } }
      : { ReadVersionChunk: { envelope, receipt: request.receipt, offset: request.offset, length: request.length } }
    return historyRequest(context.socketPath, body)
  }

  const historyEnvelope = (context: HistoryContext, actionId: string, path: string, operation: string, before: string | undefined, paths: string[]) => {
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
      managed_tool_origin_v1: context.managedToolOrigin,
      session_id: null,
      run_id: null,
      task_id: null,
      tool_id: "mimo-filesystem",
      before_revision: before ? { Opaque: { kind: "fingerprint", value: before } } : null,
      expected_after_revision: null,
      original_locator: { display_name: basename(path), location_label: resolve(path), opaque_ref: null },
      destination_locator: null,
      timestamp_millis: Date.now(),
      coverage: {
        metadata: { required_recovery: true },
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

  type HistoryTarget = {
    readonly path: string
    readonly resource: Record<string, string>
    readonly before: Buffer | undefined
    readonly stream?: { fingerprint: string; size: number; identity: FileIdentity }
    readonly resourceType: "File" | "Directory" | "Symlink"
    readonly metadata: Record<string, unknown>
  }

  type FileIdentity = { dev: number; ino: number; size: number; mtimeMs: number; ctimeMs: number; mode: number }
  const sameFile = (a: FileIdentity, b: FileIdentity) => a.dev === b.dev && a.ino === b.ino && a.size === b.size && a.mtimeMs === b.mtimeMs && a.ctimeMs === b.ctimeMs && a.mode === b.mode
  // Both the opened object and the named target must remain the same across each pass.
  const streamFile = async (target: string, expected?: FileIdentity, visit?: (chunk: Buffer, offset: number) => Promise<void>) => {
    const handle = await NFS.open(target, "r")
    try {
      const first = await handle.stat()
      if (!first.isFile() || !Number.isSafeInteger(first.size) || first.size < 0 || (expected && !sameFile(first, expected))) throw new AtomicConflict(`${target}: file changed before History capture`)
      if (!sameFile(first, await NFS.stat(target))) throw new AtomicConflict(`${target}: target changed before History capture`)
      const digest = createHash("sha256"), buffer = Buffer.allocUnsafe(512 * 1024)
      let offset = 0
      while (offset < first.size) {
        const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, first.size - offset), offset)
        if (!bytesRead) throw new AtomicConflict(`${target}: file shortened during History capture`)
        const chunk = buffer.subarray(0, bytesRead)
        digest.update(chunk)
        if (visit) await visit(chunk, offset)
        offset += bytesRead
      }
      if (!sameFile(first, await handle.stat()) || !sameFile(first, await NFS.stat(target))) throw new AtomicConflict(`${target}: file changed during History capture`)
      return { fingerprint: `sha256:${digest.digest("hex")}:${offset}`, size: offset, identity: first }
    } finally { await handle.close() }
  }

  const directoryManifest = async (target: string): Promise<Buffer> => {
    const root = await NFS.realpath(target)
    const entries: Record<string, unknown>[] = []
    const visit = async (current: string) => {
      const children = (await NFS.readdir(current, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))
      for (const child of children) {
        const item = join(current, child.name)
        const relativePath = relative(root, item).split(sep).join("/")
        const info = await NFS.lstat(item)
        const record: Record<string, unknown> = {
          path: relativePath,
          mode: info.mode & 0o7777,
          mtime_millis: Math.trunc(info.mtimeMs),
        }
        if (info.isSymbolicLink()) {
          record.type = "symlink"
          record.target = await NFS.readlink(item)
        } else if (info.isDirectory()) {
          record.type = "directory"
          await visit(item)
        } else {
          const bytes = await NFS.readFile(item)
          record.type = "file"
          record.size = bytes.byteLength
          record.sha256 = createHash("sha256").update(bytes).digest("hex")
          record.content = bytes.toString("base64")
        }
        entries.push(record)
      }
    }
    await visit(root)
    return Buffer.from(JSON.stringify({ version: 1, root_type: "directory", entries }))
  }

  const readHistoryTarget = async (target: string): Promise<HistoryTarget> => {
    const resource = {
      account_id: "",
      workspace_id: "",
      provider: "mimo-filesystem",
      resource_id: "",
    }
    try {
      const info = await NFS.lstat(target)
      let before: Buffer
      let resourceType: HistoryTarget["resourceType"]
      if (info.isSymbolicLink()) {
        before = Buffer.from(await NFS.readlink(target))
        resourceType = "Symlink"
      } else if (info.isDirectory()) {
        before = await directoryManifest(target)
        resourceType = "Directory"
      } else {
        if (info.isFile() && info.size > 256 * 1024) {
          const stream = await streamFile(target, info)
          return { path: target, resource, before: undefined, stream, resourceType: "File", metadata: { mode: info.mode, size: info.size, modified_millis: Math.trunc(info.mtimeMs) } }
        }
        before = await NFS.readFile(target)
        resourceType = "File"
      }
      return { path: target, resource, before, resourceType, metadata: { mode: info.mode, size: resourceType === "Directory" ? null : info.size, modified_millis: Math.trunc(info.mtimeMs) } }
    } catch (error: any) {
      if (error?.code !== "ENOENT") throw error
      return { path: target, resource, before: undefined, resourceType: "File", metadata: { mode: null, size: null, modified_millis: null } }
    }
  }

  const historyResource = async (context: HistoryContext, target: string) => {
    const response = await historyRequest(context.socketPath, {
      RegisterResource: {
        envelope: { protocol_version: 1, auth: historyAuth(context), action_id: `resource-${randomUUID()}` },
        account_id: context.accountId,
        workspace_id: context.workspaceId,
        root_id: context.rootId ?? null,
        root_path: context.workspaceRoot,
        relative_path: relative(context.workspaceRoot, resolve(target)).split(sep).join("/"),
        resource_id: null,
      },
    })
    const handle = response?.Resource?.handle
    if (!handle?.resource_id || handle.account_id !== context.accountId || handle.workspace_id !== context.workspaceId) {
      throw new Error("history service returned no trusted resource handle")
    }
    return {
      account_id: handle.account_id,
      workspace_id: handle.workspace_id,
      provider: "mimo-filesystem",
      resource_id: handle.resource_id,
    }
  }

  class HistoryBatchOutcomeUnknown extends Error {}

  // Keep every History IPC frame below the service's fixed 1-MiB limit.
  // A batch consumes its staged IDs only after dispatch, so unknown dispatch
  // outcomes are never cleaned up or reissued here.
  const historyBatchRequest = async (context: HistoryContext, actionId: string, entries: any[], makeBody: (entries: any[]) => any) => {
    const maxFrame = 1024 * 1024, chunkSize = 512 * 1024
    const envelope = { protocol_version: 1, auth: historyAuth(context), action_id: actionId }
    const knownUnconsumed: string[] = []
    let dispatched = false
    const request = async (body: unknown) => {
      if (Buffer.byteLength(JSON.stringify(body), "utf8") + 1 > maxFrame) throw new Error("history IPC frame exceeds limit")
      return historyRequest(context.socketPath, body)
    }
    try {
      const wire = entries.map(({ _stream, ...entry }) => ({ ...entry }))
      for (let i = 0; i < entries.length; i++) {
        const source = entries[i]._stream as HistoryTarget | undefined
        if (!source?.stream) continue
        const upload = `stage-${randomUUID()}`, expected = source.stream
        const begin = await request({StageBegin:{envelope,upload_id:upload,content_length:expected.size,fingerprint:expected.fingerprint}})
        if (begin !== "Accepted") throw new Error("history stage admission unavailable")
        knownUnconsumed.push(upload)
        const actual = await streamFile(source.path, expected.identity, async (chunk, offset) => {
          const result = await request({StageChunk:{envelope,upload_id:upload,offset,content:chunk.toString("base64")}})
          if (result !== "Accepted") throw new Error("history stage write unavailable")
        })
        if (actual.fingerprint !== expected.fingerprint) throw new AtomicConflict(`${source.path}: file content changed during History capture`)
        const finished = await request({StageFinish:{envelope,upload_id:upload}})
        if (finished?.Staged?.upload_id !== upload || finished?.Staged?.content_length !== expected.size) throw new Error("history stage finish unavailable")
        wire[i].content = null; wire[i].staged_upload_id = upload
      }
      const body = () => makeBody(wire)
      while (Buffer.byteLength(JSON.stringify(body()), "utf8") + 1 > maxFrame) {
        let index = -1, largest = -1
        for (let i=0; i<wire.length; i++) {
          if (typeof wire[i].content === "string" && wire[i].content.length > largest) { index=i; largest=wire[i].content.length }
        }
        if (index < 0) throw new Error("history batch metadata exceeds frame limit")
        const item=wire[index], content=Buffer.from(item.content, "base64")
        const fingerprint=fingerprintBytes(content), upload=`stage-${randomUUID()}`
        if (item.fingerprint !== fingerprint) throw new Error("history staged fingerprint changed")
        const begin=await request({StageBegin:{envelope,upload_id:upload,content_length:content.length,fingerprint}})
        if (begin !== "Accepted") throw new Error("history stage admission unavailable")
        knownUnconsumed.push(upload)
        for (let offset=0; offset<content.length; offset+=chunkSize) {
          const result=await request({StageChunk:{envelope,upload_id:upload,offset,content:content.subarray(offset,offset+chunkSize).toString("base64")}})
          if (result !== "Accepted") throw new Error("history stage write unavailable")
        }
        const finished=await request({StageFinish:{envelope,upload_id:upload}})
        if (finished?.Staged?.upload_id !== upload || finished?.Staged?.content_length !== content.length) throw new Error("history stage finish unavailable")
        item.content=null; item.staged_upload_id=upload
      }
      for (const entry of entries) {
        const source = entry._stream as HistoryTarget | undefined
        if (source?.stream && !sameFile(source.stream.identity, await NFS.stat(source.path))) throw new AtomicConflict(`${source.path}: target changed before History batch`)
      }
      const payload=body()
      if (Buffer.byteLength(JSON.stringify(payload), "utf8") + 1 > maxFrame) throw new Error("history batch frame exceeds limit")
      dispatched=true
      return await request(payload)
    } catch (error) {
      if (!dispatched) {
        for (const upload of knownUnconsumed) await request({StageAbort:{envelope,upload_id:upload}}).catch(() => undefined)
        throw error
      }
      throw new HistoryBatchOutcomeUnknown(`history batch outcome unknown: ${String(error)}`)
    }
  }

  const beginHistoryCapture = async (change: AtomicChange, canonicalTargets: string[]) => {
    const context = change.history
    const actionId = change.actionId
    if (!context || !actionId) return undefined
    const targets = await Promise.all(canonicalTargets.map((target) => readHistoryTarget(target)))
    const before = targets[0]?.stream?.fingerprint ?? (targets[0]?.before ? fingerprintBytes(targets[0].before) : undefined)
    const operation = before ? "replace" : "create"
    const resources = await Promise.all(targets.map((target) => historyResource(context, target.path)))
    const envelope = historyEnvelope(context, actionId, canonicalTargets[0]!, operation, before, canonicalTargets.slice(1))
    envelope.resource_key = resources[0]
    envelope.modified_resource_ids = resources
    const entries = targets.map((target, index) => {
      const resource = resources[index]!
      target.resource.account_id = resource.account_id
      target.resource.workspace_id = resource.workspace_id
      target.resource.resource_id = resource.resource_id
      const fingerprint = target.stream?.fingerprint ?? (target.before ? fingerprintBytes(target.before) : "missing")
      const digest = fingerprint === "missing" ? null : fingerprint.slice(0, fingerprint.lastIndexOf(":"))
      return {
        resource_key: resource,
        old_locator: { display_name: basename(target.path), location_label: relative(context.workspaceRoot, target.path), opaque_ref: resource.resource_id },
        new_locator: { display_name: basename(target.path), location_label: relative(context.workspaceRoot, target.path), opaque_ref: resource.resource_id },
        expected_revision: { Opaque: { kind: "fingerprint", value: fingerprint } },
        existence: fingerprint !== "missing" ? "Present" : "Absent",
        resource_type: target.resourceType,
        metadata: { ...target.metadata, opaque: target.resourceType === "Directory" ? { manifest_digest: digest } : null },
        content: target.before ? Buffer.from(target.before).toString("base64") : null,
        staged_upload_id: null,
        _stream: target.stream ? target : undefined,
        fingerprint: fingerprint,
        coverage: { byte_len: target.stream?.size ?? target.before?.byteLength ?? 0, content_digest: digest, metadata: { exact_preimage: true } },
      }
    })
    try {
      const prepared = await historyBatchRequest(context, actionId, entries, (items) => ({ PrepareBatch: { envelope: { protocol_version: 1, auth: historyAuth(context), claimed_digest: "", request: envelope }, batch_version: 1, entries: items } }))
      const record=prepared?.Action, batch=record?.mutation_batch, acknowledgement=batch?.prepare
      const keyOf=(key:any) => JSON.stringify([key?.account_id,key?.workspace_id,key?.provider,key?.resource_id])
      const expected=new Map(entries.map((entry) => [keyOf(entry.resource_key),entry]))
      const covers=(actual:any) => {
        if (!Array.isArray(actual) || actual.length !== expected.size) return false
        const seen=new Set<string>()
        return actual.every((item:any) => {
          const key=keyOf(item?.resource_key), wanted=expected.get(key)
          if (!wanted || seen.has(key)) return false
          seen.add(key)
          return item.existence===wanted.existence && item.resource_type===wanted.resource_type && item.fingerprint===wanted.fingerprint &&
            item.before?.fingerprint===wanted.fingerprint && typeof item.before?.version_id==="string" && !!item.before.version_id
        })
      }
      if (expected.size!==entries.length || record?.action_id!==actionId || record?.actor_id!==context.actorId ||
          record?.actor_account_id!==context.accountId || record?.state!=="BeforeDurable" ||
          acknowledgement?.batch_version!==1 || acknowledgement?.parent_action_id!==actionId ||
          acknowledgement?.resource_count!==entries.length || acknowledgement?.acknowledged!==true ||
          !covers(record?.before_resources) || !covers(batch?.resources)) throw new HistoryBatchOutcomeUnknown("history preparation receipt unavailable")
      if (!record.recovery_only && context.observedCaptureMembership) {
        try {
          await context.observedCaptureMembership.begin(context, actionId)
        } catch (error) {
          await historyRequest(context.socketPath, { RecordLive: {
            envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId },
            receipt: { action_id: actionId, status: "NotCommitted", fingerprint: null, after_unavailable: false },
          } }).catch(() => undefined)
          throw error
        }
      }
      context.status = Object.assign(context.status, { status: "prepared", action_id: actionId, history_status: "prepared", capture_phase: "before_durable", durable: true, coverage: "ExactBatch" })
      return { context, actionId, before, targets }
    } catch (error) {
      const unknown=error instanceof HistoryBatchOutcomeUnknown
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: unknown ? "before_outcome_unknown" : "before_failed", durable: false, coverage: unknown ? "Unknown" : "NoCapture", error: String(error) })
      throw new AtomicConflict(`history prepare failed before live mutation: ${String(error)}`)
    }
  }

  const finishHistoryCapture = async (capture: { context: HistoryContext; actionId: string; targets: HistoryTarget[] }, canonicalTargets: string[], committed: boolean) => {
    const { context, actionId } = capture
    if (!committed) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "live_not_committed", durable: false, coverage: "BeforeOnly" })
      await historyRequest(context.socketPath, { RecordLive: { envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId }, receipt: { action_id: actionId, status: "NotCommitted", fingerprint: null, after_unavailable: false } } }).catch(() => undefined)
      return
    }
    try {
      const afterTargets = await Promise.all(canonicalTargets.map((target) => readHistoryTarget(target)))
      const primary = afterTargets[0]!
      const afterFingerprint = primary.stream?.fingerprint ?? (primary.before ? fingerprintBytes(primary.before) : "missing")
      await historyRequest(context.socketPath, { RecordLive: { envelope: { protocol_version: 1, auth: historyAuth(context), action_id: actionId }, receipt: { action_id: actionId, status: "Committed", fingerprint: afterFingerprint, after_unavailable: false } } })
      const entries = await Promise.all(afterTargets.map(async (target) => {
        const resource = await historyResource(context, target.path)
        const fingerprint = target.stream?.fingerprint ?? (target.before ? fingerprintBytes(target.before) : "missing")
        const digest = fingerprint === "missing" ? null : fingerprint.slice(0, fingerprint.lastIndexOf(":"))
        return {
          resource_key: resource,
          locator: { display_name: basename(target.path), location_label: relative(context.workspaceRoot, target.path), opaque_ref: resource.resource_id },
          existence: fingerprint !== "missing" ? "Present" : "Absent",
          resource_type: target.resourceType,
          metadata: { ...target.metadata, opaque: target.resourceType === "Directory" ? { manifest_digest: digest } : null },
          content: target.before ? target.before.toString("base64") : null,
          staged_upload_id: null,
          _stream: target.stream ? target : undefined,
          fingerprint: fingerprint,
          coverage: { byte_len: target.stream?.size ?? target.before?.byteLength ?? 0, content_digest: digest, metadata: { exact_after: true } },
          outcome: { resource_id: resource.resource_id, status: "Committed", revision: { Opaque: { kind: "fingerprint", value: fingerprint } } },
        }
      }))
      const control = { protocol_version: 1, auth: historyAuth(context), action_id: actionId }
      const completed = await historyBatchRequest(context, actionId, entries, (items) => ({ CompleteBatch: { envelope: control, batch_version: 1, entries: items } })).catch(async (error) => {
        if (!(error instanceof HistoryBatchOutcomeUnknown)) throw error
        // The service may already have committed the batch. Read its original
        // terminal receipt without replaying either the file write or the batch.
        return historyRequest(context.socketPath, { GetCaptureAction: control })
      })
      if (completed?.Action?.state !== "Complete" || completed.Action.action_id !== actionId) throw new Error("history completion receipt unavailable")
      if (completed.Action.recovery_only === true) {
        const action = completed.Action
        const expected = new Map(entries.map((entry) => [entry.resource_key.resource_id, entry]))
        const seen = new Set<string>()
        if (action.actor_id !== context.actorId || action.actor_account_id !== context.accountId ||
            action.live?.action_id !== actionId || action.live?.status !== "Committed" ||
            action.live?.fingerprint !== afterFingerprint || !Array.isArray(action.after_resources) ||
            expected.size !== entries.length || action.after_resources.length !== expected.size ||
            !action.after_resources.every((item: any) => {
              const key = item?.resource_key, entry = expected.get(key?.resource_id)
              if (!entry || seen.has(key.resource_id) || key.provider !== "mimo-filesystem" ||
                  key.account_id !== context.accountId || key.workspace_id !== context.workspaceId ||
                  item.existence !== entry.existence || item.resource_type !== entry.resource_type ||
                  item.fingerprint !== entry.fingerprint || item.after?.fingerprint !== entry.fingerprint) return false
              seen.add(key.resource_id)
              return true
            })) throw new Error("private History completion receipt unavailable")
        const settlement = await historyRequest(context.socketPath, { SettleRecovery: control })
          .catch(() => historyRequest(context.socketPath, { GetCaptureAction: control }).catch(() => undefined))
        const receipt = settlement?.Action
        if (receipt?.action_id !== actionId || receipt.actor_id !== context.actorId ||
            receipt.actor_account_id !== context.accountId || receipt.recovery_only !== true ||
            receipt.recovery_settled !== true || receipt.state !== "Complete") {
          context.status = { status: "paused", history_status: "paused", capture_phase: "recovery_settlement_pending", action_id: actionId, durable: false }
          return
        }
        context.status = { status: "skipped", history_status: "skipped", capture_phase: "recovery_settled", action_id: actionId, durable: false }
        return
      }
      context.status = Object.assign(context.status, { status: "complete", action_id: actionId, history_status: "complete", capture_phase: "complete", durable: true })
      const record = completed?.Action
      const originFields = ["version", "host_chat_id", "engine_session_id", "root_operation_id", "actor_id", "execution_revision", "inner_turn_id", "tool_call_id", "assistant_message_id"] as const
      const storedOrigin = record?.managed_tool_origin_v1
      if (context.managedToolOrigin && canonicalTargets.length === 1 && record?.state === "Complete" &&
          record.action_id === actionId && record.actor_id === context.actorId && record.actor_account_id === context.accountId &&
          storedOrigin && typeof storedOrigin === "object" && Object.keys(storedOrigin).length === originFields.length &&
          originFields.every((key) => storedOrigin[key] === context.managedToolOrigin?.[key]) &&
          Array.isArray(record.before_resources) && record.before_resources.length === 1 &&
          Array.isArray(record.after_resources) && record.after_resources.length === 1) {
        const before = record.before_resources[0], after = record.after_resources[0]
        const resourceID = before?.resource_key?.resource_id
        if (typeof resourceID === "string" && resourceID.length <= 256 && resourceID.startsWith("file:") &&
            ["account_id", "workspace_id", "provider", "resource_id"].every((key) => before.resource_key[key] === after?.resource_key?.[key]) &&
            before.resource_key.account_id === context.accountId && before.resource_key.workspace_id === context.workspaceId &&
            typeof before.before?.version_id === "string" && typeof after.after?.version_id === "string" &&
            before.before.version_id.length <= 256 && after.after.version_id.length <= 256) {
          const payload = { version: 1, actionID: actionId, resourceID, beforeVersionID: before.before.version_id,
            afterVersionID: after.after.version_id, origin: context.managedToolOrigin }
          const locator = `oc-history-action:v1:${Buffer.from(JSON.stringify(payload)).toString("base64url")}`
          if (locator.length <= 4096) context.status.managed_action_ref = locator
        }
      } else if (context.managedToolOrigin && canonicalTargets.length > 1 && canonicalTargets.length <= 64 && record?.state === "Complete" &&
          record.action_id === actionId && record.actor_id === context.actorId && record.actor_account_id === context.accountId &&
          storedOrigin && typeof storedOrigin === "object" && Object.keys(storedOrigin).length === originFields.length &&
          originFields.every((key) => storedOrigin[key] === context.managedToolOrigin?.[key]) &&
          Array.isArray(record.before_resources) && record.before_resources.length === canonicalTargets.length &&
          Array.isArray(record.after_resources) && record.after_resources.length === canonicalTargets.length &&
          typeof record.request_digest === "string" && /^[a-f0-9]{64}$/.test(record.request_digest) &&
          typeof record.complete_request_digest === "string" && /^[a-f0-9]{64}$/.test(record.complete_request_digest)) {
        const ids = new Set<string>()
        const valid = record.before_resources.every((before: any, index: number) => {
          const key = before?.resource_key, after = record.after_resources[index], id = key?.resource_id
          if (typeof id !== "string" || !id.startsWith("file:") || ids.has(id) ||
              key.account_id !== context.accountId || key.workspace_id !== context.workspaceId || key.provider !== "mimo-filesystem" ||
              !["account_id", "workspace_id", "provider", "resource_id"].every((field) => key[field] === after?.resource_key?.[field]) ||
              typeof before.before?.version_id !== "string" || !before.before.version_id ||
              typeof after?.after?.version_id !== "string" || !after.after.version_id) return false
          ids.add(id); return true
        })
        if (valid) {
          const payload = { version: 2, actionID: actionId, requestDigest: record.request_digest,
            completeDigest: record.complete_request_digest, origin: context.managedToolOrigin }
          const locator = `oc-history-action:v2:${Buffer.from(JSON.stringify(payload)).toString("base64url")}`
          if (locator.length <= 4096) context.status.managed_action_ref = locator
        }
      }
    } catch (error) {
      context.status = Object.assign(context.status, { status: "failed", action_id: actionId, history_status: "failed", capture_phase: "after_failed", durable: false, error: String(error) })
      throw new AtomicConflict(`history reconciliation pending after committed mutation: ${String(error)}`)
    }
    if (context.observedCaptureMembership && !context.status.managed_action_ref)
      context.status = Object.assign(context.status, { observed_membership: "pending" })
    if (context.observedCaptureMembership && context.status.managed_action_ref) {
      try {
        await context.observedCaptureMembership.complete(context, actionId, context.status.managed_action_ref)
      } catch {
        // History already completed. Preserve its real receipt and leave the
        // transcript admission pending rather than encouraging another edit.
        context.status = Object.assign(context.status, { observed_membership: "pending" })
      }
    }
  }

  const fileFingerprint = async (target: string) => {
    try {
      return (await streamFile(target)).fingerprint
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
    let historyCapture: { context: HistoryContext; actionId: string; targets: HistoryTarget[] } | undefined
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
        const captured = historyCapture?.targets[index]
        if (captured) {
          const capturedFingerprint = captured.stream?.fingerprint ?? (captured.before ? fingerprintBytes(captured.before) : undefined)
          if (current !== capturedFingerprint) throw new AtomicConflict(`${target}: changed after History preparation`)
        }
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
    return contains(a, b) || contains(b, a)
  }

  export function contains(parent: string, child: string) {
    const rel = relative(resolve(parent), resolve(child))
    // Windows cross-drive path.relative returns the absolute `to` path, not "..".
    return rel === "" || (!isAbsolute(rel) && !win32.isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`))
  }
}
