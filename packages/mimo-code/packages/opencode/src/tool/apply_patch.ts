import z from "zod"
import * as path from "path"
import { Effect } from "effect"
import * as Tool from "./tool"
import { Bus } from "../bus"
import { FileWatcher } from "../file/watcher"
import { Instance } from "../project/instance"
import { SessionCwd } from "./session-cwd"
import { Patch } from "../patch"
import { createTwoFilesPatch, diffLines } from "diff"
import { assertWriteAllowed } from "./external-directory"
import { trimDiff } from "./edit"
import { LSP } from "../lsp"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import DESCRIPTION from "./apply_patch.txt"
import { File } from "../file"
import { Format } from "../format"
import { Global } from "../global"
import { RecoverableError } from "./recoverable"
import { assertProjectFilePolicy } from "./project-policy"
import { randomUUID } from "crypto"

const PatchParams = z.object({
  patch_text: z.string().describe("The full patch text that describes all changes to be made"),
  expected_fingerprints: z.record(z.string(), z.string()).optional().describe("Optional path-to-fingerprint CAS map"),
})

export const ApplyPatchTool = Tool.define(
  "apply_patch",
  Effect.gen(function* () {
    const lsp = yield* LSP.Service
    const afs = yield* AppFileSystem.Service
    const format = yield* Format.Service
    const bus = yield* Bus.Service

    const run = Effect.fn("ApplyPatchTool.execute")(function* (params: z.infer<typeof PatchParams>, ctx: Tool.Context) {
      if (!params.patch_text) {
        return yield* Effect.fail(new Error("patch_text is required"))
      }

      // Parse the patch to get hunks
      let hunks: Patch.Hunk[]
      try {
        const parseResult = Patch.parsePatch(params.patch_text)
        hunks = parseResult.hunks
      } catch (error) {
        return yield* Effect.fail(new Error(`apply_patch verification failed: ${error}`))
      }

      if (hunks.length === 0) {
        const normalized = params.patch_text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim()
        if (normalized === "*** Begin Patch\n*** End Patch") {
          return yield* Effect.fail(new Error("patch rejected: empty patch"))
        }
        return yield* Effect.fail(new Error("apply_patch verification failed: no hunks found"))
      }

      // Validate file paths and check permissions
      const fileChanges: Array<{
        filePath: string
        oldContent: string
        newContent: string
        type: "add" | "update" | "delete" | "move"
        movePath?: string
        diff: string
        additions: number
        deletions: number
        oldFingerprint?: string
        newFingerprint?: string
      }> = []
      const atomicChanges: AppFileSystem.AtomicChange[] = []
      const actionId = randomUUID()

      let totalDiff = ""

      for (const hunk of hunks) {
        const requestedPath = path.resolve(SessionCwd.get(ctx.sessionID), hunk.path)
        const filePath = (yield* assertWriteAllowed(ctx, requestedPath))!

        switch (hunk.type) {
          case "add": {
            if (yield* afs.existsSafe(filePath)) {
              return yield* Effect.fail(new RecoverableError(`apply_patch: add target already exists: ${filePath}`))
            }
            const oldContent = ""
            const newContent =
              hunk.contents.length === 0 || hunk.contents.endsWith("\n") ? hunk.contents : `${hunk.contents}\n`
            const diff = trimDiff(createTwoFilesPatch(filePath, filePath, oldContent, newContent))

            let additions = 0
            let deletions = 0
            for (const change of diffLines(oldContent, newContent)) {
              if (change.added) additions += change.count || 0
              if (change.removed) deletions += change.count || 0
            }

            fileChanges.push({
              filePath,
              oldContent,
              newContent,
              type: "add",
              diff,
              additions,
              deletions,
            })
            atomicChanges.push({
              path: filePath,
              content: newContent,
              requireMissing: true,
            })

            totalDiff += diff + "\n"
            break
          }

          case "update": {
            // Check if file exists for update
            const stats = yield* afs.stat(filePath).pipe(Effect.catch(() => Effect.succeed(undefined)))
            if (!stats || stats.type === "Directory") {
              return yield* Effect.fail(
                new Error(`apply_patch verification failed: Failed to read file to update: ${filePath}`),
              )
            }

            const snapshot = yield* afs.readTextSnapshot(filePath)
            const suppliedFingerprint = params.expected_fingerprints?.[hunk.path] ?? params.expected_fingerprints?.[filePath]
            if (suppliedFingerprint && snapshot.fingerprint !== suppliedFingerprint) {
              return yield* Effect.fail(
                new RecoverableError(`apply_patch: ${filePath} changed since the supplied fingerprint. Read it again and retry.`),
              )
            }
            const oldContent = snapshot.text
            let newContent = oldContent

            // Apply the update chunks to get new content
            try {
              const normalized = oldContent.replaceAll("\r\n", "\n").replaceAll("\r", "\n")
              const fileUpdate = Patch.deriveNewContentsFromChunks(filePath, hunk.chunks, normalized)
              newContent = AppFileSystem.preserveNewlines(fileUpdate.content, snapshot.newline)
            } catch (error) {
              return yield* Effect.fail(new Error(`apply_patch verification failed: ${error}`))
            }

            const diff = trimDiff(createTwoFilesPatch(filePath, filePath, oldContent, newContent))

            let additions = 0
            let deletions = 0
            for (const change of diffLines(oldContent, newContent)) {
              if (change.added) additions += change.count || 0
              if (change.removed) deletions += change.count || 0
            }

            const movePath = hunk.move_path
              ? (yield* assertWriteAllowed(
                  ctx,
                  path.resolve(SessionCwd.get(ctx.sessionID), hunk.move_path),
                ))!
              : undefined
            if (movePath && (yield* afs.existsSafe(movePath))) {
              return yield* Effect.fail(new RecoverableError(`apply_patch: move target already exists: ${movePath}`))
            }

            fileChanges.push({
              filePath,
              oldContent,
              newContent,
              type: hunk.move_path ? "move" : "update",
              movePath,
              diff,
              additions,
              deletions,
              oldFingerprint: snapshot.fingerprint,
            })
            if (movePath) {
              atomicChanges.push({
                path: movePath,
                content: AppFileSystem.encodeText(snapshot, newContent),
                requireMissing: true,
                mode: snapshot.mode,
              })
              atomicChanges.push({
                path: filePath,
                content: null,
                expectedFingerprint: snapshot.fingerprint,
                expectedMode: snapshot.mode,
                mode: snapshot.mode,
              })
            } else {
              atomicChanges.push({
                path: filePath,
                content: AppFileSystem.encodeText(snapshot, newContent),
                expectedFingerprint: snapshot.fingerprint,
                expectedMode: snapshot.mode,
                mode: snapshot.mode,
              })
            }

            totalDiff += diff + "\n"
            break
          }

          case "delete": {
            const snapshot = yield* afs.readTextSnapshot(filePath)
            const suppliedFingerprint = params.expected_fingerprints?.[hunk.path] ?? params.expected_fingerprints?.[filePath]
            if (suppliedFingerprint && snapshot.fingerprint !== suppliedFingerprint) {
              return yield* Effect.fail(
                new RecoverableError(`apply_patch: ${filePath} changed since the supplied fingerprint. Read it again and retry.`),
              )
            }
            const contentToDelete = snapshot.text
            const deleteDiff = trimDiff(createTwoFilesPatch(filePath, filePath, contentToDelete, ""))

            const deletions = contentToDelete.split("\n").length

            fileChanges.push({
              filePath,
              oldContent: contentToDelete,
              newContent: "",
              type: "delete",
              diff: deleteDiff,
              additions: 0,
              deletions,
              oldFingerprint: snapshot.fingerprint,
            })
            atomicChanges.push({
              path: filePath,
              content: null,
              expectedFingerprint: snapshot.fingerprint,
              expectedMode: snapshot.mode,
              mode: snapshot.mode,
            })

            totalDiff += deleteDiff + "\n"
            break
          }
        }
      }

      // Format temporary copies before asking permission or committing. The
      // permission prompt and returned audit metadata must describe the exact
      // bytes that the atomic batch will publish, not the pre-format patch.
      const history = atomicChanges.length
        ? AppFileSystem.historyContextFromTool(ctx, atomicChanges[0]!.path)
        : undefined
      const preparedChanges = yield* Effect.acquireUseRelease(
        afs.makeTempDirectory({ prefix: "open-clank-apply-patch-" }),
        (stageDir) =>
          Effect.gen(function* () {
            const prepared = atomicChanges.map((change) => ({ ...change, actionId, history }))
            for (const [fileIndex, change] of fileChanges.entries()) {
              if (change.type === "delete") continue
              const target = change.movePath ?? change.filePath
              const atomicIndex = prepared.findIndex((item) => item.path === target && item.content !== null)
              const atomic = prepared[atomicIndex]
              if (!atomic || atomic.content === null) {
                return yield* Effect.fail(new Error(`apply_patch: missing staged content for ${target}`))
              }
              const staged = path.join(stageDir, `${fileIndex}-${path.basename(target)}`)
              yield* afs.writeWithDirs(staged, atomic.content, atomic.mode)
              yield* format.file(staged)
              const finalSnapshot = yield* afs.readTextSnapshot(staged)
              prepared[atomicIndex] = {
                ...atomic,
                content: yield* afs.readFile(staged),
              }
              change.newContent = finalSnapshot.text
              change.diff = trimDiff(createTwoFilesPatch(target, target, change.oldContent, change.newContent))
              change.additions = 0
              change.deletions = 0
              for (const delta of diffLines(change.oldContent, change.newContent)) {
                if (delta.added) change.additions += delta.count || 0
                if (delta.removed) change.deletions += delta.count || 0
              }
            }
            return prepared
          }),
        (stageDir) => afs.remove(stageDir, { recursive: true, force: true }).pipe(Effect.ignore),
      )
      totalDiff = fileChanges.map((change) => change.diff).join("\n") + "\n"
      yield* Effect.promise(() =>
        assertProjectFilePolicy(
          ctx,
          preparedChanges.map((change) => ({
            path: change.path,
            content: change.content,
          })),
        ),
      )

      // Build per-file metadata for UI rendering (used for both permission and result)
      const files = fileChanges.map((change) => ({
        filePath: change.filePath,
        relativePath: path.relative(Instance.worktree, change.movePath ?? change.filePath).replaceAll("\\", "/"),
        type: change.type,
        patch: change.diff,
        additions: change.additions,
        deletions: change.deletions,
        movePath: change.movePath,
        old_fingerprint: change.oldFingerprint,
        fingerprint: change.newFingerprint,
      }))
      const title = files.length === 1 ? files[0]!.relativePath : `${files.length} files`

      // Check permissions if needed
      const permissionChanges = fileChanges.filter(
        (change) => !AppFileSystem.contains(path.join(Global.Path.data, "memory"), change.movePath ?? change.filePath),
      )
      // NOTE: permissionChanges already excludes memory-tree paths (filtered at
      // the `permissionChanges` definition above), so this ask never fires for
      // memory writes — the askEditUnlessMemory deferral used by write.ts/edit.ts
      // is structurally already satisfied here. Left as a direct ctx.ask.
      if (permissionChanges.length > 0) {
        const relativePaths = [
          ...new Set(
            permissionChanges.flatMap((change) =>
              [change.filePath, change.movePath]
                .filter((target): target is string => target !== undefined)
                .map((target) => path.relative(Instance.worktree, target).replaceAll("\\", "/")),
            ),
          ),
        ]
        yield* ctx.ask({
          permission: "edit",
          patterns: relativePaths,
          always: ["*"],
          metadata: {
            filepath: relativePaths.join(", "),
            diff: permissionChanges.map((change) => change.diff).join("\n") + "\n",
            files: files.filter(
              (file) => !AppFileSystem.contains(path.join(Global.Path.data, "memory"), file.movePath ?? file.filePath),
            ),
          },
        })
      }

      // Apply the changes as one staged transaction. The shared filesystem
      // restores every prior target if any rename fails.
      const updates: Array<{ file: string; event: "add" | "change" | "unlink" }> = []
      yield* afs.atomicBatch(preparedChanges).pipe(
        Effect.catchIf(
          (error) => error instanceof AppFileSystem.AtomicConflict,
          () => Effect.fail(new RecoverableError("apply_patch: a target changed during commit. Read the files and retry.")),
        ),
      )

      for (const change of fileChanges) {
        const edited = change.type === "delete" ? undefined : (change.movePath ?? change.filePath)
        switch (change.type) {
          case "add":
            updates.push({ file: change.filePath, event: "add" })
            break

          case "update":
            updates.push({ file: change.filePath, event: "change" })
            break

          case "move":
            if (change.movePath) {
              updates.push({ file: change.filePath, event: "unlink" })
              updates.push({ file: change.movePath, event: "add" })
            }
            break

          case "delete":
            updates.push({ file: change.filePath, event: "unlink" })
            break
        }

        if (edited) {
          change.newFingerprint = (yield* afs.readTextSnapshot(edited)).fingerprint
          yield* bus.publish(File.Event.Edited, { file: edited })
        }
      }
      for (const [index, change] of fileChanges.entries()) {
        files[index]!.fingerprint = change.newFingerprint
      }

      // Publish file change events
      for (const update of updates) {
        yield* bus.publish(FileWatcher.Event.Updated, update)
      }

      // Notify LSP of file changes and collect diagnostics
      for (const change of fileChanges) {
        if (change.type === "delete") continue
        const target = change.movePath ?? change.filePath
        yield* lsp.touchFile(target, true)
      }
      const diagnostics = yield* lsp.diagnostics()

      // Generate output summary
      const summaryLines = fileChanges.map((change) => {
        if (change.type === "add") {
          return `A ${path.relative(Instance.worktree, change.filePath).replaceAll("\\", "/")}`
        }
        if (change.type === "delete") {
          return `D ${path.relative(Instance.worktree, change.filePath).replaceAll("\\", "/")}`
        }
        const target = change.movePath ?? change.filePath
        return `M ${path.relative(Instance.worktree, target).replaceAll("\\", "/")}`
      })
      let output = `Success. Updated the following files:\n${summaryLines.join("\n")}`

      for (const change of fileChanges) {
        if (change.type === "delete") continue
        const target = change.movePath ?? change.filePath
        const block = LSP.Diagnostic.report(target, diagnostics[AppFileSystem.normalizePath(target)] ?? [])
        if (!block) continue
        const rel = path.relative(Instance.worktree, target).replaceAll("\\", "/")
        output += `\n\nLSP errors detected in ${rel}, please fix:\n${block}`
      }

      return {
        title,
        metadata: {
          diff: totalDiff,
          files,
          diagnostics,
          action_id: actionId,
          history: history?.status ?? { status: "unconfigured", durable: false, coverage: "NoCapture" },
        },
        output,
      }
    })

    return {
      description: DESCRIPTION,
      parameters: PatchParams,
      resources: (params: z.infer<typeof PatchParams>, ctx: Tool.Context) => {
        try {
          const reads: string[] = []
          const writes: string[] = []
          for (const hunk of Patch.parsePatch(params.patch_text).hunks) {
            const source = path.resolve(SessionCwd.get(ctx.sessionID), hunk.path)
            writes.push(source)
            if (hunk.type !== "add") reads.push(source)
            if (hunk.type === "update" && hunk.move_path) {
              writes.push(path.resolve(SessionCwd.get(ctx.sessionID), hunk.move_path))
            }
          }
          return { reads, writes }
        } catch {
          return {}
        }
      },
      execute: (params: z.infer<typeof PatchParams>, ctx: Tool.Context) => run(params, ctx).pipe(Effect.orDie),
    }
  }),
)
