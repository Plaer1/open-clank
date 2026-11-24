export const meta = {
  name: "expert",
  description: "Opt-in workflow for a focused expert draft. Choose clarification, analysis, design, execution draft and critique.",
  whenToUse: "Use when the user requests Expert. Supply args.task and optionally args.phases (clarify, analysis, design, execute, critique), args.model, args.timeoutMs or args.deadlineMs.",
  phases: [
    { title: "Clarification", detail: "Identify requirements and ask for missing decisions." },
    { title: "Analysis", detail: "Use the task and any completed clarification." },
    { title: "Design", detail: "Design a bounded approach from available prior phases." },
    { title: "Execution draft", detail: "Produce the agreed deliverable in the result, with no external writes." },
    { title: "Critique", detail: "Review the available draft or planning evidence for errors and gaps." },
  ],
}
const input = typeof args === "string" ? { task: args } : args
if (!input || typeof input.task !== "string" || !input.task.trim() || input.task.length > 12000) throw new Error("Expert requires a bounded task")
if (typeof input.model !== "string" || input.model.length > 256 || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.:/-]+$/.test(input.model))
  throw new Error("The original workflow model is unavailable")

const phases = [
  { id: "clarify", title: "Clarification", instruction: "Summarize the task and its constraints. List only unresolved questions that prevent useful work." },
  { id: "analysis", title: "Analysis", instruction: "Analyze the task from supplied evidence. Identify unknowns, dependencies and incorrect assumptions." },
  { id: "design", title: "Design", instruction: "Develop a bounded approach with concrete decisions and acceptance criteria." },
  { id: "execute", title: "Execution draft", instruction: "Produce the requested draft or a proposed patch as text. An external change requires a separate approved action. State any missing inputs explicitly." },
  { id: "critique", title: "Critique", instruction: "Review the available draft or analysis against the request. Report actionable discrepancies and remaining uncertainty." },
]
const selected = input.phases === undefined ? phases.map(step => step.id) : input.phases
if (!Array.isArray(selected) || selected.length < 1 || selected.length > phases.length
    || new Set(selected).size !== selected.length || selected.some(id => !phases.some(step => step.id === id)))
  throw new Error("Expert phases must be a nonempty selection of the named phases")

const timeout = input.timeoutMs === undefined ? 45000 : input.timeoutMs
if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 120000) throw new Error("Expert timeoutMs must be 1000–120000")

const schema = {
  type: "object",
  additionalProperties: false,
  required: ["result", "questions"],
  properties: {
    result: { type: "string", maxLength: 4000 },
    questions: { type: "array", maxItems: 6, items: { type: "string", maxLength: 350 } },
  },
}
const results = []
let blocked = false
for (const step of phases) {
  if (!selected.includes(step.id) || blocked) {
    results.push({ id: step.id, title: step.title, state: blocked && selected.includes(step.id) ? "pending" : "skipped" })
    continue
  }
  phase(step.title)
  const inputs = results.filter(value => value.state === "confirmed").map(value => ({ phase: value.id, result: value.result.slice(0, 1400) }))
  const response = await agent([
    step.instruction,
    "Use only the supplied context and honor the user's scope, repository guidance and execution limits.",
    "Return a result and any blocking questions. Claim only work actually included in the response.",
    "User task", input.task,
    "Available results from earlier phases", JSON.stringify(inputs),
  ].join("\n\n"), { label: step.id, phase: step.title, schema, tools: [], timeoutMs: timeout, model: input.model })
  if (!response || typeof response.result !== "string" || !Array.isArray(response.questions)) {
    results.push({ id: step.id, title: step.title, state: "unavailable", inputs: inputs.map(value => value.phase) })
    log(step.title + " · no confirmed return")
    blocked = true
    continue
  }
  const questions = response.questions.filter(value => typeof value === "string" && value.trim()).slice(0, 6)
  blocked = questions.length > 0
  results.push({ id: step.id, title: step.title, state: blocked ? "waiting" : "confirmed", result: response.result.slice(0, 4000),
    questions, inputs: inputs.map(value => value.phase) })
}
return {
  status: results.some(step => step.state === "waiting") ? "awaiting-input" : blocked ? "partial" : "complete",
  phases: results,
  scope: "Draft output in this workflow. Open the run inspector for recorded actor and phase details.",
}
