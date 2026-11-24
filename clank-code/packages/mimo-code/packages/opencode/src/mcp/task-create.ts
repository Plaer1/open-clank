import { createHash } from "node:crypto"

/** Closed live, partial task read. Opaque cursor is only a selector. */
export function taskRead(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>, action = v.action ?? "list"
  if (action !== "list" && action !== "inspect") return
  if (Object.keys(v).some((key) => !(action === "list" ? ["action", "limit", "cursor"] : ["action", "task_id"]).includes(key))) return
  const task_id = action === "inspect" ? v.task_id : null
  const limit = action === "list" ? v.limit ?? 32 : 1
  const cursor = action === "list" ? v.cursor ?? null : null
  if ((task_id !== null && (typeof task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(task_id))) ||
      (action === "inspect" && !task_id) || !Number.isSafeInteger(limit) || (limit as number) < 1 || (limit as number) > 32 ||
      (cursor !== null && (typeof cursor !== "string" || !cursor || cursor.length > 8192))) return
  const args = { action: action as "list" | "inspect", task_id: task_id as string | null, limit: limit as number, cursor: cursor as string | null }
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.task-read.v1", action, task_id, limit, cursor]), "utf8").digest("hex")
  return { args, argsSHA256 }
}

/** Closed UTC calendar profiles; no account or scope input. */
export function taskCreate(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>
  const daily = v.schedule === "daily"
  const calendar = v.schedule === "weekly" || v.schedule === "monthly"
  const once = v.schedule === "once"
  if ((!daily && !calendar && !once) || Object.keys(v).sort().join(",") !== (once
        ? "action,name,prompt,schedule,scheduled_date,task_type" : daily
        ? "action,name,prompt,schedule,scheduled_time,task_type"
        : "action,name,prompt,schedule,scheduled_day,scheduled_time,task_type") ||
      v.action !== "create" || v.task_type !== "llm" ||
      (calendar && (!Number.isSafeInteger(v.scheduled_day) || (v.scheduled_day as number) < (v.schedule === "weekly" ? 0 : 1) ||
        (v.scheduled_day as number) > (v.schedule === "weekly" ? 6 : 31))) ||
      typeof v.name !== "string" || !v.name.trim() || Buffer.byteLength(v.name, "utf8") > 200 ||
      typeof v.prompt !== "string" || !v.prompt.trim() || Buffer.byteLength(v.prompt, "utf8") > 16384 ||
      (!once && (typeof v.scheduled_time !== "string" || !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(v.scheduled_time))) ||
      (once && (typeof v.scheduled_date !== "string" || !/^(?!0000)[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(v.scheduled_date) ||
        !Number.isFinite(Date.parse(v.scheduled_date)) || new Date(v.scheduled_date).toISOString() !== v.scheduled_date.replace(/Z$/, ".000Z"))) ||
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v.name + v.prompt)) return
  const base = { action: "create" as const, task_type: "llm" as const, name: v.name, prompt: v.prompt }
  const args = once ? { ...base, schedule: "once" as const, scheduled_date: v.scheduled_date as string } : daily
    ? { ...base, schedule: "daily" as const, scheduled_time: v.scheduled_time as string }
    : { ...base, schedule: v.schedule as "weekly" | "monthly", scheduled_time: v.scheduled_time as string, scheduled_day: v.scheduled_day as number }
  const material = once ? ["openclank.task-create.v3", base.name, base.prompt, "once", v.scheduled_date, base.task_type] : daily
    ? ["openclank.task-create.v1", base.name, base.prompt, "daily", v.scheduled_time, base.task_type]
    : ["openclank.task-create.v2", base.name, base.prompt, v.schedule, v.scheduled_time, base.task_type, v.scheduled_day]
  const argsSHA256 = createHash("sha256").update(JSON.stringify(material), "utf8").digest("hex")
  return { args, argsSHA256 }
}

/** Closed scheduling-only action; revision is an opaque inspect selector. */
export function taskControl(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>
  if (Object.keys(v).sort().join(",") !== "action,expected_revision,task_id" ||
      (v.action !== "pause" && v.action !== "resume" && v.action !== "delete") ||
      typeof v.task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(v.task_id) ||
      typeof v.expected_revision !== "string" || !/^[A-Za-z0-9_-]{32,64}$/.test(v.expected_revision)) return
  const args = { action: v.action as "pause" | "resume" | "delete", task_id: v.task_id, expected_revision: v.expected_revision }
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.task-control.v1", v.action, v.task_id, v.expected_revision]), "utf8").digest("hex")
  return { args, argsSHA256 }
}

/** One selected managed occurrence; the inspect token is only a selector. */
export function taskRun(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>
  if (Object.keys(v).sort().join(",") !== "action,expected_revision,task_id" || v.action !== "run" ||
      typeof v.task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(v.task_id) ||
      typeof v.expected_revision !== "string" || !/^[A-Za-z0-9_-]{32,64}$/.test(v.expected_revision)) return
  const args = { action: "run" as const, task_id: v.task_id, expected_revision: v.expected_revision }
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.task-run.v1", v.task_id, v.expected_revision]), "utf8").digest("hex")
  return { args, argsSHA256 }
}

/** Closed, exact name/prompt edit of an inspected LLM task. */
export function taskEdit(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>
  if (Object.keys(v).some((key) => !["action","task_id","expected_revision","name","prompt"].includes(key)) ||
      v.action !== "edit" || typeof v.task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(v.task_id) ||
      typeof v.expected_revision !== "string" || !/^[A-Za-z0-9_-]{32,64}$/.test(v.expected_revision) ||
      (!("name" in v) && !("prompt" in v))) return
  for (const [key, cap] of [["name",200],["prompt",16384]] as const) {
    if (!(key in v)) continue
    const value = v[key]
    if (typeof value !== "string" || !value.trim() || Buffer.byteLength(value,"utf8") > cap ||
        /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)) return
  }
  const args = {action:"edit" as const,task_id:v.task_id,expected_revision:v.expected_revision,
    ...("name" in v ? {name:v.name as string} : {}), ...("prompt" in v ? {prompt:v.prompt as string} : {})}
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.task-edit.v1",v.task_id,v.expected_revision,v.name ?? null,v.prompt ?? null]),"utf8").digest("hex")
  return {args,argsSHA256}
}
