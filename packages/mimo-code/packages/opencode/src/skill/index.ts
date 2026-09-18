import os from "os"
import path from "path"
import { createHash } from "crypto"
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
import { callMemoryTool, getSharedMcpClient } from "@/memory/mcp-client"

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
})
export type Info = z.infer<typeof Info>

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

export async function _writeUsage(
  skill: Pick<Info, "name" | "skillID" | "revision" | "contentHash" | "owner" | "location">,
  sessionID: string,
): Promise<boolean> {
  if (!_isOpenClankSkill(skill.location)) return true
  if (!skill.skillID || !skill.revision || !skill.contentHash) return false
  try {
    const client = await getSharedMcpClient(sessionID)
    const result = await callMemoryTool(client, "record_skill_usage", {
      name: skill.name,
      skill_id: skill.skillID,
      revision: skill.revision,
      content_hash: skill.contentHash,
    })
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
  readonly reload: () => Effect.Effect<void>
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

  const parsed = Info.pick({ name: true, description: true, aliases: true, hidden: true }).safeParse(activeMd.data)
  if (!parsed.success) return

  const isBundled = bundledRoots.some((root) => match.startsWith(root))
  const existing = state.skills[parsed.data.name]

  if (existing) {
    const existingPriority = state.priorities[parsed.data.name] ?? Number.MAX_SAFE_INTEGER
    const explicitBundledOverride = process.env.MIMOCODE_ALLOW_BUNDLED_SKILL_OVERRIDE === "1"
    const bundledProtected = existing.bundled && !isBundled && !explicitBundledOverride
    const candidateWins =
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
    if (!candidateWins) return
  }

  const activeLocation = activation.location ?? match
  state.dirs.add(path.dirname(activeLocation))
  state.priorities[parsed.data.name] = priority
  state.skills[parsed.data.name] = {
    name: parsed.data.name,
    description: parsed.data.description,
    aliases: parsed.data.aliases,
    location: activeLocation,
    content: activeMd.content,
    hidden: remote ? true : activation.state?.published ? false : parsed.data.hidden,
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
        const s: State = { skills: {}, dirs: new Set(), priorities: {} }
        yield* loadSkills(s, yield* InstanceState.get(discovered), bus)
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

    const available = Effect.fn("Skill.available")(function* (agent?: Agent.Info) {
      const s = yield* InstanceState.get(state)
      let list: Info[] = Object.values(s.skills).filter((skill) => !skill.hidden)

      list = list.toSorted((a, b) => a.name.localeCompare(b.name))
      if (!agent) return list
      return list.filter((skill) => Permission.evaluate("skill", skill.name, agent.permission).action !== "deny")
    })

    const reload = Effect.fn("Skill.reload")(function* () {
      yield* InstanceState.invalidate(discovered)
      yield* InstanceState.invalidate(state)
    })

    return Service.of({ get, all, dirs, available, reload })
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
  if (opts.verbose) {
    return [
      "<available_skills>",
      ...list
        .sort((a, b) => a.name.localeCompare(b.name))
        .flatMap((skill) => [
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
          `    <owner>${skill.owner ?? ""}</owner>`,
          `    <last_audit>${skill.lastAudit ?? ""}</last_audit>`,
          `    <platforms>${(skill.platforms ?? []).join(",")}</platforms>`,
          `    <required_tools>${(skill.requiresToolsets ?? []).join(",")}</required_tools>`,
          `    <location>${pathToFileURL(skill.location).href}</location>`,
          "  </skill>",
        ]),
      "</available_skills>",
    ].join("\n")
  }

  return [
    "## Available Skills",
    ...list
      .toSorted((a, b) => a.name.localeCompare(b.name))
      .map(
        (skill) =>
          `- **${skill.name}** [${skill.trust ?? "unknown"} r${skill.revision ?? 1}; ${skill.source ?? "unknown"}${skill.sourceRevision ? `@${skill.sourceRevision}` : ""}]: ${skill.description}`,
      ),
  ].join("\n")
}

export * as Skill from "."
