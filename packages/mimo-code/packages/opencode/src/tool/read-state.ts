import path from "path"
import type * as Tool from "./tool"
import { SessionCwd } from "./session-cwd"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { RecoverableError } from "./recoverable"
import type { SessionID } from "../session/schema"

// Same normalization both sides of the comparison go through so a Read on
// a relative path lines up with an Edit on the absolute one.
function canon(sessionID: SessionID, p: string): string {
  const abs = path.isAbsolute(p) ? p : path.resolve(SessionCwd.get(sessionID), p)
  const canonical = AppFileSystem.resolve(abs)
  if (process.platform === "win32") return AppFileSystem.normalizePath(canonical).toLowerCase()
  return canonical
}

/**
 * Throws RecoverableError if the given file was not previously read by the
 * `read` tool in this conversation. Writes/edits to existing files must be
 * preceded by a Read so the model sees the current contents — this turns the
 * usage note in edit.txt into actual enforcement.
 *
 * RecoverableError is intentional: the failure is surfaced to the agent as a
 * tool result it can act on (call Read, then retry) rather than as a hard
 * system fault.
 */
export function assertFileRead(ctx: Tool.Context, targetPath: string, toolId: string): string | undefined {
  const target = canon(ctx.sessionID, targetPath)

  for (const msg of ctx.messages) {
    for (const part of msg.parts) {
      if (part.type !== "tool") continue
      if (part.tool !== "read") continue
      if (part.state.status !== "completed") continue
      const input = part.state.input as { file_path?: unknown } | undefined
      const fp = input?.file_path
      if (typeof fp !== "string") continue
      if (canon(ctx.sessionID, fp) !== target) continue
      const fingerprint = part.state.metadata?.fingerprint
      if (typeof fingerprint === "string") return fingerprint
      // Sessions created before fingerprinted reads shipped still contain a
      // valid completed Read. Let the writer take a fresh snapshot and use its
      // commit-time CAS; new reads always take the stronger branch above.
      return undefined
    }
  }

  throw new RecoverableError(
    `${toolId}: ${targetPath} has not been read with a current fingerprint. Call the read tool on this file, then retry.`,
  )
}
