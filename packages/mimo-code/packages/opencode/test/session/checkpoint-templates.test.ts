import { describe, expect, test } from "bun:test"
import { CHECKPOINT_TEMPLATE, CHECKPOINT_SECTION_BUDGETS, NOTES_TEMPLATE } from "../../src/session/checkpoint-templates"

describe("Checkpoint templates v8.1", () => {
  test("CHECKPOINT_TEMPLATE includes §10 Design decisions (F13)", () => {
    expect(CHECKPOINT_TEMPLATE).toContain("## §10 Design decisions and discussion outcomes")
    expect(CHECKPOINT_TEMPLATE).toMatch(/§10[\s\S]*Decisions reached through discussion/)
  })

  test("CHECKPOINT_SECTION_BUDGETS includes §10 entry (F13)", () => {
    expect(CHECKPOINT_SECTION_BUDGETS["§10 Design decisions and discussion outcomes"]).toBe(3000)
  })

  test("CHECKPOINT_TEMPLATE includes §11 Open notes (F19)", () => {
    expect(CHECKPOINT_TEMPLATE).toContain("## §11 Open notes")
    expect(CHECKPOINT_TEMPLATE).toMatch(/§11[\s\S]*Writer-curated catch-all/)
  })

  test("CHECKPOINT_SECTION_BUDGETS includes §11 entry (F19)", () => {
    expect(CHECKPOINT_SECTION_BUDGETS["§11 Open notes"]).toBe(800)
  })

  test("NOTES_TEMPLATE exists for new session notes file (F14)", () => {
    expect(NOTES_TEMPLATE).toBeDefined()
    expect(NOTES_TEMPLATE).toContain("# Session notes")
    expect(NOTES_TEMPLATE).toContain("turn N")
    expect(NOTES_TEMPLATE).toContain("YYYY-MM-DDTHH:MM:SSZ")
  })

  test("NOTES_TEMPLATE includes dedupe hint (F33)", () => {
    expect(NOTES_TEMPLATE).toContain("scan existing entries")
  })

  test("composeWriterPrompt renders Section budgets from CHECKPOINT_SECTION_BUDGETS (F43)", async () => {
    const { composeWriterPromptForTest } = await import("../../src/session/checkpoint")
    const out = composeWriterPromptForTest({
      checkpointFile: "/tmp/test/checkpoint.md",
      memoryFile: "/tmp/test/memory.md",
      taskMemDir: "/tmp/test/tasks",
      notesFile: "/tmp/test/notes.md",
      rangeDesc: "test range",
      progressDiff: "",
    })
    // Every section budget must be substituted into the prompt
    for (const [section, budget] of Object.entries(CHECKPOINT_SECTION_BUDGETS)) {
      expect(out).toContain(`${section}: ${budget}`)
    }
    // Placeholder must be substituted out
    expect(out).not.toContain("{{SECTION_BUDGETS}}")
  })
})


test("managed checkpoint writer keeps session continuity without semantic memory writes", async () => {
  const { composeWriterPromptForTest } = await import("../../src/session/checkpoint")
  const { assertMemoryWriteAllowed } = await import("../../src/tool/memory-path-guard")
  const { ProjectID } = await import("../../src/project/schema")
  const { SessionID } = await import("../../src/session/schema")
  const previous = process.env.OPEN_CLANK_MANAGED
  const input = { checkpointFile: "/data/memory/sessions/sid/checkpoint.md", memoryFile: "/data/memory/projects/p_test/MEMORY.md", taskMemDir: "/data/memory/sessions/sid/tasks", notesFile: "/data/memory/sessions/sid/notes.md", rangeDesc: "prior checkpoint", progressDiff: "" }
  const check = (target: string) => assertMemoryWriteAllowed({ target, agentName: "checkpoint-writer", memoryRoot: "/data/memory", projectID: ProjectID.make("p_test"), sessionID: SessionID.make("sid") })
  try {
    process.env.OPEN_CLANK_MANAGED = "1"
    const managed = composeWriterPromptForTest(input)
    expect(managed).toContain(input.checkpointFile)
    expect(managed).toContain(input.taskMemDir)
    expect(managed).not.toContain(input.memoryFile)
    expect(() => check(input.memoryFile)).toThrow()
    expect(() => check(input.checkpointFile)).not.toThrow()
    process.env.OPEN_CLANK_MANAGED = "0"
    expect(composeWriterPromptForTest(input)).toContain(input.memoryFile)
    expect(() => check(input.memoryFile)).not.toThrow()
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
  }
})
