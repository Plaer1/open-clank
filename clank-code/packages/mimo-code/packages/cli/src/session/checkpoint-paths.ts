import path from "path"
import fs from "fs/promises"
import { Global } from "@/global"
import type { ProjectID } from "@/project/schema"
import { SessionID } from "./schema"

// ---------------------------------------------------------------------------
// File helpers
// ---------------------------------------------------------------------------

/**
 * Session memory root. Houses checkpoint artifacts, task narratives, and
 * other per-session memory files under `<data>/memory/sessions/<sid>/`.
 */
export function metaDir(sessionID: SessionID): string {
  return path.join(Global.Path.data, "memory", "sessions", sessionID)
}

/**
 * v5 single-file checkpoint at `<sid>/checkpoint.md` (no subdir).
 */
export function checkpointPath(sessionID: SessionID): string {
  return path.join(metaDir(sessionID), "checkpoint.md")
}

/**
 * v5 per-project memory file at `<data>/memory/projects/<pid>/MEMORY.md`.
 */
export function memoryPath(projectID: ProjectID): string {
  return path.join(Global.Path.data, "memory", "projects", projectID, "MEMORY.md")
}

/**
 * Single global memory file at `<data>/memory/global/MEMORY.md`. User-level
 * cross-project preferences. Read-only from the agent side; no auto-create.
 */
export function globalMemoryPath(): string {
  return path.join(Global.Path.data, "memory", "global", "MEMORY.md")
}

/** Reject legacy project memory before any current-path read/write. */
export async function assertCurrentProjectMemory(projectID: ProjectID): Promise<void> {
  const dir = path.dirname(memoryPath(projectID))
  const names = await fs.readdir(dir).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return [] as string[]
    throw error
  })
  if (names.includes("memory.md")) throw new Error(`Legacy project memory ${dir}; run .clanker/tools/native/mimo memory with that explicit directory`)
}

/**
 * v8 session-scoped notes file at `<sid>/notes.md`. Main-agent-only
 * scratchpad; writer reconciles entries at checkpoint events.
 */
export function notesPath(sessionID: SessionID): string {
  return path.join(metaDir(sessionID), "notes.md")
}

/**
 * Per-session tasks directory at `<sid>/tasks/`. Houses per-task progress
 * journals authored either by subagents (Spec ②) or by the splitover
 * plugin (when main checkpoint.md grows past caps).
 */
export function tasksDir(sessionID: SessionID): string {
  return path.join(metaDir(sessionID), "tasks")
}

/**
 * Per-task progress journal at `<sid>/tasks/<TID>/progress.md`. Authored
 * by subagents (Spec ② actor.postStop) and read by the checkpoint writer's
 * reconcile preprocessor (Spec ② Chain 2).
 */
export function progressPath(sessionID: SessionID, taskID: string): string {
  return path.join(tasksDir(sessionID), taskID, "progress.md")
}
