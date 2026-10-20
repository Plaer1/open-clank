import crypto from "node:crypto"
import path from "node:path"
import fs from "node:fs/promises"
import z from "zod"
import { Effect } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Global } from "@/global"
import { Instance } from "@/project/instance"
import { SessionCwd } from "./session-cwd"
import { assertWriteAllowed, askEditUnlessMemory } from "./external-directory"
import { assertProjectFilePolicy } from "./project-policy"
import { RecoverableError } from "./recoverable"
import * as Tool from "./tool"
import { ManagedProvider } from "@/acp/managed-provider"

const Parameters = z.object({
  action: z.enum(["move", "delete", "restore", "list_trash"]),
  path: z.string().optional().describe("Source path, or optional restore destination"),
  destination: z.string().optional().describe("Destination path for move"),
  trash_id: z.string().regex(/^[a-f0-9]{24}$/).optional(),
  expected_fingerprint: z.string().optional(),
  cursor: z.number().int().nonnegative().optional(),
  limit: z.number().int().min(1).max(100).optional(),
})

type Manifest = {
  id: string
  original_path: string
  deleted_at: number
  expires_at: number
  owner: string
  workspace: string
  fingerprint: string
  mode: number
  size: number
}

function requestedPath(value: string, sessionID: Tool.Context["sessionID"]) {
  return path.isAbsolute(value) ? value : path.resolve(SessionCwd.get(sessionID), value)
}

function trashScope(sessionID: Tool.Context["sessionID"]) {
  const scopedOwner = ManagedProvider.enabled()
    ? ManagedProvider.managedSessionOwner(sessionID).trim().toLowerCase()
    : process.env.OPEN_CLANK_OWNER?.trim().toLowerCase() || process.env.FM_OWNER?.trim().toLowerCase()
  if (process.env.OPEN_CLANK_MANAGED === "1" && !scopedOwner) {
    throw new RecoverableError("manage_files: managed sessions require an authenticated owner")
  }
  const owner = scopedOwner || "local"
  const workspace = AppFileSystem.resolve(SessionCwd.get(sessionID))
  if (workspace === path.parse(workspace).root) {
    throw new RecoverableError("manage_files: filesystem root cannot be the active workspace")
  }
  const ownerKey = crypto.createHash("sha256").update(owner).digest("hex").slice(0, 20)
  const workspaceKey = crypto.createHash("sha256").update(workspace).digest("hex").slice(0, 20)
  return {
    owner,
    workspace,
    root: path.join(Global.Path.data, "file-trash", ownerKey, workspaceKey),
  }
}

function answer(title: string, metadata: Tool.Metadata, output: string): Tool.ExecuteResult {
  return { title, metadata, output }
}

export const ManageFilesTool = Tool.define(
  "manage_files",
  Effect.gen(function* () {
    const afs = yield* AppFileSystem.Service

    return {
      description:
        "Move files without overwriting, or delete them to recoverable trash. Supports restore and paginated trash listing.",
      parameters: Parameters,
      resources: (params: z.infer<typeof Parameters>, ctx: Tool.Context) => {
        const scope = trashScope(ctx.sessionID)
        if (params.action === "list_trash") return { reads: [scope.root] }
        if (params.action === "restore") {
          const manifest = path.join(scope.root, `${params.trash_id ?? ""}.json`)
          const payload = path.join(scope.root, `${params.trash_id ?? ""}.data`)
          const destination = params.path
            ? requestedPath(params.path, ctx.sessionID)
            : path.parse(scope.workspace).root
          return {
            reads: [manifest, payload],
            writes: [manifest, payload, destination, scope.root],
          }
        }
        if (!params.path) return {}
        const source = requestedPath(params.path, ctx.sessionID)
        if (params.action === "move") {
          return {
            reads: [source],
            writes: [
              source,
              ...(params.destination ? [requestedPath(params.destination, ctx.sessionID)] : []),
            ],
          }
        }
        return { reads: [source], writes: [source, scope.root] }
      },
      execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const scope = trashScope(ctx.sessionID)
          const root = scope.root
          if (params.action === "list_trash") {
            const cursor = params.cursor ?? 0
            const limit = params.limit ?? 20
            const rows = yield* Effect.tryPromise(async () => {
              const names = await fs.readdir(root).catch((error: NodeJS.ErrnoException) => {
                if (error.code === "ENOENT") return []
                throw error
              })
              const manifests = await Promise.all(
                names
                  .filter((name) => /^[a-f0-9]{24}\.json$/.test(name))
                  .map(async (name) => {
                    try {
                      return JSON.parse(await fs.readFile(path.join(root, name), "utf8")) as Manifest
                    } catch {
                      return undefined
                    }
                  }),
              )
              return manifests
                .filter(
                  (manifest): manifest is Manifest =>
                    manifest !== undefined &&
                    manifest.owner === scope.owner &&
                    manifest.workspace === scope.workspace,
                )
                .toSorted((a, b) => b.deleted_at - a.deleted_at)
            })
            const items = rows.slice(cursor, cursor + limit)
            const next = cursor + items.length < rows.length ? cursor + items.length : undefined
            return answer(
              "Recoverable trash",
              {
                items,
                page: { cursor, next_cursor: next, has_more: next !== undefined, total: rows.length },
                owner: scope.owner,
                workspace: scope.workspace,
              },
              items.length === 0
                ? "Trash is empty."
                : items.map((item) => `[${item.id}] ${item.original_path} (${item.size} B)`).join("\n"),
            )
          }

          if (params.action === "restore") {
            if (!params.trash_id) throw new RecoverableError("manage_files: restore requires trash_id")
            const manifestPath = path.join(root, `${params.trash_id}.json`)
            const payloadPath = path.join(root, `${params.trash_id}.data`)
            const [manifestRaw, payload] = yield* Effect.tryPromise(() =>
              Promise.all([fs.readFile(manifestPath), fs.readFile(payloadPath)]),
            )
            const manifest = JSON.parse(manifestRaw.toString("utf8")) as Manifest
            if (
              manifest.id !== params.trash_id ||
              manifest.owner !== scope.owner ||
              manifest.workspace !== scope.workspace ||
              AppFileSystem.fingerprintBytes(payload) !== manifest.fingerprint
            ) {
              throw new RecoverableError("manage_files: trashed payload does not match its manifest")
            }
            if (manifest.expires_at < Date.now() / 1000) {
              throw new RecoverableError("manage_files: trashed file recovery window has expired")
            }
            const destination = (yield* assertWriteAllowed(
              ctx,
              requestedPath(params.path ?? manifest.original_path, ctx.sessionID),
            ))!
            if (yield* afs.existsSafe(destination)) {
              throw new RecoverableError(`manage_files: restore destination already exists: ${destination}`)
            }
            yield* Effect.promise(() =>
              assertProjectFilePolicy(ctx, [{ path: destination, content: payload }]),
            )
            yield* askEditUnlessMemory(ctx, destination, {
              patterns: [path.relative(Instance.worktree, destination)],
              diff: `restore ${manifest.original_path}`,
            })
            yield* afs
              .atomicBatch([
                { path: destination, content: payload, requireMissing: true, mode: manifest.mode },
                {
                  path: payloadPath,
                  content: null,
                  expectedFingerprint: AppFileSystem.fingerprintBytes(payload),
                },
                {
                  path: manifestPath,
                  content: null,
                  expectedFingerprint: AppFileSystem.fingerprintBytes(manifestRaw),
                },
              ])
              .pipe(
                Effect.catchIf(
                  (error) => error instanceof AppFileSystem.AtomicConflict,
                  (error) =>
                    Effect.fail(
                      new RecoverableError(`manage_files: conflict: ${error.message}. Read/list again and retry.`),
                    ),
                ),
              )
            return answer(
              path.relative(Instance.worktree, destination),
              {
                path: destination,
                fingerprint: manifest.fingerprint,
                restored: true,
                owner: scope.owner,
                workspace: scope.workspace,
              },
              `Restored ${destination}`,
            )
          }

          if (!params.path) throw new RecoverableError(`manage_files: ${params.action} requires path`)
          const source = (yield* assertWriteAllowed(ctx, requestedPath(params.path, ctx.sessionID)))!
          const info = yield* Effect.tryPromise(() => fs.lstat(source))
          if (!info.isFile() || info.isSymbolicLink()) {
            throw new RecoverableError("manage_files: source must be a regular file, not a directory or symbolic link")
          }
          const payload = yield* Effect.tryPromise(() => fs.readFile(source))
          const fingerprint = AppFileSystem.fingerprintBytes(payload)
          if (params.expected_fingerprint && params.expected_fingerprint !== fingerprint) {
            throw new RecoverableError(`manage_files: ${source} changed since the supplied fingerprint`)
          }

          if (params.action === "move") {
            if (!params.destination) throw new RecoverableError("manage_files: move requires destination")
            const destination = (yield* assertWriteAllowed(ctx, requestedPath(params.destination, ctx.sessionID)))!
            if (destination === source) throw new RecoverableError("manage_files: source and destination are the same file")
            if (yield* afs.existsSafe(destination)) {
              throw new RecoverableError(`manage_files: move destination already exists: ${destination}`)
            }
            yield* Effect.promise(() =>
              assertProjectFilePolicy(ctx, [
                { path: source, content: null },
                { path: destination, content: payload },
              ]),
            )
            yield* askEditUnlessMemory(ctx, source, {
              patterns: [path.relative(Instance.worktree, source), path.relative(Instance.worktree, destination)],
              diff: `move ${source} -> ${destination}`,
            })
            yield* afs
              .atomicBatch([
                { path: destination, content: payload, requireMissing: true, mode: info.mode },
                { path: source, content: null, expectedFingerprint: fingerprint },
              ])
              .pipe(
                Effect.catchIf(
                  (error) => error instanceof AppFileSystem.AtomicConflict,
                  (error) =>
                    Effect.fail(
                      new RecoverableError(`manage_files: conflict: ${error.message}. Read/list again and retry.`),
                    ),
                ),
              )
            return answer(
              path.relative(Instance.worktree, destination),
              { source, path: destination, fingerprint, owner: scope.owner, workspace: scope.workspace },
              `Moved ${source} to ${destination}`,
            )
          }

          const id = crypto.randomBytes(12).toString("hex")
          const payloadPath = path.join(root, `${id}.data`)
          const manifestPath = path.join(root, `${id}.json`)
          const manifest: Manifest = {
            id,
            original_path: source,
            deleted_at: Date.now() / 1000,
            expires_at: Date.now() / 1000 + 30 * 24 * 60 * 60,
            owner: scope.owner,
            workspace: scope.workspace,
            fingerprint,
            mode: info.mode,
            size: payload.byteLength,
          }
          const manifestRaw = Buffer.from(JSON.stringify(manifest, null, 2))
          yield* Effect.promise(() =>
            assertProjectFilePolicy(ctx, [{ path: source, content: null }]),
          )
          yield* askEditUnlessMemory(ctx, source, {
            patterns: [path.relative(Instance.worktree, source)],
            diff: `delete ${source} to recoverable trash ${id}`,
          })
          yield* afs
            .atomicBatch([
              { path: payloadPath, content: payload, requireMissing: true, mode: 0o600 },
              { path: manifestPath, content: manifestRaw, requireMissing: true, mode: 0o600 },
              { path: source, content: null, expectedFingerprint: fingerprint },
            ])
            .pipe(
              Effect.catchIf(
                (error) => error instanceof AppFileSystem.AtomicConflict,
                (error) =>
                  Effect.fail(
                    new RecoverableError(`manage_files: conflict: ${error.message}. Read/list again and retry.`),
                  ),
              ),
            )
          return answer(
            path.relative(Instance.worktree, source),
            {
              trash_id: id,
              original_path: source,
              fingerprint,
              recoverable: true,
              owner: scope.owner,
              workspace: scope.workspace,
            },
            `Moved ${source} to recoverable trash \`${id}\`.`,
          )
        }).pipe(Effect.orDie),
    }
  }),
)
