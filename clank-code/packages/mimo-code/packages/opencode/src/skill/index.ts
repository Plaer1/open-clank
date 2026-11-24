import os from "os"
import path from "path"
import { createHash } from "crypto"
import { open, stat } from "node:fs/promises"
import { pathToFileURL } from "url"
import z from "zod"
import { Effect, Layer, Context } from "effect"
import { NamedError } from "@mimo-ai/shared/util/error"
import type { Agent } from "@/agent/agent"
import { Bus } from "@/bus"
import { InstanceState } from "@/effect"
import { Flag } from "@/flag/flag"
import { Global } from "@/global"
import { Permission } from "@/permission"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Config } from "../config"
import { ConfigMarkdown } from "../config"
import { Glob } from "@mimo-ai/shared/util/glob"
import { Log } from "../util"
import { Discovery } from "./discovery"
import { extractComposeBundle } from "./compose/extract"
import { extractBuiltinBundle, OFFICIAL_SKILL_NAMES } from "./builtin/extract"
import { callBoundOpenClankTool } from "@/memory/mcp-client"
import type { ActorExecutionIdentity } from "@/actor/turn"

const log = Log.create({ service: "skill" })
const EXTERNAL_DIRS = [".claude", ".agents", ".codex", ".opencode"]
const EXTERNAL_SKILL_PATTERN = "skills/**/SKILL.md"
const MIMOCODE_SKILL_PATTERN = "{skill,skills}/**/SKILL.md"
const SKILL_PATTERN = "**/SKILL.md"
const BUILTIN_SKILL_PATTERN = "skills/*/SKILL.md"

export const Info = z.object({
  name: z.string(),
  description: z.string(),
  aliases: z.array(z.string()).optional(),
  location: z.string(),
  content: z.string(),
  sourceFileHash: z.string().optional(),
  sourceFileDevice: z.number().optional(),
  sourceFileInode: z.number().optional(),
  metadataClaim: z.boolean().optional(),
  metadataReceiptSHA256: z.string().optional(),
  metadataSnapshotSHA256: z.string().optional(),
  domainRefs: z.array(z.object({ block_id: z.string(), revision: z.number().int().positive() })).max(64).optional(),
  hidden: z.boolean().optional(),
  bundled: z.boolean().optional(),
  skillID: z.string().optional(),
  revision: z.number().int().positive().optional(),
  contentHash: z.string().optional(),
  owner: z.string().optional(),
  status: z.string().optional(),
  source: z.string().optional(),
  sourceStatus: z.string().optional(),
  sourceURI: z.string().optional(),
  sourceRevision: z.string().optional(),
  platforms: z.array(z.string()).optional(),
  requiresToolsets: z.array(z.string()).optional(),
  trust: z.string().optional(),
  lastAudit: z.number().optional(),
  /** Upstream disable-model-invocation: user-explicit only; never model-discovered. */
  disableModelInvocation: z.boolean().optional(),
})
export type Info = z.infer<typeof Info>

/** How a skill is being invoked. */
export type InvocationMode = "model" | "explicit"

export type InvocationDenial =
  | "not_found"
  | "disabled"
  | "untrusted"
  | "revoked"
  | "staged"
  | "hidden_from_model"
  | "model_invocation_disabled"

export type InvocationResult =
  | { ok: true; info: Info }
  | { ok: false; reason: InvocationDenial; name: string }

/**
 * Disabled, untrusted, revoked, or staged skills never run in any mode.
 * Hidden alone is not a denial: trusted+enabled hidden skills stay available
 * to user-explicit invocation while remaining absent from model discovery.
 */
export function isRunnableSkill(skill: Info): boolean {
  const trust = (skill.trust ?? "").toLowerCase()
  const status = (skill.status ?? "").toLowerCase()
  if (trust === "untrusted" || trust === "revoked") return false
  if (status === "revoked" || status === "disabled" || status === "staged" || status === "draft") return false
  return true
}

/** Positive trust is required to invoke a hidden skill; hidden alone is never trust. */
export function isTrustedForHiddenInvocation(skill: Info): boolean {
  const trust = (skill.trust ?? "").toLowerCase()
  return trust === "verified" || trust === "waived" || trust === "published"
}

export function isModelInvocable(skill: Info): boolean {
  if (!isRunnableSkill(skill)) return false
  if (skill.hidden) return false
  if (skill.disableModelInvocation) return false
  return true
}

/** User-explicit invocable: runnable skills, including trusted hidden and model-disabled. */
export function isExplicitlyInvocable(skill: Info): boolean {
  return evaluateInvocation(skill, "explicit").ok
}

const MENTION_RE = /(?:^|\s)\/([A-Za-z][A-Za-z0-9_:-]*)(?=[^A-Za-z0-9_:-]|$)/g

/**
 * Slash/mention tokens the user themselves typed on the current turn.
 * Synthetic parts (injected skill bodies, reminders) are not user-originated.
 */
export function userSlashMentions(
  messages: ReadonlyArray<{
    info: { role: string }
    parts: ReadonlyArray<{ type: string; text?: string; synthetic?: boolean }>
  }>,
): string[] {
  const lastUser = messages.findLast((msg) => msg.info.role === "user")
  if (!lastUser) return []
  const text = lastUser.parts
    .flatMap((p) => (p.type === "text" && !p.synthetic ? [p.text ?? ""] : []))
    .join("\n")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`\n]*`/g, " ")
  return [...text.matchAll(MENTION_RE)]
    .map((m) => m[1])
    .filter((name): name is string => Boolean(name))
}

/**
 * True only when a real user-originated signal for THIS turn names the skill:
 * a user slash/mention of that skill, or a host-issued user-explicit token
 * from the request envelope. A model tool parameter alone never counts.
 */
export function isUserExplicitRequest(input: {
  name: string
  messages: Parameters<typeof userSlashMentions>[0]
  hostTokens?: unknown
}): boolean {
  if (Array.isArray(input.hostTokens) && input.hostTokens.some((token) => token === input.name)) return true
  return userSlashMentions(input.messages).includes(input.name)
}

/**
 * Gate a single skill for the given invocation mode.
 * Model mode: only non-hidden, model-invocable, runnable skills.
 * Explicit mode: runnable skills, including hidden ones when positively trusted.
 * Disabled, untrusted, revoked, and staged skills fail both modes.
 */
export function evaluateInvocation(skill: Info | undefined, mode: InvocationMode): InvocationResult {
  if (!skill) return { ok: false, reason: "not_found", name: "" }
  const trust = (skill.trust ?? "").toLowerCase()
  const status = (skill.status ?? "").toLowerCase()
  if (trust === "revoked" || status === "revoked") return { ok: false, reason: "revoked", name: skill.name }
  if (status === "disabled") return { ok: false, reason: "disabled", name: skill.name }
  if (trust === "untrusted") return { ok: false, reason: "untrusted", name: skill.name }
  if (status === "staged" || status === "draft") return { ok: false, reason: "staged", name: skill.name }
  if (mode === "model") {
    if (skill.hidden) return { ok: false, reason: "hidden_from_model", name: skill.name }
    if (skill.disableModelInvocation) return { ok: false, reason: "model_invocation_disabled", name: skill.name }
  } else if (skill.hidden && !isTrustedForHiddenInvocation(skill)) {
    // Hidden flag alone never grants trust.
    return { ok: false, reason: "untrusted", name: skill.name }
  }
  return { ok: true, info: skill }
}

/** Parse a bounded published-snapshot claim. Provenance here is not authority. */
export function parseResourceMetadataClaim(encoded: unknown): { metadata: {name?:string, aliases?:string[], domain_refs?:Array<{block_id:string,revision:number}>, types?:string[], processing_role?:string} } | undefined {
  if (typeof encoded !== "string" || !/^[A-Za-z0-9_-]{1,21846}$/.test(encoded)) return undefined
  try {
    const bytes=Buffer.from(encoded,"base64url"); if(bytes.length>16384 || bytes.toString("base64url")!==encoded) return undefined
    const raw=bytes.toString("utf8"); if(!Buffer.from(raw,"utf8").equals(bytes)) return undefined
    const value=JSON.parse(raw)
    const object=(v:unknown):v is Record<string,unknown> => !!v && typeof v==="object" && !Array.isArray(v)
    const exact=(v:Record<string,unknown>, keys:string[]) => Object.keys(v).sort().join("\0")===keys.sort().join("\0")
    const item=(v:unknown):v is string => typeof v==="string" && !!v && v===v.trim() && Buffer.byteLength(v,"utf8")<=512 && Buffer.from(v,"utf8").toString("utf8")===v
    if(!object(value)||!exact(value,["version","metadata","provenance"])||value.version!==1||!object(value.metadata)||!object(value.provenance)) return undefined
    const metadata=value.metadata, provenance=value.provenance
    if(!exact(provenance,["origin","actor"]) || !["manual","inferred","inherited","legacy_unknown"].includes(String(provenance.origin)) || !item(provenance.actor)) return undefined
    if(Object.keys(metadata).some((key)=>!["name","aliases","domain_refs","types","processing_role"].includes(key))) return undefined
    for(const key of ["name","processing_role"]) if(key in metadata && !item(metadata[key])) return undefined
    for(const key of ["aliases","types"]) if(key in metadata) {
      const values=metadata[key]; if(!Array.isArray(values)||values.length>64||values.some((v)=>!item(v))||new Set(values).size!==values.length)return undefined
    }
    if("domain_refs" in metadata) {
      const refs=metadata.domain_refs; if(!Array.isArray(refs)||refs.length>64)return undefined
      const seen=new Set<string>()
      for(const ref of refs) {if(!object(ref)||!exact(ref,["block_id","revision"])||!item(ref.block_id)||!Number.isSafeInteger(ref.revision)||Number(ref.revision)<1)return undefined; const key=JSON.stringify([ref.block_id,ref.revision]);if(seen.has(key))return undefined;seen.add(key)}
    }
    const sorted=(v:unknown):unknown => Array.isArray(v) ? v.map(sorted) : object(v) ? Object.fromEntries(Object.keys(v).sort().map((key)=>[key,sorted(v[key])])) : v
    if(JSON.stringify(sorted(value))!==raw)return undefined
    return {metadata:metadata as {name?:string,aliases?:string[],domain_refs?:Array<{block_id:string,revision:number}>,types?:string[],processing_role?:string}}
  } catch {return undefined}
}

/** Exact aliases are selectors only; ambiguous or gated aliases reveal no candidate. */
export function resolveSkillInvocation(skills: Record<string, Info>, name: string, mode: InvocationMode): InvocationResult {
  const direct = skills[name]
  if (direct) return evaluateInvocation(direct, mode)
  const candidates = Object.values(skills).filter((item) => item.aliases?.includes(name))
    .map((item) => evaluateInvocation(item, mode)).filter((result): result is { ok: true; info: Info } => result.ok)
  if (candidates.length !== 1) return { ok: false, reason: "not_found", name: "" }
  return candidates[0]!
}

/** Bounded, content-free explanation; canonical authorization precedes projection. */
export function resolutionCandidateViews(
  skills: Record<string,Info>, priorities: Record<string,number>, shadows: Array<{info:Info;priority:number}>,
  incomplete: boolean, name: string, mode: InvocationMode, canSee: (name:string)=>boolean,
) {
  const selected=Object.values(skills).map((info)=>({info,priority:priorities[info.name] ?? Number.MAX_SAFE_INTEGER,selected:true}))
  const shadowed=shadows.map(({info,priority})=>({info,priority,selected:false}))
  const items=[...selected,...shadowed].filter(({info}) => (info.name===name || info.aliases?.includes(name))
    && evaluateInvocation(info,mode).ok && canSee(info.name))
    .map(({info,priority,selected})=>({name:info.name,skillID:info.skillID,revision:info.revision,contentHash:info.contentHash,selected,priority}))
  return {items,complete:!incomplete}
}

// Open Clank injects its central lifecycle catalogue separately from each
// worker's private runtime data root.
const _OPEN_CLANK_SKILLS_DIR = (): string | null => {
  if (typeof process === "undefined") return null
  const explicit = process.env.OPEN_CLANK_SKILLS_DIR
  if (explicit) return explicit
  const envDir = process.env.OPEN_CLANK_DATA_DIR ?? process.env.ODYSSEUS_DATA_DIR
  if (envDir) return envDir + "/skills"
  return null
}

const _isOpenClankSkill = (match: string): boolean => {
  const root = _OPEN_CLANK_SKILLS_DIR()
  if (!root) return false
  const relative = path.relative(path.resolve(root), path.resolve(match))
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
}

async function _verifiedOpenClankDiscoveryPath(root: string, match: string): Promise<boolean> {
  const fs = await import("fs/promises")
  try {
    const resolvedRoot = path.resolve(root)
    const resolvedMatch = path.resolve(match)
    const relative = path.relative(resolvedRoot, resolvedMatch)
    if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return false

    const rootStat = await fs.lstat(resolvedRoot)
    if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) return false

    const [realRoot, realMatch] = await Promise.all([fs.realpath(resolvedRoot), fs.realpath(resolvedMatch)])
    const realRelative = path.relative(realRoot, realMatch)
    if (!realRelative || realRelative.startsWith(`..${path.sep}`) || path.isAbsolute(realRelative)) return false

    let current = resolvedRoot
    const parts = relative.split(path.sep)
    for (const [index, part] of parts.entries()) {
      current = path.join(current, part)
      const stat = await fs.lstat(current)
      if (stat.isSymbolicLink()) return false
      if (index < parts.length - 1 ? !stat.isDirectory() : !stat.isFile()) return false
    }
    return true
  } catch {
    return false
  }
}

type PublishedBundle = {
  version: number
  files: Record<string, { sha256: string; size: number; mode: number }>
}

async function _verifiedPublishedBundle(
  match: string,
  pointer: any,
  fs: typeof import("fs/promises"),
): Promise<string | undefined> {
  if (
    pointer?.bundle_version !== 2 ||
    typeof pointer?.bundle_root !== "string" ||
    typeof pointer?.bundle_manifest !== "string" ||
    typeof pointer?.bundle_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(pointer.bundle_sha256)
  ) {
    return undefined
  }
  const base = path.resolve(path.dirname(match))
  const revisions = path.resolve(base, "_revisions")
  const bundleRootParts = pointer.bundle_root.split("/")
  const bundleManifestParts = pointer.bundle_manifest.split("/")
  if (
    pointer.bundle_root.includes("\0") ||
    pointer.bundle_manifest.includes("\0") ||
    pointer.bundle_root.includes("\\") ||
    pointer.bundle_manifest.includes("\\") ||
    path.posix.isAbsolute(pointer.bundle_root) ||
    path.posix.isAbsolute(pointer.bundle_manifest) ||
    bundleRootParts[0] !== "_revisions" ||
    bundleRootParts.some((part: string) => !part || part === "." || part === "..") ||
    bundleManifestParts.some((part: string) => !part || part === "." || part === "..")
  ) {
    return undefined
  }
  const hasSymlinkComponent = async (parts: string[]): Promise<boolean> => {
    let current = base
    for (const part of parts) {
      current = path.join(current, part)
      if ((await fs.lstat(current)).isSymbolicLink()) return true
    }
    return false
  }
  if (
    (await hasSymlinkComponent(bundleRootParts)) ||
    (await hasSymlinkComponent(bundleManifestParts))
  ) {
    return undefined
  }
  const root = path.resolve(base, pointer.bundle_root)
  const manifestPath = path.resolve(base, pointer.bundle_manifest)
  const rootRelative = path.relative(revisions, root)
  if (
    !rootRelative ||
    rootRelative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(rootRelative) ||
    manifestPath !== path.join(root, "_manifest.json")
  ) {
    return undefined
  }
  const [rootStat, manifestStat, manifestText] = await Promise.all([
    fs.lstat(root),
    fs.lstat(manifestPath),
    fs.readFile(manifestPath, "utf-8"),
  ])
  if (
    rootStat.isSymbolicLink() ||
    !rootStat.isDirectory() ||
    manifestStat.isSymbolicLink() ||
    !manifestStat.isFile() ||
    createHash("sha256").update(manifestText, "utf-8").digest("hex") !== pointer.bundle_sha256
  ) {
    return undefined
  }

  let manifest: PublishedBundle
  try {
    manifest = JSON.parse(manifestText)
  } catch {
    return undefined
  }
  if (manifest?.version !== 2 || !manifest.files || typeof manifest.files !== "object") return undefined

  const expected = new Set<string>()
  for (const [relative, metadata] of Object.entries(manifest.files)) {
    const parts = relative.split("/")
    if (
      !relative ||
      relative.includes("\\") ||
      path.posix.isAbsolute(relative) ||
      parts.some((part) => !part || part === "." || part === "..") ||
      !metadata ||
      typeof metadata.sha256 !== "string" ||
      !/^[a-f0-9]{64}$/i.test(metadata.sha256) ||
      !Number.isSafeInteger(metadata.size) ||
      metadata.size < 0 ||
      !Number.isSafeInteger(metadata.mode) ||
      metadata.mode < 0 ||
      metadata.mode > 0o7777
    ) {
      return undefined
    }
    const target = path.resolve(root, ...parts)
    const targetRelative = path.relative(root, target)
    if (!targetRelative || targetRelative.startsWith(`..${path.sep}`) || path.isAbsolute(targetRelative)) {
      return undefined
    }
    const info = await fs.lstat(target)
    if (info.isSymbolicLink() || !info.isFile() || (info.mode & 0o7777) !== metadata.mode) return undefined
    const content = await fs.readFile(target)
    if (
      content.byteLength !== metadata.size ||
      createHash("sha256").update(content).digest("hex") !== metadata.sha256
    ) {
      return undefined
    }
    expected.add(relative)
  }
  if (!expected.has("SKILL.md")) return undefined

  const actual = new Set<string>()
  const walk = async (directory: string, prefix = ""): Promise<boolean> => {
    const entries = await fs.readdir(directory, { withFileTypes: true })
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name)
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name
      if (entry.isSymbolicLink()) return false
      if (entry.isDirectory()) {
        if (!(await walk(absolute, relative))) return false
      } else if (entry.isFile()) {
        if (relative !== "_manifest.json") actual.add(relative)
      } else {
        return false
      }
    }
    return true
  }
  if (!(await walk(root))) return undefined
  if (actual.size !== expected.size || [...actual].some((item) => !expected.has(item))) return undefined
  return path.join(root, "SKILL.md")
}

async function _publishedOpenClankSkill(
  match: string,
  md: any,
): Promise<{ md: any; state?: any; location?: string } | undefined> {
  if (!_isOpenClankSkill(match)) return { md }
  const owner = process.env.OPEN_CLANK_OWNER
  if (owner === undefined || String(md.data.owner ?? "") !== owner) return undefined

  const fs = await import("fs/promises")
  const lifecyclePath = path.join(path.dirname(match), "_lifecycle.json")
  try {
    const state = JSON.parse(await fs.readFile(lifecyclePath, "utf-8"))
    if (state?.metadata_pending != null) return undefined
    const pointer = state?.published
    if (pointer?.snapshot) {
      const base = path.resolve(path.dirname(match))
      if (
        typeof pointer.snapshot !== "string" ||
        pointer.snapshot.includes("\0") ||
        pointer.snapshot.includes("\\") ||
        path.posix.isAbsolute(pointer.snapshot)
      ) {
        return undefined
      }
      const snapshotParts = pointer.snapshot.split("/")
      if (
        snapshotParts[0] !== "_revisions" ||
        snapshotParts.some((part: string) => !part || part === "." || part === "..")
      ) {
        return undefined
      }
      let snapshotComponent = base
      for (const part of snapshotParts) {
        snapshotComponent = path.join(snapshotComponent, part)
        if ((await fs.lstat(snapshotComponent)).isSymbolicLink()) return undefined
      }
      const snapshot = path.resolve(base, pointer.snapshot)
      const relative = path.relative(base, snapshot)
      if (relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return undefined
      const snapshotText = await fs.readFile(snapshot, "utf-8")
      if (
        typeof pointer.snapshot_sha256 !== "string" ||
        !/^[a-f0-9]{64}$/i.test(pointer.snapshot_sha256)
      ) {
        log.error("published skill pointer lacks a snapshot hash", { match, snapshot })
        return undefined
      }
      if (createHash("sha256").update(snapshotText, "utf-8").digest("hex") !== pointer.snapshot_sha256) {
        log.error("published skill snapshot bytes changed", { match, snapshot })
        return undefined
      }
      const bundleSkill = await _verifiedPublishedBundle(match, pointer, fs)
      if (!bundleSkill) {
        log.error("published skill bundle is missing or invalid", { match })
        return undefined
      }
      const bundleText = await fs.readFile(bundleSkill, "utf-8")
      if (bundleText !== snapshotText) {
        log.error("published skill bundle does not match its revision snapshot", { match, snapshot, bundleSkill })
        return undefined
      }
      const published = await ConfigMarkdown.parse(bundleSkill)
      const headOwner = String(md.data.owner ?? "")
      const publishedOwner = String(published.data.owner ?? "")
      if (
        state.skill_id !== md.data.skill_id ||
        state.head_revision !== md.data.revision ||
        state.head_hash !== md.data.content_hash ||
        !("owner" in state) ||
        String(state.owner ?? "") !== publishedOwner ||
        pointer.skill_id !== md.data.skill_id ||
        !("owner" in pointer) ||
        String(pointer.owner ?? "") !== publishedOwner ||
        published.data.skill_id !== state.skill_id ||
        published.data.revision !== pointer.revision ||
        published.data.content_hash !== pointer.content_hash ||
        publishedOwner !== headOwner ||
        (owner !== undefined && publishedOwner !== owner)
      ) {
        log.error("published skill pointer does not match snapshot", { match, snapshot })
        return undefined
      }
      return { md: published, state, location: bundleSkill }
    }
    // A lifecycle file with no pointer is an explicit demotion. Do not revive
    // it from stale legacy frontmatter.
    return undefined
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== "ENOENT") {
      log.error("failed to validate Open Clank skill lifecycle", { match, error })
      return undefined
    }
  }
  // Legacy frontmatter is provenance, not local publication authority.
  return undefined
}

/** Verify the original catalogue file, without switching to a newer source. */
export async function _sourceStillCurrent(skill: Info): Promise<boolean> {
  if (!skill.sourceFileHash || skill.sourceFileDevice === undefined || skill.sourceFileInode === undefined) return false
  let file: Awaited<ReturnType<typeof open>> | undefined
  try {
    file = await open(skill.location, "r")
    const metadata = await file.stat()
    if (!metadata.isFile() || metadata.size > 4 * 1024 * 1024 ||
        metadata.dev !== skill.sourceFileDevice || metadata.ino !== skill.sourceFileInode) return false
    const bytes = await file.readFile()
    const after = await file.stat()
    return bytes.length === metadata.size && after.size === metadata.size && after.mtimeMs === metadata.mtimeMs &&
      createHash("sha256").update(bytes).digest("hex") === skill.sourceFileHash
  } catch {
    return false
  } finally {
    await file?.close()
  }
}

/** Check a body load after canonical permission. Metadata-qualified loads do not record a use count. */
export async function _prepareBody(skill: Info, sessionID: string, actor?: ActorExecutionIdentity): Promise<boolean> {
  if (!(await _sourceStillCurrent(skill))) return false
  if (skill.metadataClaim) {
    if (!actor || actor.sessionID!==sessionID || !actor.rootOperationID || !actor.innerTurnID || !skill.skillID || !skill.revision ||
        !skill.contentHash || !skill.metadataSnapshotSHA256 || !skill.metadataReceiptSHA256) return false
    try {
      const managed=await import("@/acp/managed-provider")
      if (!managed.enabled()) return false
      const result=await managed.managedHostCall("_openclank/session/v1/skill/metadata-current",{
        sessionID,rootOperationID:actor.rootOperationID,actorID:actor.actorID,executionRevision:actor.executionRevision,innerTurnID:actor.innerTurnID,
        skillID:skill.skillID,revision:skill.revision,contentHash:skill.contentHash,snapshotSHA256:skill.metadataSnapshotSHA256,metadataReceiptSHA256:skill.metadataReceiptSHA256,
      })
      if (result.state!=="ready" || !(await _sourceStillCurrent(skill))) return false
      // The existing child usage writer cannot revalidate the host's domain
      // authority. Do not fabricate a usage event for this optional path.
      return true
    } catch { return false }
  }
  return _writeUsage(skill,sessionID,actor)
}

export async function _writeUsage(
  skill: Pick<Info, "name" | "skillID" | "revision" | "contentHash" | "owner" | "location">,
  sessionID: string,
  actorExecution?: ActorExecutionIdentity,
): Promise<boolean> {
  if (!_isOpenClankSkill(skill.location)) return true
  if (!skill.skillID || !skill.revision || !skill.contentHash) return false
  try {
    const result = await callBoundOpenClankTool(sessionID, "record_skill_usage", {
      name: skill.name,
      skill_id: skill.skillID,
      revision: skill.revision,
      content_hash: skill.contentHash,
    }, actorExecution, "skill")
    const block = result.content.find((item) => item.type === "text")
    if (!block || block.type !== "text") return false
    const payload = JSON.parse(block.text)
    return (
      payload?.ok === true &&
      payload.name === skill.name &&
      payload.skill_id === skill.skillID &&
      payload.owner === (skill.owner ?? "") &&
      payload.revision === skill.revision &&
      payload.content_hash === skill.contentHash
    )
  } catch {
    return false
  }
}

export const InvalidError = NamedError.create(
  "SkillInvalidError",
  z.object({
    path: z.string(),
    message: z.string().optional(),
    issues: z.custom<z.core.$ZodIssue[]>().optional(),
  }),
)

export const NameMismatchError = NamedError.create(
  "SkillNameMismatchError",
  z.object({
    path: z.string(),
    expected: z.string(),
    actual: z.string(),
  }),
)

type State = {
  skills: Record<string, Info>
  dirs: Set<string>
  priorities: Record<string, number>
  shadows: Array<{info: Info; priority: number}>
  shadowsIncomplete: boolean
}

type DiscoveryState = {
  matches: string[]
  dirs: string[]
  bundledRoots: string[]
  priorities: Record<string, number>
}

type ScanState = {
  matches: Set<string>
  dirs: Set<string>
  priorities: Map<string, number>
}

export interface Interface {
  readonly get: (name: string) => Effect.Effect<Info | undefined>
  readonly all: () => Effect.Effect<Info[]>
  readonly dirs: () => Effect.Effect<string[]>
  readonly available: (agent?: Agent.Info) => Effect.Effect<Info[]>
  /** Optional process-local evidence, invalidated with the actual catalog scope. */
  readonly availableWithProvenance?: (agent?: Agent.Info) => Effect.Effect<{items:Info[]; current:()=>boolean}>
  readonly reload: () => Effect.Effect<void>
  /** Resolve a skill for model or user-explicit invocation with trust/visibility gates. */
  readonly getForInvocation: (name: string, mode: InvocationMode) => Effect.Effect<InvocationResult>
  /** Content-free candidates, filtered through the current actor's canonical skill permission. */
  readonly resolutionCandidates: (name: string, agent: Agent.Info, mode: InvocationMode) => Effect.Effect<{items: Array<{name: string; skillID?: string; revision?: number; contentHash?: string; selected: boolean; priority: number}>, complete: boolean}>
  /** Skills a user may explicitly invoke (slash/mention), including trusted hidden. */
  readonly explicit: () => Effect.Effect<Info[]>
}

const add = Effect.fnUntraced(function* (
  state: State,
  match: string,
  bundledRoots: string[],
  priority: number,
  bus: Bus.Interface,
) {
  let md = yield* Effect.tryPromise({
    try: () => ConfigMarkdown.parse(match),
    catch: (err) => err,
  }).pipe(
    Effect.catch(
      Effect.fnUntraced(function* (err) {
        const message = ConfigMarkdown.FrontmatterError.isInstance(err)
          ? err.data.message
          : `Failed to parse skill ${match}`
        const { Session } = yield* Effect.promise(() => import("@/session"))
        yield* bus.publish(Session.Event.Error, { error: new NamedError.Unknown({ message }).toObject() })
        log.error("failed to load skill", { skill: match, err })
        return undefined
      }),
    ),
  )

  if (!md) return
  const initialMd = md
  const remote = priority >= 90
  const remoteManifest = remote
    ? yield* Effect.tryPromise(() =>
        import("fs/promises")
          .then((fs) => fs.readFile(path.join(path.dirname(match), ".remote-skill.json"), "utf-8"))
          .then(JSON.parse),
      ).pipe(Effect.catch(() => Effect.succeed(undefined)))
    : undefined
  const activation = remote
    ? { md: initialMd }
    : yield* Effect.promise(() => _publishedOpenClankSkill(match, initialMd))
  if (!activation) return
  const activeMd = activation.md

  const hasClaim=Object.prototype.hasOwnProperty.call(activeMd.data,"openclank_metadata_v1")
  const claim=hasClaim ? parseResourceMetadataClaim(activeMd.data.openclank_metadata_v1) : undefined
  if(hasClaim && !claim)return
  const declaredAliases=activeMd.data.aliases
  const claimAliases=claim?.metadata.aliases
  if(claimAliases && declaredAliases && JSON.stringify(claimAliases)!==JSON.stringify(declaredAliases))return
  const parsed = Info.pick({
    name: true,
    description: true,
    aliases: true,
    hidden: true,
    disableModelInvocation: true,
  }).safeParse({
    name: activeMd.data.name,
    description: activeMd.data.description,
    aliases: claimAliases ?? declaredAliases,
    hidden: activeMd.data.hidden,
    disableModelInvocation: activeMd.data["disable-model-invocation"] ?? activeMd.data.disableModelInvocation,
  })
  if (!parsed.success) return

  const isBundled = bundledRoots.some((root) => match.startsWith(root))
  const existing = state.skills[parsed.data.name]

  let candidateWins = true
  if (existing) {
    const existingPriority = state.priorities[parsed.data.name] ?? Number.MAX_SAFE_INTEGER
    const explicitBundledOverride = process.env.MIMOCODE_ALLOW_BUNDLED_SKILL_OVERRIDE === "1"
    const bundledProtected = existing.bundled && !isBundled && !explicitBundledOverride
    candidateWins =
      !bundledProtected &&
      (
        (explicitBundledOverride && existing.bundled && !isBundled) ||
        (isBundled && !existing.bundled) ||
        priority < existingPriority ||
        (priority === existingPriority && match.localeCompare(existing.location) < 0)
      )
    log.warn("duplicate skill name", {
      name: parsed.data.name,
      existing: existing.location,
      duplicate: match,
      winner: candidateWins ? match : existing.location,
      existingPriority,
      duplicatePriority: priority,
    })
  }

  const activeLocation = activation.location ?? match
  // Record the file identity associated with the parsed catalogue snapshot.
  const identity = yield* Effect.promise(() => stat(activeLocation).catch(() => undefined))
  const sourceFileHash = activeMd.orig ? createHash("sha256").update(activeMd.orig).digest("hex") : undefined
  const metadataRows=Object.values(activation.state?.metadata_receipts ?? {}).filter((row:any)=>row && row.content_hash===activeMd.data.content_hash && row.revision===activeMd.data.revision)
  const metadataReceipt=metadataRows.length===1 ? metadataRows[0] as any : undefined
  const candidate: Info = {
    name: parsed.data.name,
    description: parsed.data.description,
    aliases: parsed.data.aliases,
    location: activeLocation,
    content: activeMd.content,
    sourceFileHash,
    sourceFileDevice: identity?.dev,
    sourceFileInode: identity?.ino,
    metadataClaim:hasClaim || undefined,
    metadataReceiptSHA256:hasClaim && activation.state?.metadata_profile==="skill-metadata-writer.v1" && typeof metadataReceipt?.receipt_sha256==="string" ? metadataReceipt.receipt_sha256 : undefined,
    metadataSnapshotSHA256:hasClaim && typeof activation.state?.published?.snapshot_sha256==="string" ? activation.state.published.snapshot_sha256 : undefined,
    domainRefs:hasClaim && activation.state?.metadata_profile==="skill-metadata-writer.v1" && metadataReceipt && activation.state?.published
      ? claim?.metadata.domain_refs : undefined,
    hidden: remote ? true : parsed.data.hidden ?? (activation.state?.published ? false : undefined),
    disableModelInvocation: parsed.data.disableModelInvocation,
    bundled: isBundled || undefined,
    skillID: typeof activeMd.data.skill_id === "string" ? activeMd.data.skill_id : undefined,
    revision: typeof activeMd.data.revision === "number" ? activeMd.data.revision : undefined,
    contentHash: remote
      ? typeof remoteManifest?.files?.["SKILL.md"] === "string"
        ? remoteManifest.files["SKILL.md"]
        : undefined
      : typeof activeMd.data.content_hash === "string"
        ? activeMd.data.content_hash
        : undefined,
    owner: typeof activeMd.data.owner === "string" ? activeMd.data.owner : undefined,
    status: remote
      ? "staged"
      : activation.state?.published
      ? "published"
      : typeof activeMd.data.status === "string"
        ? activeMd.data.status
        : undefined,
    source: remote ? "remote" : typeof activeMd.data.source === "string" ? activeMd.data.source : undefined,
    sourceStatus: remote
      ? typeof activeMd.data.status === "string"
        ? activeMd.data.status
        : undefined
      : typeof activeMd.data.source_status === "string"
        ? activeMd.data.source_status
        : undefined,
    sourceURI: remote
      ? typeof remoteManifest?.base === "string"
        ? remoteManifest.base
        : undefined
      : typeof activeMd.data.source_uri === "string"
        ? activeMd.data.source_uri
        : undefined,
    sourceRevision: remote
      ? typeof remoteManifest?.revision === "string"
        ? remoteManifest.revision
        : undefined
      : typeof activeMd.data.source_revision === "string"
        ? activeMd.data.source_revision
        : undefined,
    platforms: Array.isArray(activeMd.data.platforms) ? activeMd.data.platforms.map(String) : undefined,
    requiresToolsets: Array.isArray(activeMd.data.requires_toolsets)
      ? activeMd.data.requires_toolsets.map(String)
      : undefined,
    trust: remote
      ? "untrusted"
      : activation.state?.published?.waiver
        ? "waived"
      : activation.state?.attestations?.[activeMd.data.content_hash]?.verdict === "pass" &&
          activation.state?.attestations?.[activeMd.data.content_hash]?.compatible === true &&
          activation.state?.attestations?.[activeMd.data.content_hash]?.skill_id === activeMd.data.skill_id &&
          activation.state?.attestations?.[activeMd.data.content_hash]?.revision === activeMd.data.revision &&
          activation.state?.attestations?.[activeMd.data.content_hash]?.content_hash === activeMd.data.content_hash
        ? "verified"
        : _isOpenClankSkill(match)
          ? "published"
          : undefined,
    lastAudit:
      typeof activation.state?.attestations?.[activeMd.data.content_hash]?.audited_at === "number"
        ? activation.state.attestations[activeMd.data.content_hash].audited_at
        : undefined,
  }
  if (existing) {
    const shadow = candidateWins ? {info:existing, priority:state.priorities[parsed.data.name] ?? Number.MAX_SAFE_INTEGER} : {info:candidate, priority}
    if (state.shadows.length < 128) state.shadows.push({info:{...shadow.info,content:""},priority:shadow.priority})
    else state.shadowsIncomplete = true
  }
  if (!candidateWins) return
  state.dirs.add(path.dirname(activeLocation))
  state.priorities[parsed.data.name] = priority
  state.skills[parsed.data.name] = candidate
})

const scan = Effect.fnUntraced(function* (
  state: ScanState,
  root: string,
  pattern: string,
  opts?: { dot?: boolean; scope?: string; priority?: number },
) {
  const matches = yield* Effect.tryPromise({
    try: () =>
      Glob.scan(pattern, {
        cwd: root,
        absolute: true,
        include: "file",
        symlink: true,
        dot: opts?.dot,
      }),
    catch: (error) => error,
  }).pipe(
    Effect.catch((error) => {
      if (!opts?.scope) return Effect.die(error)
      log.error(`failed to scan ${opts.scope} skills`, { dir: root, error })
      return Effect.succeed([] as string[])
    }),
  )

  const openClankRoot = _OPEN_CLANK_SKILLS_DIR()
  const authoritative = openClankRoot !== null && path.resolve(root) === path.resolve(openClankRoot)
  const accepted = authoritative
    ? yield* Effect.promise(() =>
        Promise.all(matches.map(async (match) => ((await _verifiedOpenClankDiscoveryPath(root, match)) ? match : null))),
      )
    : matches

  for (const match of accepted) {
    if (match === null) continue
    state.matches.add(match)
    state.dirs.add(path.dirname(match))
    const priority = _isOpenClankSkill(match) ? 10 : opts?.priority ?? 50
    state.priorities.set(match, Math.min(state.priorities.get(match) ?? priority, priority))
  }
})

const discoverSkills = Effect.fnUntraced(function* (
  config: Config.Interface,
  discovery: Discovery.Interface,
  fsys: AppFileSystem.Interface,
  directory: string,
  worktree: string,
) {
  const state: ScanState = { matches: new Set(), dirs: new Set(), priorities: new Map() }
  const bundledRoots: string[] = []

  // Extract builtin skills to disk first (user skills with same name override)
  if (!Flag.MIMOCODE_DISABLE_BUILTIN_SKILLS) {
    const builtinSkillRoot = yield* extractBuiltinBundle(fsys).pipe(
      Effect.catch(() => Effect.succeed(undefined)),
    )
    if (builtinSkillRoot && (yield* fsys.isDir(builtinSkillRoot))) {
      bundledRoots.push(builtinSkillRoot)
      yield* scan(state, builtinSkillRoot, BUILTIN_SKILL_PATTERN, { scope: "builtin", priority: 0 })
      if (Flag.MIMOCODE_DISABLE_OFFICIAL_SKILLS) {
        const skillsRoot = path.join(builtinSkillRoot, "skills")
        for (const name of OFFICIAL_SKILL_NAMES) {
          const prefix = path.join(skillsRoot, name) + path.sep
          for (const match of state.matches) {
            if (match.startsWith(prefix)) {
              state.matches.delete(match)
              state.dirs.delete(path.dirname(match))
            }
          }
        }
      }
    }
  }

  // Extract compose skills to disk (user skills with same name override)
  if (!Flag.MIMOCODE_DISABLE_COMPOSE_SKILLS) {
    const composeSkillRoot = yield* extractComposeBundle(fsys).pipe(
      Effect.catch(() => Effect.succeed(undefined)),
    )
    if (composeSkillRoot && (yield* fsys.isDir(composeSkillRoot))) {
      bundledRoots.push(composeSkillRoot)
      yield* scan(state, composeSkillRoot, SKILL_PATTERN, { scope: "compose", priority: 5 })
    }
  }

  if (!Flag.MIMOCODE_DISABLE_EXTERNAL_SKILLS) {
    const externalDirs = EXTERNAL_DIRS.filter((dir) => {
      if (dir === ".claude" && Flag.MIMOCODE_DISABLE_CLAUDE_CODE_SKILLS) return false
      if (dir === ".codex" && Flag.MIMOCODE_DISABLE_CODEX_SKILLS) return false
      if (dir === ".opencode" && Flag.MIMOCODE_DISABLE_OPENCODE_SKILLS) return false
      return true
    })

    for (const dir of externalDirs) {
      const root = path.join(Global.Path.home, dir)
      if (!(yield* fsys.isDir(root))) continue
      yield* scan(state, root, EXTERNAL_SKILL_PATTERN, { dot: true, scope: "global", priority: 60 })
    }

    const upDirs = yield* fsys
      .up({ targets: externalDirs, start: directory, stop: worktree })
      .pipe(Effect.catch(() => Effect.succeed([] as string[])))

    for (const root of upDirs) {
      yield* scan(state, root, EXTERNAL_SKILL_PATTERN, { dot: true, scope: "project", priority: 20 })
    }
  }

  const configDirs = yield* config.directories()
  for (const [index, dir] of configDirs.entries()) {
    yield* scan(state, dir, MIMOCODE_SKILL_PATTERN, { scope: "config", priority: 30 + index })
  }

  const cfg = yield* config.get()
  for (const [index, item] of (cfg.skills?.paths ?? []).entries()) {
    const expanded = item.startsWith("~/") ? path.join(os.homedir(), item.slice(2)) : item
    const dir = path.isAbsolute(expanded) ? expanded : path.join(directory, expanded)
    if (!(yield* fsys.isDir(dir))) {
      log.warn("skill path not found", { path: dir })
      continue
    }

    yield* scan(state, dir, SKILL_PATTERN, { scope: "configured", priority: 40 + index })
  }

  for (const url of cfg.skills?.urls ?? []) {
    const pulledDirs = yield* discovery.pull(url)
    for (const dir of pulledDirs) {
      yield* scan(state, dir, SKILL_PATTERN, { scope: "remote", priority: 90 })
    }
  }

  return {
    matches: Array.from(state.matches),
    dirs: Array.from(state.dirs),
    bundledRoots,
    priorities: Object.fromEntries(state.priorities),
  }
})

const loadSkills = Effect.fnUntraced(function* (state: State, discovered: DiscoveryState, bus: Bus.Interface) {
  const matches = discovered.matches.toSorted(
    (a, b) => (discovered.priorities[a] ?? 50) - (discovered.priorities[b] ?? 50) || a.localeCompare(b),
  )
  for (const match of matches) {
    yield* add(state, match, discovered.bundledRoots, discovered.priorities[match] ?? 50, bus)
  }

  log.info("init", { count: Object.keys(state.skills).length })
})

const catalogLifetimes = new WeakMap<State, {alive:boolean}>()

export class Service extends Context.Service<Service, Interface>()("@opencode/Skill") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const discovery = yield* Discovery.Service
    const config = yield* Config.Service
    const bus = yield* Bus.Service
    const fsys = yield* AppFileSystem.Service
    const discovered = yield* InstanceState.make(
      Effect.fn("Skill.discovery")(function* (ctx) {
        return yield* discoverSkills(config, discovery, fsys, ctx.directory, ctx.worktree)
      }),
    )
    const state = yield* InstanceState.make(
      Effect.fn("Skill.state")(function* (ctx) {
        const s: State = { skills: {}, dirs: new Set(), priorities: {}, shadows: [], shadowsIncomplete: false }
        yield* loadSkills(s, yield* InstanceState.get(discovered), bus)
        const lifetime = {alive:true}
        catalogLifetimes.set(s, lifetime)
        yield* Effect.addFinalizer(() => Effect.sync(() => { lifetime.alive = false }))
        return s
      }),
    )

    const get = Effect.fn("Skill.get")(function* (name: string) {
      const s = yield* InstanceState.get(state)
      const item = s.skills[name]
      return item?.hidden ? undefined : item
    })

    const all = Effect.fn("Skill.all")(function* () {
      const s = yield* InstanceState.get(state)
      return Object.values(s.skills).filter((skill) => !skill.hidden)
    })

    const dirs = Effect.fn("Skill.dirs")(function* () {
      return (yield* InstanceState.get(discovered)).dirs
    })

    const selectAvailable = (s:State, agent?:Agent.Info) => {
      const list = Object.values(s.skills).filter((item) => isModelInvocable(item)).toSorted((a, b) => a.name.localeCompare(b.name))
      return agent ? list.filter((item) => Permission.evaluate("skill", item.name, agent.permission).action !== "deny") : list
    }
    const available = Effect.fn("Skill.available")(function* (agent?: Agent.Info) {
      return selectAvailable(yield* InstanceState.get(state), agent)
    })
    const availableWithProvenance = Effect.fn("Skill.availableWithProvenance")(function* (agent?: Agent.Info) {
      const s = yield* InstanceState.get(state)
      const lifetime = catalogLifetimes.get(s)
      const items = selectAvailable(s, agent)
      const catalogSnapshot = (list:Info[]) => {
        if (list.length > 128) return
        for (const item of list) {
          const fields = [item.name,item.description,item.skillID,item.source,item.sourceStatus,item.sourceRevision,item.owner,item.location,item.trust,item.status]
          if (fields.some(value=>typeof value === "string" && Buffer.byteLength(value,"utf8") > 4096) ||
              (item.platforms?.length ?? 0)>128 || (item.requiresToolsets?.length ?? 0)>128 || (item.domainRefs?.length ?? 0)>128 ||
              [...(item.platforms ?? []),...(item.requiresToolsets ?? []),...(item.domainRefs ?? []).map(ref=>ref.block_id)].some(value=>typeof value !== "string" || Buffer.byteLength(value,"utf8")>4096)) return
        }
        const value=fmt(list,{verbose:true})
        return Buffer.byteLength(value,"utf8")<=65536 ? value : undefined
      }
      const snapshot = catalogSnapshot(items)
      return {items, current: () => {
        try { return !!lifetime?.alive && snapshot !== undefined && catalogSnapshot(selectAvailable(s, agent)) === snapshot } catch { return false }
      }}
    })

    const getForInvocation = Effect.fn("Skill.getForInvocation")(function* (name: string, mode: InvocationMode) {
      const s = yield* InstanceState.get(state)
      return resolveSkillInvocation(s.skills, name, mode)
    })

    const resolutionCandidates = Effect.fn("Skill.resolutionCandidates")(function* (name: string, agent: Agent.Info, mode: InvocationMode) {
      const s=yield* InstanceState.get(state)
      if (!agent || !agent.permission) return {items: [], complete: false}
      return resolutionCandidateViews(s.skills,s.priorities,s.shadows,s.shadowsIncomplete,name,mode,
        (canonical)=>Permission.evaluate("skill",canonical,agent.permission).action!=="deny")
    })

    const explicit = Effect.fn("Skill.explicit")(function* () {
      const s = yield* InstanceState.get(state)
      return Object.values(s.skills)
        .filter((skill) => isExplicitlyInvocable(skill))
        .toSorted((a, b) => a.name.localeCompare(b.name))
    })

    const reload = Effect.fn("Skill.reload")(function* () {
      yield* InstanceState.invalidate(discovered)
      yield* InstanceState.invalidate(state)
    })

    return Service.of({ get, all, dirs, available, availableWithProvenance, reload, getForInvocation, resolutionCandidates, explicit })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Discovery.defaultLayer),
  Layer.provide(Config.defaultLayer),
  Layer.provide(Bus.layer),
  Layer.provide(AppFileSystem.defaultLayer),
)

export function fmt(list: Info[], opts: { verbose: boolean }) {
  if (list.length === 0) return "No skills are currently available."
  const domains = (skill:Info) => {
    if (!skill.domainRefs) return ""
    const refs=skill.domainRefs.slice(0,8).map(({block_id,revision})=>({block_id,revision}))
    const encoded=JSON.stringify(refs).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    return `recorded domain refs ${encoded}; ${skill.domainRefs.length>refs.length ? "additional refs omitted; " : ""}revalidated on load`
  }
  const sorted=list.toSorted((a,b)=>a.name.localeCompare(b.name))
  const marker=opts.verbose ? "  <recorded_domains_omitted>additional domain annotations omitted</recorded_domains_omitted>" : "Additional domain annotations omitted."
  let remaining=8192-Buffer.byteLength(marker,"utf8")-1, omitted=false
  const annotated=sorted.map(skill=>{
    const annotation=domains(skill)
    const wrapped=annotation ? (opts.verbose ? `    <recorded_domains>${annotation}</recorded_domains>` : ` (${annotation})`) : ""
    const bytes=Buffer.byteLength(wrapped,"utf8")+(opts.verbose && wrapped ? 1 : 0)
    if(bytes>remaining) {omitted=true;return {skill,wrapped:""}}
    remaining-=bytes
    return {skill,wrapped}
  })
  if (opts.verbose) {
    return [
      "<available_skills>",
      ...annotated.flatMap(({skill,wrapped}) => [
          "  <skill>",
          `    <name>${skill.name}</name>`,
          `    <description>${skill.description}</description>`,
          `    <skill_id>${skill.skillID ?? skill.name}</skill_id>`,
          `    <revision>${skill.revision ?? 1}</revision>`,
          `    <trust>${skill.trust ?? "unknown"}</trust>`,
          `    <status>${skill.status ?? "unknown"}</status>`,
          `    <source>${skill.source ?? "unknown"}</source>`,
          `    <source_status>${skill.sourceStatus ?? ""}</source_status>`,
          `    <source_revision>${skill.sourceRevision ?? ""}</source_revision>`,
          ...(wrapped ? [wrapped] : []),
          `    <owner>${skill.owner ?? ""}</owner>`,
          `    <last_audit>${skill.lastAudit ?? ""}</last_audit>`,
          `    <platforms>${(skill.platforms ?? []).join(",")}</platforms>`,
          `    <required_tools>${(skill.requiresToolsets ?? []).join(",")}</required_tools>`,
          `    <location>${pathToFileURL(skill.location).href}</location>`,
          "  </skill>",
        ]),
      ...(omitted ? [marker] : []),
      "</available_skills>",
    ].join("\n")
  }

  return [
    "## Available Skills",
    ...annotated.map(
        ({skill,wrapped}) =>
          `- **${skill.name}** [${skill.trust ?? "unknown"} r${skill.revision ?? 1}; ${skill.source ?? "unknown"}${skill.sourceRevision ? `@${skill.sourceRevision}` : ""}]: ${skill.description}${wrapped}`,
      ),
    ...(omitted ? [marker] : []),
  ].join("\n")
}

export * as Skill from "."
