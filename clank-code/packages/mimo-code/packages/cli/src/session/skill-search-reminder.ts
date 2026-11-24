import { Flag } from "../flag/flag"
// Keep the original model restriction local to this reminder; catalog filtering
// remains owned by the current skill registry.
function isSkillSearchDisabled(model: {id?:string;modelID?:string;api?:{id?:string};name?:string;family?:string}) {
  return [model.id,model.modelID,model.api?.id,model.name,model.family]
    .filter((value) => value !== undefined)
    .some((value) => /(^|[^a-z0-9])(claude|gpt)($|[^a-z0-9])/i.test(value))
}

export const SKILL_SEARCH_REMINDER_MARKER = "Skill search trigger:"

const SKILL_QUERY_GUIDANCE = [
  "Describe the required capability and final deliverable; treat file types such as CSV as inputs, not automatically as the target skill.",
  "Office examples:",
  "- Excel/CSV: analyze or clean data, work with formulas and tables, or create charts and workbooks.",
  "- PowerPoint/PPT: create, revise, or review a presentation or executive deck.",
  "- Word/DOCX: draft, edit, review, or format a document or report.",
  "- PDF: extract or review content, generate a PDF, or preserve and inspect page layout.",
  'Code examples: code review, debugging, test generation, security or performance analysis, frontend design, and other specialized engineering workflows.',
  'For example, "analyze gender_submission.csv" should express data analysis as the capability, the CSV dataset as input, and findings as output.',
  "These are examples of likely specialized workflows, not requirements to call a tool.",
]

type ReminderMessage = {
  info: { role: string; id: string; parentID?: string; time: { created: number } }
  parts: { type: string; text?: string; synthetic?: boolean }[]
}

/** Canonical v1 constructor: producer eligibility remains owned by the caller. */
export function buildSkillSearchReminderV1(kind: "first" | "refresh") {
  if (kind === "first") {
    return [
      "<system-reminder>",
      "Skill search trigger: consider the current request for a specialized skill.",
      "You should call skill_search before starting the task.",
      "First rewrite the request as a Skill Query containing action, input, output, and audience when available.",
      ...SKILL_QUERY_GUIDANCE,
      "</system-reminder>",
    ].join("\n")
  }
  return [
    "<system-reminder>",
    "Skill search trigger: reassess the current request for a specialized skill.",
    "By default: call skill_search before acting, unless the user explicitly references the current task or current artifact.",
    "When searching, rewrite the request as a Skill Query containing action, input, output, and audience when available.",
    ...SKILL_QUERY_GUIDANCE,
    "</system-reminder>",
  ].join("\n")
}

export function skillSearchReminder(input: { currentUserAt: number; previousUserAt?: number }) {
  if (input.previousUserAt === undefined) return buildSkillSearchReminderV1("first")
  if (input.currentUserAt - input.previousUserAt < Flag.OPENCLANK_SKILL_SEARCH_REFRESH_INTERVAL_MS) return
  return buildSkillSearchReminderV1("refresh")
}

export function skillSearchReminderForMessages(messages: ReminderMessage[]) {
  const users = messages.filter(
    (message) => message.info.role === "user" && message.parts.some((part) => !part.synthetic),
  )
  const current = users.at(-1)
  if (!current) return
  if (
    current.parts.some(
      (part) => part.type === "text" && part.synthetic && part.text?.includes(SKILL_SEARCH_REMINDER_MARKER),
    )
  )
    return
  if (messages.some((message) => message.info.role === "assistant" && message.info.parentID === current.info.id)) return
  return skillSearchReminder({
    currentUserAt: current.info.time.created,
    previousUserAt: users.at(-2)?.info.time.created,
  })
}

export function skillSearchReminderForSession(input: {
  session: { parentID?: string }
  agent: { name: string; mode: "subagent" | "primary" | "all" }
  model: { id: string; name?: string; family?: string; api: { id: string } }
  messages: ReminderMessage[]
}) {
  if (
    !Flag.OPENCLANK_ENABLE_SKILL_SEARCH_REMINDER ||
    input.session.parentID ||
    input.agent.mode === "subagent" ||
    input.agent.name === "compose" ||
    isSkillSearchDisabled(input.model)
  )
    return
  return skillSearchReminderForMessages(input.messages)
}
