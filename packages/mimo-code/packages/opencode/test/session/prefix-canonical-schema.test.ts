import { expect, test } from "bun:test"
import { jsonSchema, tool } from "ai"
import { asSchema, lazySchema } from "@ai-sdk/provider-utils"
import { SessionPrefixSnapshot } from "../../src/session/prefix-snapshot"

test("lazy and eager schemas share the exact resolved advertisement and retain execution", async () => {
  const shape = { type: "object" as const, properties: { value: { type: "string" as const } }, required: ["value"] }
  let resolutions = 0
  const validate = (value: unknown) => ({ success: true as const, value })
  const eager = tool({ description: "read", inputSchema: jsonSchema(shape, { validate }), execute: async () => "ok" })
  const lazy = tool({ description: "read", inputSchema: lazySchema(() => { resolutions++; return jsonSchema(shape, { validate }) }), execute: async () => "ok" })
  const a = await SessionPrefixSnapshot.resolveTools({ read: eager }, ["read"])
  const b = await SessionPrefixSnapshot.resolveTools({ read: lazy }, ["read"])
  expect(resolutions).toBe(1)
  expect(SessionPrefixSnapshot.toolsHash(a.snapshots)).toBe(SessionPrefixSnapshot.toolsHash(b.snapshots))
  expect(await asSchema(b.tools.read!.inputSchema).jsonSchema).toEqual(b.snapshots[0]!.input_schema)
  expect(b.tools.read!.execute).toBe(lazy.execute)
  expect(asSchema(b.tools.read!.inputSchema).validate).toBe(validate)
})
