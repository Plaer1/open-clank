import { expect, test } from "bun:test"
import { parseCodexCatalog, parseCopilotCatalog, parseGenericCatalog } from "../../src/acp/provider-discovery"

test("Codex parser accepts arbitrary visible IDs and excludes hidden rows", () => {
  const parsed = parseCodexCatalog({
    models: [
      { slug: "new-model", visibility: "public" },
      { slug: "hidden-model", visibility: "hidden" },
      { slug: "another-model" },
      { slug: 42 },
    ],
  })

  expect(parsed.models.map((model) => model.modelID)).toEqual(["new-model", "another-model"])
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
