// A slow server must not hold tools() hostage: it returns what is ready after a
// bounded wait, and the late server shows up in a later call.
import { expect } from "bun:test"
import { Effect, Layer } from "effect"
import { MCP } from "../../src/mcp"
import { HostMcp } from "../../src/mcp/host"
import { McpAuth } from "../../src/mcp/auth"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const it = testEffect(Layer.mergeAll(MCP.defaultLayer, McpAuth.defaultLayer, CrossSpawnSpawner.defaultLayer))

it.live("tools() returns ready servers without waiting for a slow one, which appears later", () =>
  Effect.gen(function* () {
    let releaseSlow!: () => void
    const slowBlocked = new Promise<void>((r) => (releaseSlow = r))
    yield* Effect.addFinalizer(() =>
      Effect.sync(() => {
        releaseSlow()
        HostMcp.set({})
      }),
    )
    const server = yield* Effect.acquireRelease(
      Effect.sync(() =>
        Bun.serve({
          hostname: "127.0.0.1",
          port: 0,
          async fetch(request) {
            if (request.method !== "POST") return new Response(null, { status: 405 })
            const msg = (await request.json()) as { id?: number | string; method?: string }
            if (msg.id == null) return new Response(null, { status: 202 })
            const name = new URL(request.url).pathname === "/slow" ? "slow" : "fast"
            if (msg.method === "initialize" && name === "slow") await slowBlocked
            const result =
              msg.method === "initialize"
                ? { protocolVersion: "2024-11-05", capabilities: { tools: {} }, serverInfo: { name, version: "1" } }
                : { tools: [{ name: "ping", inputSchema: { type: "object" } }] }
            return Response.json({ jsonrpc: "2.0", id: msg.id, result })
          },
        }),
      ),
      (server) => Effect.promise(() => server.stop(true)),
    )
    const cfg = (path: string) => ({
      type: "remote" as const,
      url: `${server.url}${path}`,
      oauth: false as const,
      enabled: true,
    })
    HostMcp.set({ fast: cfg("fast"), slow: cfg("slow") })

    yield* provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const mcp = yield* MCP.Service

        const first = yield* mcp.tools()
        expect(Object.keys(first)).toEqual(["fast_ping"])

        releaseSlow()
        // status() observes the fully settled state.
        const status = yield* mcp.status()
        expect(status.fast?.status).toBe("connected")
        expect(status.slow?.status).toBe("connected")
        expect(Object.keys(yield* mcp.tools()).toSorted()).toEqual(["fast_ping", "slow_ping"])
      }),
    )
  }),
)
