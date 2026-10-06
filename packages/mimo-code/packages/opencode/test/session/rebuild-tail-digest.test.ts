import { describe, expect, test } from "bun:test"
import { MessageV2 } from "../../src/session/message-v2"
import { collapseCheckpointTail, renderTailDigest } from "../../src/session/tail-digest"
import type { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { MessageID, PartID, SessionID } from "../../src/session/schema"

const sessionID = SessionID.make("session")
const providerID = ProviderID.make("test")
const model: Provider.Model = {
  id: ModelID.make("test-model"),
  providerID,
  api: { id: "test-model", url: "https://example.com", npm: "@ai-sdk/openai" },
  name: "Test Model",
  capabilities: {
    temperature: true,
    reasoning: false,
    attachment: false,
    toolcall: true,
    input: { text: true, audio: false, image: false, video: false, pdf: false },
    output: { text: true, audio: false, image: false, video: false, pdf: false },
    interleaved: false,
  },
  cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
  limit: { context: 0, input: 0, output: 0 },
  status: "active",
  options: {},
  headers: {},
  release_date: "2026-01-01",
}

function user(id: string): MessageV2.User {
  return {
    id: MessageID.make(id),
    sessionID,
    role: "user",
    time: { created: 0 },
    agent: "user",
    model: { providerID, modelID: ModelID.make("test") },
    tools: {},
    mode: "",
  } as MessageV2.User
}

function assistant(id: string): MessageV2.Assistant {
  return {
    id: MessageID.make(id),
    sessionID,
    role: "assistant",
    time: { created: 0 },
    parentID: MessageID.make("user"),
    modelID: model.id,
    providerID,
    mode: "build",
    agent: "build",
    path: { cwd: "/", root: "/" },
    cost: 0,
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
  } as MessageV2.Assistant
}

function part(messageID: string, id: string) {
  return { id: PartID.make(id), sessionID, messageID: MessageID.make(messageID) }
}

function tool(messageID: string, id: string, state: MessageV2.ToolPart["state"], output = "SECRET_TOOL_OUTPUT") {
  return {
    ...part(messageID, id),
    type: "tool" as const,
    callID: `call-${id}`,
    tool: "read",
    state: state.status === "completed" ? { ...state, output } : state,
  } as MessageV2.Part
}

function boundary(id: string, digestUpTo?: string): MessageV2.WithParts {
  return {
    info: user(id),
    parts: [
      {
        ...part(id, `${id}-checkpoint`),
        type: "checkpoint" as const,
        checkpointDir: "",
        checkpointNumber: 0,
        coveredUpTo: MessageID.make("msg_01"),
        ...(digestUpTo ? { digestUpTo: MessageID.make(digestUpTo) } : {}),
      } as MessageV2.Part,
      {
        ...part(id, `${id}-text`),
        type: "text" as const,
        synthetic: true,
        text: "# Session checkpoint\n\n# Recent activity\n\n- read(path=\"old.ts\")",
      } as MessageV2.Part,
    ],
  }
}

const completed = (input: Record<string, unknown>) => ({
  status: "completed" as const,
  input,
  output: "",
  title: "read",
  metadata: {},
  time: { start: 0, end: 1 },
})

describe("rebuild tail digest", () => {
  test("renders bounded assistant activity without tool outputs or prior boundaries", () => {
    const digest = renderTailDigest([
      boundary("msg_boundary", "msg_01"),
      {
        info: assistant("msg_01"),
        parts: [
          tool("msg_01", "tool", completed({ path: "src/example.ts", body: "x".repeat(300) })),
          { ...part("msg_01", "text"), type: "text" as const, text: "completed the inspection" } as MessageV2.Part,
          {
            ...part("msg_01", "subtask"),
            type: "subtask" as const,
            prompt: "ignored prompt",
            description: "inspect",
            agent: "explore",
            command: "rg checkpoint",
          } as MessageV2.Part,
          { ...part("msg_01", "synthetic"), type: "text" as const, text: "internal", synthetic: true } as MessageV2.Part,
        ],
      },
    ])
    expect(digest).toContain("# Recent activity")
    expect(digest).toContain('- read(path="src/example.ts"')
    expect(digest).toContain("- assistant: completed the inspection")
    expect(digest).toContain("- subtask: rg checkpoint")
    expect(digest).not.toContain("SECRET_TOOL_OUTPUT")
    expect(digest).not.toContain("old.ts")
    expect(digest).not.toContain("internal")
    expect(digest.length).toBeLessThan(900)
  })

  test("never includes tool error payloads", () => {
    const digest = renderTailDigest([
      {
        info: assistant("msg_error"),
        parts: [
          tool("msg_error", "error", {
            status: "error",
            input: { path: "safe.ts" },
            error: "SECRET_ERROR_OUTPUT",
            time: { start: 0, end: 1 },
          }),
        ],
      },
    ])
    expect(digest).toContain('- read(path="safe.ts") → error')
    expect(digest).not.toContain("SECRET_ERROR_OUTPUT")
  })

  test("keeps the newest bounded activity and marks interruption only when its line is retained", () => {
    const pending = (i: number) => ({
      info: assistant(`msg_${String(i).padStart(3, "0")}`),
      parts: [
        tool(`msg_${String(i).padStart(3, "0")}`, `tool_${i}`, {
          status: "pending" as const,
          input: { command: `cmd-${i}` },
          raw: "",
        }),
      ],
    })
    const visible = renderTailDigest([pending(0)])
    expect(visible).toContain("tool loop interrupted")

    const completedMessage = (i: number) => ({
      info: assistant(`done_${String(i).padStart(3, "0")}`),
      parts: [tool(`done_${String(i).padStart(3, "0")}`, `done_tool_${i}`, completed({ path: `p-${i}` }))],
    })
    const newestPending = renderTailDigest(
      Array.from({ length: 200 }, (_, i) => completedMessage(i)).concat([pending(201)]),
    )
    expect(newestPending).toContain("tool loop interrupted")
    expect(newestPending).not.toContain('path="p-0"')
    expect(newestPending).toContain('path="p-199"')

    const oldestPending = renderTailDigest(
      [pending(0)].concat(
        Array.from({ length: 200 }, (_, i) => ({
          info: assistant(`done_${i}`),
          parts: [tool(`done_${i}`, `done_tool_${i}`, completed({ path: `p-${i}` }))],
        })),
      ),
    )
    expect(oldestPending).not.toContain("tool loop interrupted")
  })

  test("collapses the stable pre-insert ID range while preserving every user turn and post-insert work", async () => {
    const input: MessageV2.WithParts[] = [
      { info: assistant("msg_02"), parts: [tool("msg_02", "pre", completed({ path: "pre.ts" }), "PRE_BODY")] },
      boundary("msg_cp", "msg_03"),
      {
        info: user("msg_03"),
        parts: [{ ...part("msg_03", "file"), type: "file" as const, mime: "image/png", url: "data:image/png;base64,x" } as MessageV2.Part],
      },
      { info: assistant("msg_04"), parts: [tool("msg_04", "post", completed({ path: "post.ts" }), "POST_BODY")] },
    ]
    const collapsed = collapseCheckpointTail(input)
    expect(collapsed.map((m) => String(m.info.id))).toEqual(["msg_cp", "msg_03", "msg_04"])

    const messages = await MessageV2.toModelMessages(input, model, { collapseCheckpointTail: true })
    const rendered = JSON.stringify(messages)
    expect(rendered).toContain("# Recent activity")
    expect(rendered).not.toContain("PRE_BODY")
    expect(rendered).toContain("POST_BODY")
    expect(rendered).toContain("image/png")

    const verbatim = JSON.stringify(await MessageV2.toModelMessages(input, model))
    expect(verbatim).toContain("PRE_BODY")
  })

  test("leaves legacy checkpoint boundaries without a digest range verbatim", () => {
    const input = [boundary("msg_cp"), { info: assistant("msg_02"), parts: [tool("msg_02", "tool", completed({}))] }]
    expect(collapseCheckpointTail(input)).toBe(input)
  })
})
