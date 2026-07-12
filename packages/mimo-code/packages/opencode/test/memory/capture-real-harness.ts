// Shared harness for the real-traffic capture tests. One test per FILE by
// design: sequential Instance/runtime pairs inside one bun process pollute
// each other (observed 2026-07-08: capture silently no-ops for the second
// test in a file), and per-file processes give each test its own fm-mcp
// singleton + FM_DB. REAL traffic by management order — real DeepSeek, real
// fm-mcp, no fakes.
import { Database as BunDB } from "bun:sqlite"
import { Effect, Layer, ManagedRuntime } from "effect"
import * as fs from "fs"
import * as os from "os"
import path from "path"
import { Session } from "../../src/session"
import { SessionPrompt } from "../../src/session/prompt"
import * as MemoryCapture from "../../src/memory/capture"
import { memoMap } from "../../src/effect/memo-map"

export const FM_BIN = "/home/e/sauce/ai/open-clank/mcp_servers/frankenmemory/target/release/fm-mcp"

// ONE shared fm DB per bun process: all test files run in a single process
// and the fm-mcp client is a process singleton that snapshots env at first
// spawn — per-file DB paths silently diverge from where rows actually land.
// Assertions therefore scope by marker/entity, never by whole-table counts,
// and nothing deletes the DB mid-suite (the OS owns tmp cleanup).
export const FM_DB = path.join(os.tmpdir(), `fm-realtraffic-${process.pid}.db`)
process.env["FM_MCP_COMMAND"] = FM_BIN
process.env["FM_DB_PATH"] = FM_DB

// Credentials arrive ONLY via the standard env-var channel (management
// order: everything above the board — never scrape keys out of config
// files). The test preload hermetically redirects HOME/XDG, so the user's
// global mimocode config is deliberately invisible here; export
// FM_TEST_DEEPSEEK_API_KEY (repo .env carries it, gitignored) to run them —
// a dedicated name because the preload wipes DEEPSEEK_API_KEY on purpose.
// Without it the real-traffic tests SKIP loudly instead of faking anything.
export const hasCreds = Boolean(process.env["FM_TEST_DEEPSEEK_API_KEY"]?.trim())
export const skipReason = "FM_TEST_DEEPSEEK_API_KEY not set — real-traffic memory tests need it (see repo .env)"

export const testConfig = (memory: boolean, graph?: { enabled?: boolean; model?: string }) => ({
  provider: {
    deepseek: {
      options: { apiKey: process.env["FM_TEST_DEEPSEEK_API_KEY"] ?? "", baseURL: "https://api.deepseek.com" },
      models: {
        "deepseek-v4-flash": {
          id: "deepseek-v4-flash",
          name: "DeepSeek V4 Flash",
          reasoning: true,
          limit: { context: 1000000, output: 384000 },
          cost: { input: 0.14, output: 0.28 },
        },
        "deepseek-v4-pro-instant": {
          id: "deepseek-v4-pro-instant",
          name: "DeepSeek V4 Pro (fast)",
          reasoning: false,
          limit: { context: 1000000, output: 384000 },
          cost: { input: 1.74, output: 3.48 },
        },
      },
    },
  },
  agent: {
    build: { model: "deepseek/deepseek-v4-flash" },
    compaction: { model: "deepseek/deepseek-v4-flash" },
  },
  ...(memory
    ? { memory: { provider: "frankenmemory" as const, ...(graph === undefined ? {} : { graph }) } }
    : {}),
})

// The runtime MUST share the process-wide layer memoMap: session events are
// published through the module-level Bus helpers (their own makeRuntime),
// and only memoMap sharing makes that Bus the same instance as the one the
// capture subscription registers on — exactly like production runtimes.
const rt = ManagedRuntime.make(
  Layer.mergeAll(MemoryCapture.defaultLayer, Session.defaultLayer, SessionPrompt.defaultLayer),
  { memoMap },
)

export function run<A, E>(
  fx: Effect.Effect<A, E, MemoryCapture.CaptureService | Session.Service | SessionPrompt.Service>,
) {
  return rt.runPromise(fx.pipe(Effect.scoped))
}

export function fmRowsContaining(marker: string): number {
  if (!fs.existsSync(FM_DB)) return 0
  const db = new BunDB(FM_DB, { readonly: true })
  try {
    const raw = db.query("SELECT count(*) AS c FROM raw WHERE content LIKE ?").get(`%${marker}%`) as { c: number }
    const curated = db
      .query("SELECT count(*) AS c FROM curated WHERE content LIKE ?")
      .get(`%${marker}%`) as { c: number }
    return raw.c + curated.c
  } finally {
    db.close()
  }
}

/** Real model turn + in-instance settle so the async capture callback runs
 * while the instance is alive. */
export function realTurn(input: { agent: string; marker: string; settleMs: number; text?: string }) {
  return Effect.gen(function* () {
    yield* MemoryCapture.CaptureService.use((s) => s.init())
    const sessions = yield* Session.Service
    const prompt = yield* SessionPrompt.Service
    const session = yield* sessions.create({})
    yield* prompt.prompt({
      sessionID: session.id,
      agent: input.agent,
      parts: [
        {
          type: "text",
          text:
            input.text ??
            `Reply with exactly one short sentence that contains the token ${input.marker} verbatim. No other requirements.`,
        },
      ],
    })
    yield* Effect.promise(() => new Promise((r) => setTimeout(r, input.settleMs)))
    return session.id
  })
}

export function fmGraphNodesContaining(sub: string): number {
  if (!fs.existsSync(FM_DB)) return 0
  const db = new BunDB(FM_DB, { readonly: true })
  try {
    return (
      db.query("SELECT count(*) AS c FROM graph_nodes WHERE norm_name LIKE ?").get(`%${sub.toLowerCase()}%`) as {
        c: number
      }
    ).c
  } finally {
    db.close()
  }
}

export function fmGraphEdgesTouching(sub: string): number {
  if (!fs.existsSync(FM_DB)) return 0
  const db = new BunDB(FM_DB, { readonly: true })
  try {
    return (
      db
        .query(
          `SELECT count(*) AS c FROM graph_edges e
           JOIN graph_nodes s ON s.id = e.src_id
           JOIN graph_nodes d ON d.id = e.dst_id
           WHERE s.norm_name LIKE ?1 OR d.norm_name LIKE ?1`,
        )
        .get(`%${sub.toLowerCase()}%`) as { c: number }
    ).c
  } finally {
    db.close()
  }
}
