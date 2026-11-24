import { describe, expect, test } from "bun:test"
import path from "node:path"
import { fileResult } from "../../src/tool/file-contract"

const specification = await Bun.file(
  path.resolve(import.meta.dir, "../../../../../../tests/fixtures/file_tool_contract_v1.json"),
).json()

function validate(result: ReturnType<typeof fileResult>) {
  for (const field of specification.required) expect(result).toHaveProperty(field)
  for (const field of specification.page_required) expect(result.page).toHaveProperty(field)
  expect(result.contract).toBe(specification.contract)
  expect(specification.operations).toContain(result.operation)
  expect(specification.kinds).toContain(result.kind)
  expect(specification.page_units).toContain(result.page.unit)
  if (result.truncation_reason) expect(specification.truncation_reasons).toContain(result.truncation_reason)
  if (result.search_mode) expect(specification.search_modes).toContain(result.search_mode)
}

describe("Open Clank file result contract", () => {
  for (const sample of [
    fileResult({
      operation: "read",
      path: "/workspace/note.txt",
      kind: "text",
      page: { unit: "line", cursor: 1, next_cursor: null, has_more: false, returned: 1, total: 1 },
    }),
    fileResult({
      operation: "write",
      path: "/workspace/note.txt",
      kind: "text",
      page: { unit: "byte", cursor: 0, next_cursor: null, has_more: false, returned: 4, total: 4 },
    }),
    fileResult({
      operation: "edit",
      path: "/workspace/note.txt",
      kind: "text",
      page: { unit: "byte", cursor: 0, next_cursor: null, has_more: false, returned: 4, total: 4 },
    }),
    fileResult({
      operation: "list",
      path: "/workspace",
      kind: "directory",
      page: { unit: "entry", cursor: 0, next_cursor: null, has_more: false, returned: 1, total: 1 },
      items: [{ path: "/workspace/note.txt", kind: "file" }],
    }),
    fileResult({
      operation: "grep",
      path: "/workspace",
      kind: "search",
      page: { unit: "result", cursor: 0, next_cursor: 1, has_more: true, returned: 1, total: 2 },
      truncation_reason: "result_limit",
      search_mode: "literal",
      items: [{ path: "/workspace/note.txt", line: 1, text: "needle" }],
    }),
  ]) {
    test(`${sample.operation} uses the shared envelope`, () => validate(sample))
  }
})
