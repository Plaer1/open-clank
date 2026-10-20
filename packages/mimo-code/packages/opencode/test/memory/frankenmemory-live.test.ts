import { afterEach, expect, test } from "bun:test"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { Effect, Layer } from "effect"
import { spawnSync } from "node:child_process"
import * as fs from "fs/promises"
import path from "path"
import { Agent } from "../../src/agent/agent"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { registerManagedMcpClient, unregisterManagedMcpClient } from "../../src/memory/mcp-client"
import { registerMemorySessionScope, unregisterMemorySessionScope } from "../../src/memory/session-scope"
import { Memory } from "../../src/memory"
import { Instance } from "../../src/project/instance"
import { MessageID, SessionID } from "../../src/session/schema"
import { Truncate } from "../../src/tool"
import { MemoryTool } from "../../src/tool/memory"
import { tmpdir } from "../fixture/fixture"

const repo = path.resolve(import.meta.dir, "../../../../../..")
const bootstrap = path.join(repo, "scripts/openclank_bootstrap.py")

function resolveRuntime() {
  const candidates = [
    process.env.OPEN_CLANK_RUNTIME_RESOLVER_PYTHON,
    process.env.PYTHON,
    process.platform === "win32" ? "python" : "python3",
    process.platform === "win32" ? "py" : "python",
    path.join(repo, "venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python"),
    path.join(repo, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python"),
  ].filter((value): value is string => Boolean(value))
  const diagnostics: string[] = []
  for (const candidate of candidates) {
    const result = spawnSync(candidate, [bootstrap, "runtime", "--repo-root", repo], {
      cwd: repo,
      encoding: "utf8",
      timeout: 15_000,
    })
    const output = `${result.stdout || ""}`.trim()
    if (result.status === 0) {
      try {
        const report = JSON.parse(output.split("\n").at(-1) || "")
        if (report.ok) return report
      } catch (error) {
        diagnostics.push(`${candidate}: invalid resolver output`)
        continue
      }
    }
    diagnostics.push(`${candidate}: ${result.error?.message || output.slice(-240) || `exit ${result.status}`}`)
  }
  throw new Error(`runtime resolver failed before MCP startup:\n${diagnostics.join("\n")}`)
}

const runtime = resolveRuntime()
const python = runtime.python.path
const lifetools = runtime.lifetools.path
const fm = runtime.fm_mcp.path
const fmDir = path.join(repo, "mcp_servers/frankenmemory")

const sessionID = SessionID.make("ses_fm_only_live")
const clientName = "lifetools_fm_only_live"

const ctx = {
  sessionID,
  messageID: MessageID.make(""),
  callID: "fm-only-live",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [],
  metadata: () => Effect.void,
  ask: () => Effect.void,
}

afterEach(async () => {
  unregisterMemorySessionScope(sessionID)
  await Instance.disposeAll()
})

test(
  "MiMo memory tool recalls an FM-only record through the real process chain",
  async () => {
    if (process.env.OPEN_CLANK_LIVE_BUILD === "1") {
      const build = spawnSync("cargo", ["build", "--release", "-p", "fm-mcp"], {
        cwd: fmDir,
        encoding: "utf8",
        timeout: 300_000,
      })
      if (build.status !== 0) {
        throw new Error(`current-source fm-mcp build failed:\n${build.stdout}\n${build.stderr}`)
      }
    }
    await Promise.all([python, lifetools, fm].map((item) => fs.stat(item)))

    await using tmp = await tmpdir({
      outsideGit: true,
      config: {
        memory: { provider: "frankenmemory" },
        checkpoint: { memory_reconcile_on_search: false },
      },
    })
    const env = Object.fromEntries(
      Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
    )
    delete env.FM_DB_ID
    Object.assign(env, {
      OWNER: "alice",
      OPEN_CLANK_SKILL_OWNER: "alice",
      SESSION_ID: sessionID,
      WORKSPACE: tmp.path,
      FM_OWNER: "alice",
      FM_WORKSPACE_ID: "global",
      FM_DB_PATH: path.join(tmp.path, "frankenmemory.db"),
      FM_MCP_COMMAND: fm,
      OPEN_CLANK_DATA_DIR: path.join(tmp.path, "open-clank-data"),
      PYTHONPATH: repo,
    })

    const transport = new StdioClientTransport({
      command: python,
      args: [lifetools],
      cwd: repo,
      env,
      stderr: "pipe",
    })
    const client = new Client({ name: "mimo-fm-only-live-test", version: "1.0.0" })
    try {
      await client.connect(transport)
      const sentinel = `orbit-lantern-${crypto.randomUUID()}`
      const captured = await client.callTool({
        name: "capture",
        arguments: {
          content: `The FM-only acceptance marker is ${sentinel}.`,
          capture_mode: "manual",
          owner: "alice",
          workspace_id: "global",
          source: "user",
          source_type: "human",
          category: "fact",
          session_id: sessionID,
          session_key: sessionID,
        },
      })
      expect(captured.isError).not.toBeTrue()

      registerManagedMcpClient(clientName, client)
      registerMemorySessionScope(
        sessionID,
        [
          {
            name: clientName,
            command: python,
            args: [lifetools],
            env: [
              { name: "FM_OWNER", value: "alice" },
              { name: "FM_WORKSPACE_ID", value: "global" },
            ],
          },
        ],
        tmp.path,
      )

      const layer = Layer.mergeAll(
        Memory.defaultLayer,
        CrossSpawnSpawner.defaultLayer,
        Truncate.defaultLayer,
        Agent.defaultLayer,
      )
      const result = await Instance.provide({
        directory: tmp.path,
        fn: () =>
          Effect.runPromise(
            Effect.gen(function* () {
              const memory = yield* Memory.Service
              const root = yield* memory.root()
              yield* Effect.promise(() => fs.rm(root, { recursive: true, force: true }))
              const info = yield* MemoryTool
              const tool = yield* info.init()
              return yield* tool.execute({ operation: "search", query: sentinel, limit: 5 }, ctx)
            }).pipe(Effect.scoped, Effect.provide(layer)),
          ),
      })

      expect(result.output).toContain(sentinel)
      expect(result.metadata.backends).toEqual(["frankenmemory"])
    } finally {
      unregisterMemorySessionScope(sessionID)
      unregisterManagedMcpClient(clientName, client)
      await client.close()
    }
  },
  // This lane builds a release Rust binary and starts Python + MCP processes.
  // It normally completes in ~15s warm, but parallel test-file load and a
  // cold Cargo cache can legitimately exceed the package's 30s unit default.
  120_000,
)
