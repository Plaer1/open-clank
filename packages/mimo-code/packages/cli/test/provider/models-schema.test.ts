import { expect, test } from "bun:test"
import { validateCatalog } from "../../src/provider/models-schema"

const fixturePath = `${import.meta.dir}/../tool/fixtures/models-api.json`

test("validates the pinned catalog without dropping source metadata", async () => {
  const fixture = await Bun.file(fixturePath).json()
  const validated = validateCatalog(fixture)

  expect(validated).toEqual(fixture)
  expect(validated["ollama-cloud"].models["mistral-large-3:675b"].limit.context).toBe(262144)
  expect((validated["ollama-cloud"] as Record<string, unknown>).doc).toBe("https://docs.ollama.com/cloud")
})

test("rejects provider and model identity drift", async () => {
  const fixture = await Bun.file(fixturePath).json()

  const providerDrift = structuredClone(fixture)
  providerDrift["ollama-cloud"].id = "other-provider"
  expect(() => validateCatalog(providerDrift)).toThrow("provider identity mismatch")

  const modelDrift = structuredClone(fixture)
  modelDrift["ollama-cloud"].models["mistral-large-3:675b"].id = "other-model"
  expect(() => validateCatalog(modelDrift)).toThrow("model identity mismatch")
})

test("rejects an empty catalog unless explicitly allowed", () => {
  expect(() => validateCatalog({}, false)).toThrow("contains no models")
  expect(validateCatalog({}, true)).toEqual({})
})
