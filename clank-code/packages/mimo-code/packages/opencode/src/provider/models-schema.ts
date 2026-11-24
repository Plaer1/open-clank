import z from "zod"

type JsonValue = string | number | boolean | null | { [key: string]: JsonValue } | JsonValue[]
const JsonValue: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([z.string(), z.number(), z.boolean(), z.null(), z.array(JsonValue), z.record(z.string(), JsonValue)]),
)

const Cost = z.object({
  input: z.number(),
  output: z.number(),
  cache_read: z.number().optional(),
  cache_write: z.number().optional(),
  context_over_200k: z
    .object({
      input: z.number(),
      output: z.number(),
      cache_read: z.number().optional(),
      cache_write: z.number().optional(),
    })
    .optional(),
})

export const Model = z.object({
  id: z.string(),
  name: z.string(),
  family: z.string().optional(),
  release_date: z.string(),
  attachment: z.boolean(),
  reasoning: z.boolean(),
  temperature: z.boolean(),
  tool_call: z.boolean(),
  // Host speech metadata is optional because it is not present in models.dev.
  voice_design: z.boolean().optional().default(false),
  voice_clone: z.boolean().optional().default(false),
  interleaved: z
    .union([
      z.literal(true),
      z.object({ field: z.enum(["reasoning", "reasoning_content", "reasoning_details"]) }).strict(),
    ])
    .optional(),
  cost: Cost.optional(),
  limit: z.object({ context: z.number(), input: z.number().optional(), output: z.number() }),
  modalities: z
    .object({
      input: z.array(z.enum(["text", "audio", "image", "video", "pdf"])),
      output: z.array(z.enum(["text", "audio", "image", "video", "pdf"])),
    })
    .optional(),
  experimental: z
    .object({
      modes: z
        .record(
          z.string(),
          z.object({
            cost: Cost.optional(),
            provider: z
              .object({
                body: z.record(z.string(), JsonValue).optional(),
                headers: z.record(z.string(), z.string()).optional(),
              })
              .optional(),
          }),
        )
        .optional(),
    })
    .optional(),
  status: z.enum(["alpha", "beta", "deprecated"]).optional(),
  provider: z.object({ npm: z.string().optional(), api: z.string().optional() }).optional(),
})
export type Model = z.infer<typeof Model>

export const Provider = z.object({
  api: z.string().optional(),
  name: z.string(),
  env: z.array(z.string()),
  id: z.string(),
  npm: z.string().optional(),
  models: z.record(z.string(), Model),
})
export type Provider = z.infer<typeof Provider>

/** Validate catalog structure while preserving unknown upstream fields in the snapshot. */
export function validateCatalog(input: unknown, allowEmpty = false): Record<string, Provider> {
  const wireModel = Model.partial().required({ id: true, name: true, limit: true })
  const parsed = z.record(z.string(), Provider.extend({ models: z.record(z.string(), wireModel) })).parse(input)
  if (!allowEmpty && !Object.values(parsed).some((provider) => Object.keys(provider.models).length > 0)) {
    throw new Error("models.dev catalog contains no models")
  }
  for (const [providerId, provider] of Object.entries(parsed)) {
    if (!providerId || provider.id !== providerId) throw new Error("models.dev provider identity mismatch")
    for (const [modelId, model] of Object.entries(provider.models)) {
      if (!modelId || model.id !== modelId) throw new Error("models.dev model identity mismatch")
    }
  }
  return structuredClone(input) as Record<string, Provider>
}
