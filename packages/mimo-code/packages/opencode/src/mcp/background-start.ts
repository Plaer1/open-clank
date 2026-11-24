import { createHash } from "node:crypto"

/** Closed START shape shared by the live wrapper and native part reader. */
export function backgroundStart(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value = input as Record<string, unknown>
  if (Object.keys(value).some((key) => !["action", "command", "max_runtime_s"].includes(key)) ||
      value.action !== "start" || typeof value.command !== "string") return
  const command = value.command.trim()
  const runtime = value.max_runtime_s === undefined ? 3600 : value.max_runtime_s
  if (!command || Buffer.byteLength(command, "utf8") > 65536 || !Number.isSafeInteger(runtime) ||
      (runtime as number) < 1 || (runtime as number) > 86400 || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(command)) return
  const args = { action: "start" as const, command, max_runtime_s: runtime as number }
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.bg-start.v1", command, runtime]), "utf8").digest("hex")
  return { args, argsSHA256 }
}

export function backgroundRead(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value=input as Record<string, unknown>, action=value.action
  if (typeof action!=="string" || !["list","poll","tail","wait"].includes(action)) return
  const allowed=action==="list" ? ["action","limit","cursor"] : action==="poll" ? ["action","job_id"] :
    action==="tail" ? ["action","job_id","cursor","limit"] : ["action","job_id","timeout_s"]
  if (Object.keys(value).some((key)=>!allowed.includes(key)) || (action==="list" && value.cursor!==undefined && value.cursor!==0)) return
  const job_id=action==="list" ? null : value.job_id
  if (job_id!==null && (typeof job_id!=="string" || !/^[A-Za-z0-9_-]{1,128}$/.test(job_id))) return
  const cursor=action==="tail" ? value.cursor ?? 0 : 0
  const limit=action==="tail" ? value.limit ?? 16384 : action==="list" ? value.limit ?? 32 : 32
  const timeout_s=action==="wait" ? value.timeout_s ?? 30 : 0
  if (!Number.isSafeInteger(cursor) || (cursor as number)<0 || !Number.isSafeInteger(limit) || (limit as number)<1 ||
      (limit as number)>(action==="tail" ? 65536 : 32) || !Number.isSafeInteger(timeout_s) ||
      (timeout_s as number)<0 || (timeout_s as number)>30) return
  const args={action:action as "list"|"poll"|"tail"|"wait",job_id:job_id as string|null,
    cursor:cursor as number,limit:limit as number,timeout_s:timeout_s as number}
  const argsSHA256=createHash("sha256").update(JSON.stringify(["openclank.bg-read.v1",action,job_id,cursor,limit,timeout_s]),"utf8").digest("hex")
  return {args,argsSHA256}
}

export function backgroundWrite(input: unknown) {
  if (!input || typeof input!=="object" || Array.isArray(input)) return
  const v=input as Record<string,unknown>
  if (Object.keys(v).sort().join(",")!=="action,data,job_id" || v.action!=="write" || typeof v.job_id!=="string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(v.job_id) || typeof v.data!=="string" || !v.data || Buffer.byteLength(v.data,"utf8")>16384 ||
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v.data)) return
  const bytes=Buffer.from(v.data,"utf8"), hash=createHash("sha256").update(bytes).digest("hex")
  const args={action:"write" as const,job_id:v.job_id,data:v.data}
  const argsSHA256=createHash("sha256").update(JSON.stringify(["openclank.bg-write.v1",v.job_id,hash,bytes.length]),"utf8").digest("hex")
  return {args,argsSHA256}
}

export function backgroundTerminate(input: unknown) {
  if (!input || typeof input!=="object" || Array.isArray(input)) return
  const value=input as Record<string,unknown>
  if (Object.keys(value).length!==2 || value.action!=="terminate" || typeof value.job_id!=="string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(value.job_id)) return
  const args={action:"terminate" as const,job_id:value.job_id}
  const argsSHA256=createHash("sha256").update(JSON.stringify(["openclank.bg-terminate.v1",value.job_id]),"utf8").digest("hex")
  return {args,argsSHA256}
}

export function backgroundRetire(input: unknown) {
  if (!input || typeof input!=="object" || Array.isArray(input)) return
  const value=input as Record<string,unknown>
  if (Object.keys(value).length!==2 || value.action!=="retire" || typeof value.job_id!=="string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(value.job_id)) return
  return {args:{action:"retire" as const,job_id:value.job_id},
    argsSHA256:createHash("sha256").update(JSON.stringify(["openclank.bg-retire.v1",value.job_id]),"utf8").digest("hex")}
}
