import { createHash } from "node:crypto"

/** Normalize the closed managed shared-text request without accepting identity or paths. */
export function sharedText(input: unknown): { argsJSON: string; argsSHA256: string } | undefined {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>
  const validString = (x: unknown, max: number, empty = false): x is string => {
    if (typeof x !== "string" || (!empty && !x.length) || Buffer.byteLength(x, "utf8") > max) return false
    for (let i = 0; i < x.length; i++) {
      const c = x.charCodeAt(i)
      if (c >= 0xd800 && c <= 0xdbff) { const d = x.charCodeAt(++i); if (!(d >= 0xdc00 && d <= 0xdfff)) return false }
      else if (c >= 0xdc00 && c <= 0xdfff) return false
    }
    return true
  }
  const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).sort().join(",") === keys.sort().join(",")
  const digest = (x: unknown) => typeof x === "string" && /^[a-f0-9]{64}$/.test(x)
  if (!validString(v.publication_id, 128)) return
  if (v.format === "workbook") {
    let workbook: Record<string, unknown>
    if (v.action === "read" && exact(v, ["format", "action", "publication_id"])) {
      workbook = { format: "workbook", action: v.action, publication_id: v.publication_id }
    } else if (v.action === "status" && exact(v, ["format", "action", "publication_id", "operation_id", "client_digest"]) && validString(v.operation_id, 256) && digest(v.client_digest)) {
      workbook = { format: "workbook", action: v.action, publication_id: v.publication_id, operation_id: v.operation_id, client_digest: v.client_digest }
    } else if (v.action === "commit" && exact(v, ["format", "action", "publication_id", "base_text", "base_sha256", "cells"]) && validString(v.base_text, 65536, true) && digest(v.base_sha256) && Array.isArray(v.cells) && v.cells.length >= 1 && v.cells.length <= 64) {
      if (createHash("sha256").update(v.base_text, "utf8").digest("hex") !== v.base_sha256) return
      const seen = new Set<string>(), cells: { sheet_id: string; cell_id: string; raw: string }[] = []
      let bytes = 0
      for (const item of v.cells) {
        if (!item || typeof item !== "object" || Array.isArray(item)) return
        const c = item as Record<string, unknown>
        if (!exact(c, ["sheet_id", "cell_id", "raw"]) || typeof c.sheet_id !== "string" || typeof c.cell_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(c.sheet_id) || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(c.cell_id) || !validString(c.raw, 32768, true) || c.raw.length > 65536) return
        const key = JSON.stringify([c.sheet_id, c.cell_id]); if (seen.has(key)) return
        seen.add(key); bytes += Buffer.byteLength(JSON.stringify(c.raw), "utf8"); if (bytes > 32768) return
        cells.push({ sheet_id: c.sheet_id, cell_id: c.cell_id, raw: c.raw })
      }
      workbook = { format: "workbook", action: v.action, publication_id: v.publication_id, base_text: v.base_text, base_sha256: v.base_sha256, cells }
    } else return
    const argsJSON = JSON.stringify(["openclank.shared-workbook.v1", workbook])
    if (Buffer.byteLength(argsJSON, "utf8") > 131072) return
    return { argsJSON, argsSHA256: createHash("sha256").update(argsJSON, "utf8").digest("hex") }
  }
  if (v.format === "slides") {
    let slides: Record<string, unknown>
    if (v.action === "read" && exact(v, ["format", "action", "publication_id"])) {
      slides = { format: "slides", action: v.action, publication_id: v.publication_id }
    } else if (v.action === "status" && exact(v, ["format", "action", "publication_id", "operation_id", "client_digest"]) && validString(v.operation_id, 256) && digest(v.client_digest)) {
      slides = { format: "slides", action: v.action, publication_id: v.publication_id, operation_id: v.operation_id, client_digest: v.client_digest }
    } else if (v.action === "commit" && exact(v, ["format", "action", "publication_id", "base_text", "base_sha256", "objects"]) && validString(v.base_text, 65536, true) && digest(v.base_sha256) && Array.isArray(v.objects) && v.objects.length >= 1 && v.objects.length <= 64) {
      if (createHash("sha256").update(v.base_text, "utf8").digest("hex") !== v.base_sha256) return
      const seen = new Set<string>(), objects: { slide_id: string; object_id: string; text: string }[] = []
      let bytes = 0
      for (const item of v.objects) {
        if (!item || typeof item !== "object" || Array.isArray(item)) return
        const c = item as Record<string, unknown>
        if (!exact(c, ["slide_id", "object_id", "text"]) || typeof c.slide_id !== "string" || typeof c.object_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(c.slide_id) || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(c.object_id) || !validString(c.text, 32768, true) || c.text.length > 65536) return
        const key = JSON.stringify([c.slide_id, c.object_id]); if (seen.has(key)) return
        seen.add(key); bytes += Buffer.byteLength(JSON.stringify(c.text), "utf8"); if (bytes > 32768) return
        objects.push({ slide_id: c.slide_id, object_id: c.object_id, text: c.text })
      }
      slides = { format: "slides", action: v.action, publication_id: v.publication_id, base_text: v.base_text, base_sha256: v.base_sha256, objects }
    } else return
    const argsJSON = JSON.stringify(["openclank.shared-slides.v1", slides])
    if (Buffer.byteLength(argsJSON, "utf8") > 131072) return
    return { argsJSON, argsSHA256: createHash("sha256").update(argsJSON, "utf8").digest("hex") }
  }
  let args: Record<string, unknown>
  if (v.action === "read" && exact(v, ["action", "publication_id"])) {
    args = { action: v.action, publication_id: v.publication_id }
  } else if (v.action === "status" && exact(v, ["action", "publication_id", "operation_id", "client_digest"]) && validString(v.operation_id, 256) && digest(v.client_digest)) {
    args = { action: v.action, publication_id: v.publication_id, operation_id: v.operation_id, client_digest: v.client_digest }
  } else if (v.action === "commit" && exact(v, ["action", "publication_id", "base_text", "base_sha256", "edits"]) && validString(v.base_text, 65536, true) && digest(v.base_sha256) && Array.isArray(v.edits) && v.edits.length >= 1 && v.edits.length <= 64) {
    const base = Buffer.from(v.base_text, "utf8")
    if (createHash("sha256").update(base).digest("hex") !== v.base_sha256) return
    let previous = 0, inserted = 0
    const edits: { start: number; end: number; text: string }[] = []
    for (const item of v.edits) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return
      const e = item as Record<string, unknown>
      if (!exact(e, ["start", "end", "text"]) || !Number.isSafeInteger(e.start) || !Number.isSafeInteger(e.end) || typeof e.start !== "number" || typeof e.end !== "number" || e.start < previous || e.end < e.start || e.end > base.length || !validString(e.text, 32768, true)) return
      if ((e.start < base.length && (base[e.start]! & 0xc0) === 0x80) || (e.end < base.length && (base[e.end]! & 0xc0) === 0x80)) return
      inserted += Buffer.byteLength(e.text, "utf8")
      if (inserted > 32768) return
      previous = e.end
      edits.push({ start: e.start, end: e.end, text: e.text })
    }
    args = { action: v.action, publication_id: v.publication_id, base_text: v.base_text, base_sha256: v.base_sha256, edits }
  } else return
  const argsJSON = JSON.stringify(["openclank.shared-text.v1", args])
  if (Buffer.byteLength(argsJSON, "utf8") > 131072) return
  return { argsJSON, argsSHA256: createHash("sha256").update(argsJSON, "utf8").digest("hex") }
}
