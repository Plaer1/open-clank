import z from "zod"
import { Effect } from "effect"
import * as Tool from "./tool"
import { replace, trimDiff } from "./edit"
import DESCRIPTION from "./multiedit.txt"
import path from "path"
import { Instance } from "../project/instance"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { SessionCwd } from "./session-cwd"
import { assertWriteAllowed, askEditUnlessMemory } from "./external-directory"
import { assertFileRead } from "./read-state"
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
  file_path: z.string().describe("The absolute path to the file to modify"),
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
          const readFingerprint = assertFileRead(ctx, filepath, "multiedit")
          const snapshot = yield* afs.readTextSnapshot(filepath)
          if (
            (readFingerprint && snapshot.fingerprint !== readFingerprint) ||
            (params.expected_fingerprint && snapshot.fingerprint !== params.expected_fingerprint)
          ) {
            throw new RecoverableError(`multiedit: ${filepath} changed since it was read. Read it again and retry.`)
          }
          let content = snapshot.text
          for (const entry of params.edits) {
            content = replace(content, entry.old_string, entry.new_string, entry.replace_all)
          }
          const candidateContent = AppFileSystem.encodeText(snapshot, content)
          let diff = trimDiff(createTwoFilesPatch(filepath, filepath, snapshot.text, content))
          const policy = yield* Effect.promise(() =>
            assertProjectFilePolicy(ctx, [{ path: filepath, content: candidateContent }]),
          )
          yield* askEditUnlessMemory(ctx, filepath, {
            patterns: [path.relative(Instance.worktree, filepath)],
            diff,
          })
          yield* afs.atomicWrite({
            path: filepath,
            content: candidateContent,
            actionId,
            history,
            expectedFingerprint: snapshot.fingerprint,
            mode: snapshot.mode,
          }).pipe(
            Effect.catchIf(
              (error) => error instanceof AppFileSystem.AtomicConflict,
              () => Effect.fail(new RecoverableError(`multiedit: ${filepath} changed during commit. Read it again and retry.`)),
            ),
          )
          if (!policy.enforced) {
            yield* format.file(filepath).pipe(Effect.catch(() => Effect.void))
          }
          const finalSnapshot = yield* afs.readTextSnapshot(filepath)
          content = finalSnapshot.text
          diff = trimDiff(createTwoFilesPatch(filepath, filepath, snapshot.text, content))
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
              history: history?.status ?? { status: "unconfigured", durable: false, coverage: "NoCapture" },
            },
            output: `Applied ${params.edits.length} edits atomically.`,
          }
        }).pipe(Effect.orDie),
    }
  }),
)
