export const meta = {
  name: "advisor",
  description: "Opt-in independent, tool-free second opinion and synthesis.",
  whenToUse: "Use only when explicitly requested. Args: question, optional model (provider/model), timeoutMs (up to 120000) and deadlineMs (up to 900000).",
  phases: [{ title: "Independent view" }, { title: "Synthesis" }],
}
const input = typeof args === "string" ? { question: args } : args
if (!input || typeof input.question !== "string" || !input.question.trim() || input.question.length > 16000) throw new Error("question is required")
const timeout = input.timeoutMs === undefined ? 120000 : input.timeoutMs
if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 120000) throw new Error("timeoutMs must be 1000–120000")
if (input.model !== undefined && (typeof input.model !== "string" || input.model.length > 256 || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.:/-]+$/.test(input.model))) throw new Error("model must be an explicit provider/model reference")
phase("Independent view")
const view = await agent(`Give an independent second opinion. State uncertainty, risks and alternatives.\n\n${input.question}`,
  {label:"advisor", phase:"Independent view", tools:[], timeoutMs:timeout, ...(input.model ? {model:input.model} : {})})
if (view == null) return {status:"unavailable", views:[{label:"advisor", status:"unavailable", value:null}], synthesis:null, limits:{participants:1, timeoutMs:timeout, requestedDeadlineMs:input.deadlineMs ?? 300000}, attribution:"Use the workflow run inspector for actor IDs."}
phase("Synthesis")
const synthesis = await agent(`Summarize this second opinion, preserving its uncertainty and identifying any unresolved questions.\n\n${JSON.stringify(view)}`,
  {label:"synthesis", phase:"Synthesis", tools:[], timeoutMs:timeout})
return {status:synthesis == null ? "partial" : "complete", views:[{label:"advisor", status:"available", value:view}], synthesis:synthesis ?? null,
  limits:{participants:1, timeoutMs:timeout, requestedDeadlineMs:input.deadlineMs ?? 300000}, attribution:"Use the workflow run inspector for actor IDs."}
