import { stageFormatted } from "./format-staging"
import { fileResult } from "./file-contract"
import z from "zod"
import { Effect } from "effect"
import * as Tool from "./tool"
import { replace } from "./edit"
import DESCRIPTION from "./multiedit.txt"
import path from "path"
import { Instance } from "../project/instance"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { SessionCwd } from "./session-cwd"
import { assertWriteAllowed, askEditUnlessMemory } from "./external-directory"
import { createTwoFilesPatch, diffLines } from "diff"
import { Format } from "../format"
import { Bus } from "../bus"
import { File } from "../file"
import { FileWatcher } from "../file/watcher"
import { Snapshot } from "@/snapshot"
import { RecoverableError } from "./recoverable"
import { assertProjectFilePolicy } from "./project-policy"
import { randomUUID } from "crypto"

const EditEntry = z.object({
  old_string: z.string().describe("The text to replace"),
  new_string: z.string().describe("The text to replace it with (must be different from old_string)"),
  replace_all: z.boolean().optional().describe("Replace all occurrences of old_string (default false)"),
})

const Parameters = z.object({
  file_path: z.string().describe("Path to the file to modify"),
  edits: z.array(EditEntry).describe("Array of edit operations to perform sequentially on the file"),
  expected_fingerprint: z.string().optional().describe("Fingerprint returned by read; rejects stale edits"),
})

export const MultiEditTool = Tool.define(
  "multiedit",
  Effect.gen(function* () {
    const afs = yield* AppFileSystem.Service
    const format = yield* Format.Service
    const bus = yield* Bus.Service

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      resources: (params: z.infer<typeof Parameters>, ctx: Tool.Context) => {
        const filepath = path.isAbsolute(params.file_path)
          ? params.file_path
          : path.join(SessionCwd.get(ctx.sessionID), params.file_path)
        return { reads: [filepath], writes: [filepath] }
      },
      execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const requested = path.isAbsolute(params.file_path)
            ? params.file_path
            : path.join(SessionCwd.get(ctx.sessionID), params.file_path)
          const filepath = (yield* assertWriteAllowed(ctx, requested))!
          const history = AppFileSystem.historyContextFromTool(ctx, filepath)
          const actionId = randomUUID()
          const proposed = params.edits.length
          const noMutation = `Proposed ${proposed} chunks. Applied 0 chunks.`
          const snapshot = yield* afs.readTextSnapshot(filepath)
          if (params.expected_fingerprint && snapshot.fingerprint !== params.expected_fingerprint) {
            throw new RecoverableError(`${noMutation} The file changed since it was read. Read it again and retry.`)
          }
          let content = snapshot.text
          for (const [index, entry] of params.edits.entries()) {
            try {
              content = replace(content, entry.old_string, entry.new_string, entry.replace_all)
            } catch (cause) {
              const reason = cause instanceof Error ? cause.message.slice(0, 4096) : "Replacement unavailable."
              throw new RecoverableError(`${noMutation} Chunk ${index + 1} failed during preparation. ${reason}`, { cause })
            }
          }
          let candidateContent: string | Uint8Array = AppFileSystem.encodeText(snapshot, content)
          let formatStatus: "skipped_policy" | "completed" | "failed" = "skipped_policy"
          let diff = createTwoFilesPatch(filepath, filepath, snapshot.text, content)
          const policy = yield* Effect.promise(() =>
            assertProjectFilePolicy(ctx, [{ path: filepath, content: candidateContent }]),
          )
          if (!policy.enforced) {
            const staged = yield* stageFormatted(afs, format, filepath, candidateContent, snapshot.mode)
            candidateContent = staged.content
            content = staged.text
            formatStatus = staged.status
            diff = createTwoFilesPatch(filepath, filepath, snapshot.text, content)
            yield* Effect.promise(() => assertProjectFilePolicy(ctx, [{ path: filepath, content: candidateContent }]))
          }
          yield* askEditUnlessMemory(ctx, filepath, {
            patterns: [path.relative(Instance.worktree, filepath)],
            diff,
          })
          const submittedFingerprint = AppFileSystem.fingerprintBytes(typeof candidateContent === "string" ? Buffer.from(candidateContent) : candidateContent)
          const observedFingerprint = yield* afs.atomicWrite({
            path: filepath,
            content: candidateContent,
            actionId,
            history,
            expectedFingerprint: snapshot.fingerprint,
            mode: snapshot.mode,
          }).pipe(
            Effect.catchIf(
              (error) => error instanceof AppFileSystem.AtomicConflict,
              (error) => {
                if (error.message.startsWith("history reconciliation pending after committed mutation")) {
                  return Effect.fail(new RecoverableError(`Proposed ${proposed} chunks. The file write committed, but Lore needs reconciliation. Check original action ${actionId} before retrying.`, { cause: error }))
                }
                const reason = error.message.startsWith("history prepare failed")
                  ? "Lore capture could not be prepared."
                  : "The file changed during commit. Read it again before retrying."
                return Effect.fail(new RecoverableError(`${noMutation} ${reason}`, { cause: error }))
              },
            ),
          )
          const finalSnapshot = yield* afs.readTextSnapshot(filepath)
          if (observedFingerprint !== submittedFingerprint || finalSnapshot.fingerprint !== submittedFingerprint) {
            throw new RecoverableError(`Proposed ${proposed} chunks. The atomic write completed, then the file changed. Check original action ${actionId} for the recorded version before retrying.`)
          }
          content = finalSnapshot.text
          diff = createTwoFilesPatch(filepath, filepath, snapshot.text, content)
          yield* bus.publish(File.Event.Edited, { file: filepath })
          yield* bus.publish(FileWatcher.Event.Updated, { file: filepath, event: "change" })

          const filediff: Snapshot.FileDiff = { file: filepath, patch: diff, additions: 0, deletions: 0 }
          for (const change of diffLines(snapshot.text, content)) {
            if (change.added) filediff.additions += change.count || 0
            if (change.removed) filediff.deletions += change.count || 0
          }
          return {
            title: path.relative(Instance.worktree, filepath),
            metadata: {
              diff,
              filediff,
              filepath,
              old_fingerprint: snapshot.fingerprint,
              fingerprint: finalSnapshot.fingerprint,
              action_id: actionId,
              format_status: formatStatus,
              edit_chunks: {
                mode: "atomic",
                proposed,
                applied: proposed,
                failed: 0,
                resulting_fingerprint: finalSnapshot.fingerprint,
                lore_status: history?.status.history_status ?? "unavailable",
              },
              history: history?.status ?? { status: "unconfigured", durable: false, coverage: "NoCapture" },
              file: fileResult({
                operation: "edit",
                path: filepath,
                kind: "text",
                range: finalSnapshot.size ? { unit: "byte", start: 0, end: finalSnapshot.size - 1 } : null,
                page: { unit: "byte", cursor: 0, next_cursor: null, has_more: false, returned: finalSnapshot.size, total: finalSnapshot.size },
                bytes_considered: finalSnapshot.size,
                lines_considered: finalSnapshot.text ? finalSnapshot.text.split(/\r\n|\n|\r/).length : 0,
                encoding: finalSnapshot.encoding,
                newline: finalSnapshot.newline === "\n" ? "lf" : finalSnapshot.newline === "\r\n" ? "crlf" : "cr",
                media_type: AppFileSystem.mimeType(filepath),
                fingerprint: finalSnapshot.fingerprint,
              }),
            },
            output: `Proposed ${proposed} chunks. Applied ${proposed} atomically. Failed 0. Resulting revision: ${finalSnapshot.fingerprint}. Lore: ${history?.status.history_status ?? "unavailable"} (action ${actionId}).`,
          }
        }).pipe(Effect.orDie),
    }
  }),
)
