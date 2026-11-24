import { createHash } from "node:crypto"

/** Closed existing-tool one-frame action; the selectors grant no authority. */
export function frameCapture(input: unknown) {
  if (!input || typeof input!=="object" || Array.isArray(input)) return
  const v=input as Record<string,unknown>
  if (Object.keys(v).sort().join(",")!=="action,destination_ref,name,target" || v.action!=="frame" || v.target!=="portal" ||
      typeof v.destination_ref!=="string" || !v.destination_ref || Buffer.byteLength(v.destination_ref,"utf8")>8192 ||
      typeof v.name!=="string" || !v.name || Buffer.byteLength(v.name,"utf8")>240 || !v.name.toLowerCase().endsWith(".png") ||
      v.name.trim()!==v.name || /[\/\\\x00-\x1f\x7f]/.test(v.name) ||
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v.name+v.destination_ref)) return
  const args={action:"frame" as const,target:"portal" as const,destination_ref:v.destination_ref,name:v.name}
  const argsJSON=JSON.stringify(["openclank.actor-frame.v1",args])
  return {argsJSON,argsSHA256:createHash("sha256").update(argsJSON,"utf8").digest("hex")}
}
