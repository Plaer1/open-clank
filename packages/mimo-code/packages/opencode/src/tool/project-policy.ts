import type * as Tool from "./tool"
import { callBoundOpenClankTool } from "@/memory/mcp-client"

type Candidate = {
  path: string
  content: string | Uint8Array | null
}

type PolicyResult = {
  enforced?: boolean
  allowed?: boolean
  reason?: string
}

function required() {
  return process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE === "required"
}

function encode(content: string | Uint8Array) {
  return typeof content === "string"
    ? Buffer.from(content).toString("base64")
    : Buffer.from(content.buffer, content.byteOffset, content.byteLength).toString("base64")
}

async function callPolicy(
  ctx: Tool.Context,
  input: Record<string, unknown>,
): Promise<PolicyResult> {
  if (!required()) return { enforced: false, allowed: true }
  const result = await callBoundOpenClankTool(
    ctx.sessionID,
    "project_mutation_policy",
    input,
  )
  const block = (result.content as Array<{ type: string; text?: string }> | undefined)?.[0]
  const parsed = JSON.parse(block?.text ?? "{}") as PolicyResult
  if (parsed.allowed !== true) {
    throw new Error(parsed.reason || "active Open Clank project policy blocked the mutation")
  }
  return parsed
}

export function assertProjectFilePolicy(ctx: Tool.Context, candidates: Candidate[]) {
  return callPolicy(ctx, {
    mode: "files",
    candidates: candidates.map((candidate) => ({
      path: candidate.path,
      ...(candidate.content === null
        ? { deleted: true }
        : { content_base64: encode(candidate.content) }),
    })),
  })
}

export function assertProjectShellPolicy(ctx: Tool.Context) {
  return callPolicy(ctx, { mode: "shell" })
}
