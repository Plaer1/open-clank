import path from "path"
import { randomUUID } from "node:crypto"
import z from "zod"
import { Effect } from "effect"
import { InstanceState } from "@/effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Bus } from "@/bus"
import { ManagedProvider } from "@/acp/managed-provider"
import { assertExternalDirectoryEffect } from "./external-directory"
import { SessionCwd } from "./session-cwd"
import {
  beginManagedSessionTransition,
  clearManagedSessionTransition,
  managedSessionBinding,
  markManagedSessionReconciling,
  replaceManagedSessionBinding,
} from "@/memory/session-scope"
import * as Tool from "./tool"

const DESCRIPTION = [
  "Switch the working directory for the current session (like cd in a terminal).",
  "",
  "Use this when the user asks to switch, change, or cd into a directory,",
  "or when you need to work extensively within a subdirectory (e.g., a monorepo package).",
  "",
  "After calling this tool, all subsequent file operations (read, edit, write, glob, grep, bash)",
  "will resolve relative paths from the new directory. Subagents inherit the changed directory.",
  "",
  "Pass an absolute path, or a relative path (resolved from the current working directory).",
  'Pass "~" to reset back to the project root.',
].join("\n")

async function requestManagedCwd(sessionID: string, cwd: string) {
  const binding = managedSessionBinding(sessionID)
  if (!binding || binding.transition) throw new Error("managed session binding is unavailable")
  const transitionID = randomUUID()
  let settledFromAuthority = false
  beginManagedSessionTransition(sessionID, cwd, transitionID, binding.bindingRevision)
  let result: Awaited<ReturnType<typeof ManagedProvider.requestSessionCwdChange>>
  try {
    result = await ManagedProvider.requestSessionCwdChange(sessionID, cwd, binding.bindingRevision, transitionID)
  } catch (error) {
    try {
      const recovered = await ManagedProvider.readManagedSessionBinding(sessionID)
      if (recovered.engineSessionID !== binding.engineSessionID || recovered.stableChatID !== binding.stableChatID || recovered.owner !== binding.owner || recovered.mapRevision < binding.mapRevision || recovered.mappingRevision !== binding.mappingRevision || recovered.workspaceRevision < binding.bindingRevision) throw error
      replaceManagedSessionBinding(sessionID, {
        ...binding,
        engineSessionID: recovered.engineSessionID,
        engineAliases: recovered.engineAliases,
        memoryWorkspaceID: recovered.memoryWorkspaceID,
        authorityWorkspaceID: recovered.authorityWorkspaceID,
        copalWorkspace: recovered.copalWorkspace,
        physicalCwd: recovered.canonicalCwd,
        bindingRevision: recovered.workspaceRevision,
        mapRevision: recovered.mapRevision,
        mappingRevision: recovered.mappingRevision,
        memoryEnabled: recovered.memoryEnabled,
        transition: null,
      }, { expectedTransitionID: transitionID, expectedWorkspaceRevision: binding.bindingRevision })
      settledFromAuthority = true
      if (recovered.canonicalCwd !== cwd) throw error
      return {
        outcome: "accepted" as const,
        canonicalCwd: recovered.canonicalCwd,
        workspaceRevision: recovered.workspaceRevision,
        changed: recovered.canonicalCwd !== binding.physicalCwd,
        transitionID,
      }
    } catch {
      if (settledFromAuthority) throw error
      markManagedSessionReconciling(sessionID, transitionID, binding.bindingRevision)
      throw error
    }
  }
  if (result.outcome === "rejected") {
    clearManagedSessionTransition(sessionID, transitionID, binding.bindingRevision)
    throw new Error(`workspace change rejected: ${result.code}`)
  }
  const expectedRevision = binding.bindingRevision + (result.changed ? 1 : 0)
  if (result.transitionID !== transitionID || !path.isAbsolute(result.canonicalCwd) || path.normalize(result.canonicalCwd) !== result.canonicalCwd || result.workspaceRevision !== expectedRevision || result.changed !== (result.canonicalCwd !== binding.physicalCwd)) {
    markManagedSessionReconciling(sessionID, transitionID, binding.bindingRevision)
    throw new Error("managed session cwd acknowledgement is invalid")
  }
      replaceManagedSessionBinding(sessionID, {
        ...binding,
        physicalCwd: result.canonicalCwd,
        bindingRevision: result.workspaceRevision,
        transition: null,
      }, { expectedTransitionID: transitionID, expectedWorkspaceRevision: binding.bindingRevision })
  return result
}

// Kept as a narrow seam for the managed cwd reconciliation proof. The tool
// remains the only production caller; tests use it to exercise ambiguous host
// responses without constructing the full tool layer.
export const requestManagedCwdForTest = requestManagedCwd

export const ChangeDirectoryTool = Tool.define(
  "change_directory",
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service
    const bus = yield* Bus.Service

    return {
      description: DESCRIPTION,
      parameters: z.object({
        path: z
          .string()
          .describe(
            "The directory to switch to. Absolute or relative to current working directory. Use '~' to reset to project root.",
          ),
      }),
      resources: (params: { path: string }, ctx: Tool.Context) => ({
        reads:
          params.path === "~" || params.path === ""
            ? []
            : [
                path.isAbsolute(params.path)
                  ? params.path
                  : path.resolve(SessionCwd.get(ctx.sessionID), params.path),
              ],
      }),
      execute: (params: { path: string }, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const ins = yield* InstanceState.context
          const currentCwd = SessionCwd.get(ctx.sessionID)

          if (params.path === "~" || params.path === "") {
            const approved = ManagedProvider.enabled()
              ? yield* Effect.tryPromise(() => requestManagedCwd(ctx.sessionID, ins.directory)).pipe(Effect.orDie)
              : undefined
            const nextCwd = approved?.canonicalCwd ?? ins.directory
            if (nextCwd === ins.directory) SessionCwd.clear(ctx.sessionID)
            else SessionCwd.set(ctx.sessionID, nextCwd)
            yield* bus.publish(SessionCwd.Event.Changed, {
              sessionID: ctx.sessionID,
              cwd: nextCwd,
            })
            return {
              title: "reset",
              metadata: { from: currentCwd, to: nextCwd },
              output: `Working directory reset to project root: ${nextCwd}`,
            }
          }

          const resolved = path.isAbsolute(params.path)
            ? params.path
            : path.resolve(currentCwd, params.path)

          const normalized = path.normalize(resolved)

          const stat = yield* fs.stat(normalized).pipe(
            Effect.catch(() => Effect.succeed(undefined)),
          )

          if (!stat) {
            throw new Error(`Directory does not exist: ${normalized}`)
          }

          if (stat.type !== "Directory") {
            throw new Error(`Path is not a directory: ${normalized}`)
          }

          yield* assertExternalDirectoryEffect(ctx, normalized, { kind: "directory" })

          const approved = ManagedProvider.enabled()
              ? yield* Effect.tryPromise(() => requestManagedCwd(ctx.sessionID, normalized)).pipe(Effect.orDie)
            : undefined
          const nextCwd = approved?.canonicalCwd ?? normalized
          SessionCwd.set(ctx.sessionID, nextCwd)
          yield* bus.publish(SessionCwd.Event.Changed, {
            sessionID: ctx.sessionID,
            cwd: nextCwd,
          })

          return {
            title: path.relative(ins.worktree, nextCwd) || ".",
            metadata: { from: currentCwd, to: nextCwd },
            output: `Working directory changed: ${currentCwd} → ${nextCwd}`,
          }
        }),
    }
  }),
)
