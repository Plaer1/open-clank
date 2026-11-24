import z from "zod"
import { Effect } from "effect"
import { HttpClient } from "effect/unstable/http"
import * as Tool from "../tool"
import * as McpExa from "../mcp-exa"
import * as MimoWebsearch from "./mimo"
import { Auth } from "@/auth"
import { Provider } from "@/provider"
import { isXiaomiFamilyProvider } from "@/provider/xiaomi-family"
import { ManagedProvider } from "@/acp/managed-provider"
import { managedSearch } from "@/acp/managed-search"
import DESCRIPTION from "./websearch.txt"

const NO_RESULTS =
  "Web search returned no results. Try a different query, or use `webfetch` with a relevant URL."
const MAX_TIMEOUT = 120 * 1000 // 2 minutes

/** The Xiaomi gateway search is opt-in; nothing turns it on by default. */
export const xiaomiGatewayEnabled = () => process.env.OPEN_CLANK_XIAOMI_WEBSEARCH === "1"

const Parameters = z.object({
  query: z.string().describe("Websearch query"),
  numResults: z.number().optional().describe("Number of search results to return (default: 8)"),
  timeout: z.number().describe("Optional timeout in seconds (max 120)").optional(),
  livecrawl: z
    .enum(["fallback", "preferred"])
    .optional()
    .describe(
      "Live crawl mode - 'fallback': use live crawling as backup if cached content unavailable, 'preferred': prioritize live crawling (default: 'fallback')",
    ),
  type: z
    .enum(["auto", "fast", "deep"])
    .optional()
    .describe("Search type - 'auto': balanced search (default), 'fast': quick results, 'deep': comprehensive search"),
  contextMaxCharacters: z
    .number()
    .optional()
    .describe("Maximum characters for context string optimized for LLMs (default: 10000)"),
  freshness: z.enum(["day", "week", "month", "year"]).optional().describe("Optional freshness filter"),
})

export const WebSearchTool = Tool.define(
  "websearch",
  Effect.gen(function* () {
    const http = yield* HttpClient.HttpClient
    const auth = yield* Auth.Service

    return {
      get description() {
        return DESCRIPTION.replace("{{year}}", new Date().getFullYear().toString())
      },
      parameters: Parameters,
      execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          yield* ctx.ask({
            permission: "websearch",
            patterns: [params.query],
            always: ["*"],
            metadata: {
              query: params.query,
              numResults: params.numResults,
              livecrawl: params.livecrawl,
              type: params.type,
              contextMaxCharacters: params.contextMaxCharacters,
              timeout: params.timeout,
              freshness: params.freshness,
            },
          })

          const timeout = params.timeout === undefined ? undefined : Math.min(params.timeout * 1000, MAX_TIMEOUT)

          if (ManagedProvider.enabled()) {
            const count = params.numResults
            if (count !== undefined && (!Number.isSafeInteger(count) || count < 1 || count > 50)) throw new Error("managed web search result count must be between 1 and 50")
            const response = yield* Effect.tryPromise(() => managedSearch(
              { sessionID: ctx.sessionID, actorID: (ctx.extra?.actorExecution as { actorID?: string } | undefined)?.actorID, executionRevision: (ctx.extra?.actorExecution as { executionRevision?: number } | undefined)?.executionRevision, callID: ctx.callID, abort: ctx.abort },
              { query: params.query, count, freshness: params.freshness },
              timeout ?? 30_000,
            ))
            return { output: JSON.stringify(response), title: `Web search: ${params.query}`, metadata: {} }
          }

          // Xiaomi search gateway (`./mimo`): an Open Clank-owned compatibility provider that upstream
          // dropped. Off unless OPEN_CLANK_XIAOMI_WEBSEARCH=1; the default path is upstream's Exa search.
          const model = (ctx.extra as { model?: Provider.Model })?.model
          const gateway = xiaomiGatewayEnabled() && model && isXiaomiFamilyProvider(model.providerID) ? model : undefined
          const result = gateway
            ? yield* Effect.catchCause(
                Effect.gen(function* () {
                  const info = yield* auth.get(gateway.providerID)
                  if (!info || info.type !== "api") return undefined
                  return yield* MimoWebsearch.call(
                    http,
                    gateway.api.url,
                    info.key,
                    params.query,
                    "mimo-v2.5",
                    timeout ?? "30 seconds",
                  )
                }),
                () => Effect.succeed(undefined),
              )
            : yield* McpExa.call(
                http,
                "web_search_exa",
                McpExa.SearchArgs,
                {
                  query: params.query,
                  type: params.type || "auto",
                  numResults: params.numResults || 8,
                  livecrawl: params.livecrawl || "fallback",
                  contextMaxCharacters: params.contextMaxCharacters,
                },
                timeout ?? "25 seconds",
              )

          return {
            output: result ?? NO_RESULTS,
            title: `Web search: ${params.query}`,
            metadata: {},
          }
        }).pipe(Effect.orDie),
    }
  }),
)
