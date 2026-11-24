import { parseJsonInput } from "../util/toon-subset"
import type { GcfArtifact } from "./gcf-native"

/** Representation only: the source receipt and current authority are owned by the caller. */
export function graphGcfView(original: string, artifact: GcfArtifact, memoScope: string | undefined, primer: string): { detected: false } | { detected: true; frame?: string } {
  let inner: string
  let order: string[]
  try {
    const wrapper = parseJsonInput(original)
    if (wrapper.k !== "o" || wrapper.v.length !== 2) return { detected: false }
    order = wrapper.v.map(([key]) => key)
    const output = wrapper.v.find(([key]) => key === "output")?.[1]
    const exit = wrapper.v.find(([key]) => key === "exit_code")?.[1]
    if (output?.k !== "a" || typeof output.v !== "string" || exit?.k !== "a" || exit.v !== 0) return { detected: false }
    inner = output.v
  } catch { return { detected: false } }
  let value: unknown
  try { value = JSON.parse(inner) } catch { return { detected: false } }
  if (!value || typeof value !== "object" || Array.isArray(value) || (value as Record<string, unknown>).schema !== "open-clank.code-query/v1") return { detected: false }
  const graph = value as Record<string, unknown>
  if (graph.error !== null || graph.partial !== false || graph.degraded !== false) return { detected: true }
  try {
    if (artifact.availability() !== "available" || artifact.decodeAvailability() !== "available") return { detected: true }
    const input = Buffer.from(inner, "utf8")
    const encoded = artifact.encode(input, "gcf-generic-v3-safe-integers-v1", memoScope)
    if (encoded.state !== "encoded" || artifact.availability() !== "available") return { detected: true }
    const decoded = artifact.decode(encoded.output, "gcf-generic-v3-safe-integers-v1")
    if (decoded.state !== "decoded" || !decoded.output.equals(input) || artifact.availability() !== "available") return { detected: true }
    const body = new TextDecoder("utf-8", { fatal: true }).decode(encoded.output)
    if (!body.startsWith("GCF profile=generic\n") || !body.endsWith("\n")) return { detected: true }
    const frame = `[The decoded payload is the original UTF-8 JSON text value of output. The original wrapper fields are in this order: ${order.join(", ")}; exit_code is 0.]\n${primer}\n${body}`
    return Buffer.byteLength(frame, "utf8") < Buffer.byteLength(original, "utf8") ? { detected: true, frame } : { detected: true }
  } catch { return { detected: true } }
}
