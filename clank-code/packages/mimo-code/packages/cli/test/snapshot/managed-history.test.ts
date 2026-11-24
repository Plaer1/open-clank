import { test, expect } from "bun:test"
import { spawn } from "node:child_process"
import * as fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

test("direct edit stores managed origin and reads exact action without treating it as a snapshot", async () => {
  const binary = process.env.P05_HISTORY_BIN
  if (!binary || !process.env.XDG_DATA_HOME || !process.env.XDG_CONFIG_HOME || !process.env.XDG_CACHE_HOME || !process.env.XDG_STATE_HOME) throw new Error("isolated fixture environment is required")
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "p05-history-"))
  const socket = path.join(root, "history.sock")
  const service = spawn(binary, [socket, path.join(root, "catalog"), path.join(root, "lore"), "fixture-account", root], {
    env: { ...process.env, OPENCLANK_HISTORY_TOKEN: "fixture-token" }, stdio: ["ignore", "ignore", "pipe"],
  })
  let errors = ""
  service.stderr.on("data", (chunk) => { if (errors.length < 16_384) errors += String(chunk).slice(0, 16_384 - errors.length) })
  try {
    for (let attempt = 0; attempt < 300; attempt++) {
      if (service.exitCode !== null || service.signalCode !== null) throw new Error("fixture History service exited: " + errors)
      if (await fs.stat(socket).then(() => true, () => false)) break
      if (attempt === 299) throw new Error("fixture History service did not start")
      await new Promise((resolve) => setTimeout(resolve, 20))
    }
    const [{ Effect, Layer }, { NodeFileSystem }, { AppFileSystem }, { readManagedAction, assertGitSnapshotRef }] = await Promise.all([
      import("effect"), import("@effect/platform-node"), import("@mimo-ai/shared/filesystem"), import("../../src/snapshot/managed-history"),
    ])
    const target = path.join(root, "example.txt")
    await fs.writeFile(target, "before")
    const origin = { version: 1 as const, host_chat_id: "chat", engine_session_id: "engine", root_operation_id: "root-op", actor_id: "main", execution_revision: 1, inner_turn_id: "turn", tool_call_id: "call", assistant_message_id: "message" }
    const history: any = {
      actorId: "fixture-actor", accountId: "fixture-account", workspaceId: "fixture-workspace", workspaceRoot: root,
      socketPath: socket, token: "fixture-token", managedToolOrigin: origin,
      status: { status: "paused", history_status: "paused", capture_phase: "unavailable", durable: false, coverage: "NoCapture" },
    }
    const layer = AppFileSystem.layer.pipe(Layer.provide(NodeFileSystem.layer))
    await Effect.runPromise(Effect.gen(function* () {
      const files = yield* AppFileSystem.Service
      yield* files.atomicWrite({ path: target, content: "after", actionId: "fixture-action", history })
    }).pipe(Effect.provide(layer)))
    expect(history.status.status).toBe("complete")
    const ref = history.status.managed_action_ref
    expect(typeof ref).toBe("string")
    await fs.writeFile(target, "later current content")
    const result = await readManagedAction(ref, history, () => true)
    expect(result.before.state).toBe("text")
    expect(result.after.state).toBe("text")
    if (result.before.state !== "text" || result.after.state !== "text") throw new Error("exact payload unavailable")
    expect(Buffer.from(result.before.bytes).toString()).toBe("before")
    expect(Buffer.from(result.after.bytes).toString()).toBe("after")
    await expect(readManagedAction(ref, history, () => false)).rejects.toThrow()
    expect(() => assertGitSnapshotRef(ref)).toThrow()
    expect(() => assertGitSnapshotRef("legacy-unprefixed-git-ref")).not.toThrow()
  } finally {
    if (service.exitCode === null && service.signalCode === null) service.kill("SIGTERM")
    await new Promise<void>((resolve) => {
      if (service.exitCode !== null || service.signalCode !== null) return resolve()
      const timer = setTimeout(() => service.kill("SIGKILL"), 1000)
      service.once("exit", () => { clearTimeout(timer); resolve() })
    })
    await fs.rm(root, { recursive: true, force: true })
  }
}, 30000)
