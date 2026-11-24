export const meta = {
  name: "council",
  description: "Opt-in independent perspectives and one synthesis; no participant tools by default.",
  whenToUse: "Use only when a user explicitly requests a small council. Args: question, participants (1–5, default 3), optional models (provider/model per participant), timeoutMs (up to 120000) and deadlineMs (up to 900000).",
  phases: [{ title: "Independent views" }, { title: "Synthesis" }],
}
const input = typeof args === "string" ? { question: args } : args
if (!input || typeof input.question !== "string" || !input.question.trim() || input.question.length > 16000) throw new Error("question is required")
const count = input.participants === undefined ? 3 : input.participants
if (!Number.isInteger(count) || count < 1 || count > 5) throw new Error("participants must be 1–5")
const timeout = input.timeoutMs === undefined ? 120000 : input.timeoutMs
if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 120000) throw new Error("timeoutMs must be 1000–120000")
const models = input.models === undefined ? [] : input.models
if (!Array.isArray(models) || models.length > count || models.some(m => typeof m !== "string" || m.length > 256 || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.:/-]+$/.test(m))) throw new Error("models must be explicit provider/model references")
phase("Independent views")
const views = await parallel(Array.from({length: count}, (_, i) => () => agent(
  `Independently assess this question. State your reasoning, uncertainty and useful alternatives. Do not assume other views.\n\n${input.question}`,
  {label:`view-${i+1}`, phase:"Independent views", tools:[], timeoutMs:timeout, ...(models[i] ? {model:models[i]} : {})}
)))
const labeled = views.map((value, i) => ({label:`view-${i+1}`, status:value == null ? "unavailable" : "available", value:value ?? null}))
const available = labeled.filter(x => x.status === "available")
if (!available.length) return {status:"unavailable", views:labeled, synthesis:null, limits:{participants:count, timeoutMs:timeout, requestedDeadlineMs:input.deadlineMs ?? 300000}, attribution:"Use the workflow run inspector for actor IDs."}
phase("Synthesis")
const synthesis = await agent(`Synthesize these independent perspectives. Distinguish disagreement and gaps; do not claim consensus from missing views.\n\n${JSON.stringify(labeled)}`,
  {label:"synthesis", phase:"Synthesis", tools:[], timeoutMs:timeout})
return {status: available.length === count && synthesis != null ? "complete" : "partial", views:labeled, synthesis:synthesis ?? null,
  limits:{participants:count, timeoutMs:timeout, requestedDeadlineMs:input.deadlineMs ?? 300000}, attribution:"Use the workflow run inspector for actor IDs."}
