import { createHash } from "node:crypto"

/** Exact closed subset; proof is recomputed from the real running part. */
export function filesMutate(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value = input as Record<string, unknown>
  if (value.action !== "move" && value.action !== "delete") return
  const keys = value.action === "move" ? ["action", "path", "destination", "expected_fingerprint"] : ["action", "path", "expected_fingerprint"]
  if (Object.keys(value).some((key) => !keys.includes(key))) return
  const validPath = (x: unknown): x is string => typeof x === "string" && !!x && x === x.trim() &&
    Buffer.byteLength(x, "utf8") <= 4096 && !x.includes("\0") &&
    !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(x)
  if (!validPath(value.path) || (value.action === "move" && !validPath(value.destination))) return
  const expected = value.expected_fingerprint === undefined ? null : value.expected_fingerprint
  if (expected !== null && (typeof expected !== "string" || !expected || expected.length > 512 ||
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(expected))) return
  const action = value.action === "delete" ? "trash" as const : "move" as const
  const path = value.path
  const destination = action === "move" ? value.destination as string : null
  const args = action === "move" ? { action, path, destination: destination as string, expectedFingerprint: expected as string | null }
    : { action, path, destination: null, expectedFingerprint: expected as string | null }
  const argsSHA256 = createHash("sha256").update(JSON.stringify(["openclank.file-mutation.v1", action, path, destination, expected]), "utf8").digest("hex")
  return { args, argsSHA256 }
}

/** Select only the original private deferred Files call; it is not authority. */
export function deferredFilesMutate(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value = input as Record<string, unknown>
  if (Object.keys(value).length !== 2 || typeof value.name !== "string" ||
      !/^lifetools_[a-f0-9]+_manage_files$/.test(value.name) || !("arguments" in value)) return
  const normalized = filesMutate(value.arguments)
  return normalized ? { toolName: value.name, ...normalized } : undefined
}

/** Closed ordinary text replacement; the actual registered private descriptor selects the tool. */
export function ordinaryFilesMutate(input: unknown, tool: "write_file" | "edit_file") {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value = input as Record<string, unknown>
  const keys = tool === "write_file" ? ["path", "content", "expected_fingerprint"]
    : ["path", "old_string", "new_string", "replace_all", "expected_fingerprint"]
  if (Object.keys(value).some((key) => !keys.includes(key))) return
  const scalar = (x: unknown): x is string => typeof x === "string" &&
    !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(x)
  const path = value.path
  if (!scalar(path) || !path || path !== path.trim() || path.includes("\0") || Buffer.byteLength(path,"utf8") > 4096) return
  const expected = value.expected_fingerprint === undefined ? null : value.expected_fingerprint
  if (expected !== null && (!scalar(expected) || !expected || expected.length > 512 || expected.includes("\0"))) return
  const first = tool === "write_file" ? value.content : value.old_string
  const second = tool === "write_file" ? null : value.new_string
  const replaceAll = tool === "write_file" ? null : value.replace_all === undefined ? false : value.replace_all
  if (!scalar(first)) return
  if (tool === "edit_file" && (!scalar(second) || typeof replaceAll !== "boolean" || !first || first === second)) return
  const action = tool === "write_file" ? "write" as const : "edit" as const
  const material = ["openclank.file-mutation.v2",action,path,first,second,replaceAll,expected]
  const encoded = JSON.stringify(material)
  if (Buffer.byteLength(encoded,"utf8") > 131072) return
  const args = action === "write" ? {action,path,content:first,expectedFingerprint:expected as string | null}
    : {action,path,oldText:first,newText:second as string,replaceAll:replaceAll as boolean,expectedFingerprint:expected as string | null}
  return {args,argsSHA256:createHash("sha256").update(encoded,"utf8").digest("hex")}
}

/** A wrapper selector is matched against a currently registered private MCP descriptor elsewhere. */
export function deferredOrdinaryFilesMutate(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const value = input as Record<string, unknown>
  if (Object.keys(value).length !== 2 || typeof value.name !== "string" || !("arguments" in value)) return
  const matched = /^lifetools_[a-f0-9]+_(write_file|edit_file)$/.exec(value.name)
  if (!matched) return
  const normalized = ordinaryFilesMutate(value.arguments, matched[1] as "write_file" | "edit_file")
  return normalized ? {toolName:value.name,...normalized} : undefined
}
