import { expect, test } from "bun:test"
import { parseClaudeCatalog, parseCodexCatalog, parseCopilotCatalog, parseGenericCatalog, parseGrokCatalog } from "../../src/acp/provider-discovery"
import { CLAUDE_DISCOVERY_REQUEST, claudeDiscoveryRequest } from "../../src/plugin/claude"

test("Codex parser accepts arbitrary visible IDs and excludes hidden rows", () => {
  const parsed = parseCodexCatalog({
    models: [
      {
        slug: "gpt-6.1-terra",
        display_name: "GPT-6.1 Terra",
        visibility: "public",
        default_reasoning_level: "high",
        supported_reasoning_levels: [
          { effort: "low", description: "low" },
          { effort: "medium", description: "medium" },
          { effort: "high", description: "high" },
          { effort: "none", description: "explicit none fixture" },
          { effort: "0", description: "explicit zero fixture" },
          { effort: "high", description: "duplicate" },
          { effort: "inherit", description: "reserved default sentinel" },
        ],
      },
      { slug: "hidden-model", visibility: "hidden" },
      { slug: "another-model" },
      { slug: 42 },
    ],
  })

  expect(parsed.models.map((model) => model.modelID)).toEqual(["gpt-6.1-terra", "another-model"])
  expect(parsed.models[0].displayName).toBe("GPT-6.1 Terra")
  expect(parsed.models[0].capabilities).toMatchObject({
    reasoning: true,
    reasoning_effort: ["low", "medium", "high", "none", "0"],
    reasoning_default_effort: "high",
  })
  expect(parsed.models[1].capabilities).not.toHaveProperty("reasoning_effort")
  expect(parsed.invalidRows).toBe(1)
  expect(parsed.rowCount).toBe(4)
})

test("Copilot parser marks malformed mixed rows while preserving valid rows", () => {
  const parsed = parseCopilotCatalog({
    data: [
      { id: "copilot-a", name: "A", capabilities: {} },
      { id: "disabled", model_picker_enabled: false, capabilities: {} },
      { id: "malformed" },
    ],
  })

  expect(parsed.models.map((model) => model.modelID)).toEqual(["copilot-a"])
  expect(parsed.invalidRows).toBe(1)
})

test("generic parser distinguishes valid empty inventories from malformed payloads", () => {
  expect(parseGenericCatalog({ data: [] })).toEqual({ models: [], invalidRows: 0, rowCount: 0 })
  expect(parseGenericCatalog({ data: [{ id: "new-id" }, { name: "named-id" }, null] }).models).toHaveLength(2)
  expect(parseGenericCatalog({ invalid: true })).toMatchObject({ models: [], invalidRows: 1, rowCount: 0 })
})

test("Grok catalog uses account rows and retains only explicit capability fields", () => {
  const parsed = parseGrokCatalog({
    data: [
      {
        id: "account-model",
        name: "Account model",
        capabilities: {
          tool_call: true,
          modalities: { input: ["text", "image"], output: ["text"] },
          secret_metadata: "discard-me",
        },
      },
      { id: "hidden", hidden: true },
      { id: 42 },
    ],
  })
  expect(parsed.models.map((model) => model.modelID)).toEqual(["account-model"])
  expect(parsed.models[0]?.capabilities).toMatchObject({
    tool_call: true,
    modalities: { input: ["text", "image"], output: ["text"] },
  })
  expect(JSON.stringify(parsed)).not.toContain("discard-me")
  expect(parseGrokCatalog({ data: [] })).toEqual({ models: [], invalidRows: 0, rowCount: 0 })
})

test("Claude parser preserves IDs/display names and keeps unknown capabilities conservative", () => {
  const parsed = parseClaudeCatalog({
    data: [
      { id: "claude-opus", display_name: "Claude Opus" },
      { id: "claude-unknown" },
      { id: "claude-opus", display_name: "duplicate" },
      { id: 42 },
    ],
  })
  expect(parsed.status).toBe("ready")
  expect(parsed.hasMore).toBe(false)
  expect(parsed.models.map((model) => [model.modelID, model.displayName])).toEqual([
    ["claude-opus", "Claude Opus"],
    ["claude-unknown", "claude-unknown"],
  ])
  expect(parsed.invalidRows).toBe(2)
  expect(parsed.models[1]?.capabilities).not.toHaveProperty("vision")
  expect(parsed.models[1]?.capabilities).not.toHaveProperty("tool_call")
})

test("Claude parser distinguishes malformed, legitimate empty, and partial pagination", () => {
  expect(parseClaudeCatalog({ invalid: true })).toMatchObject({ status: "malformed", models: [], hasMore: false })
  expect(parseClaudeCatalog({ data: [] })).toMatchObject({ status: "empty", models: [], hasMore: false, invalidRows: 0 })
  expect(parseClaudeCatalog({ data: [{ id: "one" }], has_more: true })).toMatchObject({ status: "partial", hasMore: true })
  expect(parseClaudeCatalog({ data: [{ id: "one" }], has_more: true })).not.toHaveProperty("nextCursor")
  const bounded = parseClaudeCatalog({ data: Array.from({ length: 1001 }, (_, index) => ({ id: `m-${index}` })) })
  expect(bounded.models).toHaveLength(1000)
  expect(bounded.rowCount).toBe(1000)
  expect(bounded.status).toBe("partial")
})

test("Claude discovery request profile pins URL and leased bearer without API-key auth", () => {
  expect(CLAUDE_DISCOVERY_REQUEST.url).toBe("https://api.anthropic.com/v1/models?limit=1000")
  const request = claudeDiscoveryRequest("leased-access")
  const headers = new Headers(request.init.headers)
  expect(request.url).toBe(CLAUDE_DISCOVERY_REQUEST.url)
  expect(headers.get("authorization")).toBe("Bearer leased-access")
  expect(headers.get("anthropic-version")).toBeTruthy()
  expect(headers.get("anthropic-beta")).toBeTruthy()
  expect(headers.get("user-agent")).toBe("OpenClank/managed-anthropic")
  expect(headers.has("x-api-key")).toBe(false)
  expect(headers.has("api-key")).toBe(false)
})
