import z from "zod"
import * as path from "path"
import { Effect } from "effect"
import * as Tool from "./tool"
import { LSP } from "../lsp"
import { createTwoFilesPatch } from "diff"
import DESCRIPTION from "./write.txt"
import { Bus } from "../bus"
import { File } from "../file"
import { FileWatcher } from "../file/watcher"
import { Format } from "../format"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Instance } from "../project/instance"
import { SessionCwd } from "./session-cwd"
import { trimDiff } from "./edit"
import { assertWriteAllowed, askEditUnlessMemory } from "./external-directory"
import { RecoverableError } from "./recoverable"
import { fileResult } from "./file-contract"
import { assertProjectFilePolicy } from "./project-policy"
import { randomUUID } from "crypto"

const MAX_PROJECT_DIAGNOSTICS_FILES = 5

export const WriteTool = Tool.define(
  "write",
  Effect.gen(function* () {
    const lsp = yield* LSP.Service
    const fs = yield* AppFileSystem.Service
    const bus = yield* Bus.Service
    const format = yield* Format.Service

    return {
      description: DESCRIPTION,
      parameters: z.object({
        content: z.string().describe("The content to write to the file"),
        file_path: z.string().describe("The absolute path to the file to write (must be absolute, not relative)"),
        expected_fingerprint: z.string().optional().describe("Fingerprint returned by read; rejects stale overwrites"),
      }),
      resources: (params: { file_path: string }, ctx: Tool.Context) => {
        const filepath = path.isAbsolute(params.file_path)
          ? params.file_path
          : path.join(SessionCwd.get(ctx.sessionID), params.file_path)
        return { reads: [filepath], writes: [filepath] }
      },
      execute: (params: { content: string; file_path: string; expected_fingerprint?: string }, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const requested = path.isAbsolute(params.file_path)
            ? params.file_path
            : path.join(SessionCwd.get(ctx.sessionID), params.file_path)
          const filepath = (yield* assertWriteAllowed(ctx, requested))!
          const history = AppFileSystem.historyContextFromTool(ctx, filepath)

          const exists = yield* fs.existsSafe(filepath)
          const snapshot = exists
            ? yield* fs.readTextSnapshot(filepath).pipe(
                Effect.catch(() =>
                  Effect.fail(new RecoverableError(`write: ${filepath} is not a supported text file`)),
                ),
              )
            : undefined
          const observedFingerprint = snapshot?.fingerprint
          if (
            (exists && params.expected_fingerprint && params.expected_fingerprint !== observedFingerprint) ||
            (!exists && params.expected_fingerprint && params.expected_fingerprint !== "missing")
          ) {
            throw new RecoverableError(`write: ${filepath} changed since it was read. Read it again and retry.`)
          }
          const contentOld = snapshot?.text ?? ""
          const rendered = snapshot
            ? AppFileSystem.preserveNewlines(params.content, snapshot.newline)
            : params.content
          const candidateContent = snapshot
            ? AppFileSystem.encodeText(snapshot, rendered)
            : rendered
          const actionId = randomUUID()

          const diff = trimDiff(createTwoFilesPatch(filepath, filepath, contentOld, rendered))
          const policy = yield* Effect.promise(() =>
            assertProjectFilePolicy(ctx, [{ path: filepath, content: candidateContent }]),
          )
          yield* askEditUnlessMemory(ctx, filepath, {
            patterns: [path.relative(Instance.worktree, filepath)],
            diff,
          })

          yield* fs.atomicWrite({
            path: filepath,
            content: candidateContent,
            actionId,
            history,
            expectedFingerprint: observedFingerprint,
            requireMissing: !exists,
            mode: snapshot?.mode,
          }).pipe(
            Effect.catchIf(
              (error) => error instanceof AppFileSystem.AtomicConflict,
              () => Effect.fail(new RecoverableError(`write: ${filepath} changed during the write. Read it again and retry.`)),
            ),
          )
          if (!policy.enforced) {
            yield* format.file(filepath).pipe(Effect.catch(() => Effect.void))
          }
          const finalBytes = yield* fs.readFile(filepath)
          const finalFingerprint = AppFileSystem.fingerprintBytes(finalBytes)
          const newline = snapshot?.newline ?? (rendered.includes("\r\n") ? "\r\n" : rendered.includes("\r") ? "\r" : "\n")
          yield* bus.publish(File.Event.Edited, { file: filepath })
          yield* bus.publish(FileWatcher.Event.Updated, {
            file: filepath,
            event: exists ? "change" : "add",
          })

          let output = "Wrote file successfully."
          yield* lsp.touchFile(filepath, true)
          const diagnostics = yield* lsp.diagnostics()
          const normalizedFilepath = AppFileSystem.normalizePath(filepath)
          let projectDiagnosticsCount = 0
          for (const [file, issues] of Object.entries(diagnostics)) {
            const current = file === normalizedFilepath
            if (!current && projectDiagnosticsCount >= MAX_PROJECT_DIAGNOSTICS_FILES) continue
            const block = LSP.Diagnostic.report(current ? filepath : file, issues)
            if (!block) continue
            if (current) {
              output += `\n\nLSP errors detected in this file, please fix:\n${block}`
              continue
            }
            projectDiagnosticsCount++
            output += `\n\nLSP errors detected in other files:\n${block}`
          }

          return {
            title: path.relative(Instance.worktree, filepath),
            metadata: {
              diagnostics,
              diff,
              filepath,
              exists: exists,
              old_fingerprint: observedFingerprint,
              fingerprint: finalFingerprint,
              action_id: actionId,
              history: history?.status ?? { status: "unconfigured", durable: false, coverage: "NoCapture" },
              file: fileResult({
                operation: "write",
                path: filepath,
                kind: "text",
                range: finalBytes.byteLength
                  ? { unit: "byte", start: 0, end: finalBytes.byteLength - 1 }
                  : null,
                page: {
                  unit: "byte",
                  cursor: 0,
                  next_cursor: null,
                  has_more: false,
                  returned: finalBytes.byteLength,
                  total: finalBytes.byteLength,
                },
                bytes_considered: finalBytes.byteLength,
                lines_considered: rendered ? rendered.split(/\r\n|\n|\r/).length : 0,
                encoding: snapshot?.encoding ?? "utf-8",
                newline: newline === "\n" ? "lf" : newline === "\r\n" ? "crlf" : "cr",
                media_type: AppFileSystem.mimeType(filepath),
                fingerprint: finalFingerprint,
              }),
            },
            output,
          }
        }).pipe(Effect.orDie),
    }
  }),
)
