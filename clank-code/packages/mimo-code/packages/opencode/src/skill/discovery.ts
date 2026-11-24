import { NodePath } from "@effect/platform-node"
import { createHash } from "crypto"
import { lookup as dnsLookup } from "dns/promises"
import { isIP } from "net"
import { join } from "path"
import { Effect, Layer, Path, Context } from "effect"
import matter from "gray-matter"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Flag } from "../flag/flag"
import { Global } from "../global"
import { Log } from "../util"
import { assertSafeUrl } from "../util/ssrf"

const MAX_REDIRECTS = 5
const MAX_INDEX_BYTES = 100_000
const MAX_SKILLS = 100
const MAX_FILES_PER_SKILL = 64
const MAX_FILE_BYTES = 400_000
const MAX_TOTAL_BYTES = 2_000_000
const MAX_DEPTH = 4
const SHA256 = /^[a-f0-9]{64}$/i
const SAFE_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/i
const ARCHIVE = /\.(?:zip|tar|tgz|gz|bz2|xz|7z)$/i
const ALLOWED_SUFFIXES = new Set([
  ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".toml",
  ".js", ".ts", ".css", ".html", ".xml", ".csv",
])
const TEXT_NAMES = new Set(["skill.md", "license", "license.md", "readme.md"])
const SOURCE_SKILL = ".remote-source-SKILL.md"
const INDEX_MANIFEST = ".remote-index.json"

type LookupAll = (hostname: string) => Promise<readonly { address: string; family: number }[]>
export type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>
type RemoteFile = { path: string; sha256: string; size?: number }
type RemoteSkill = { name: string; revision: string; files: RemoteFile[] }

const lookupAll: LookupAll = (hostname) => dnsLookup(hostname, { all: true, verbatim: true })

function isLoopback(address: string) {
  const value = address.toLowerCase()
  return value === "::1" || value === "0:0:0:0:0:0:0:1" || value.startsWith("127.")
}

export async function assertRemoteSkillUrl(
  url: string,
  allowedOrigin: string,
  lookupImpl: LookupAll = lookupAll,
) {
  const parsed = new URL(url)
  if (parsed.protocol !== "https:") throw new Error("remote skills require HTTPS")
  if (parsed.username || parsed.password) throw new Error("remote skill URLs cannot contain credentials")
  if (parsed.search || parsed.hash) throw new Error("remote skill URLs cannot contain a query or fragment")
  if (parsed.origin !== allowedOrigin) throw new Error("remote skill redirect left its configured origin")

  const hostname = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase()
  if (hostname === "localhost" || hostname.endsWith(".localhost")) {
    throw new Error("remote skill origin cannot be loopback")
  }
  const family = isIP(hostname)
  const addresses = family
    ? [{ address: hostname, family }]
    : await lookupImpl(hostname).catch(() => {
        throw new Error(`remote skill DNS resolution failed for "${hostname}"`)
      })
  if (addresses.length === 0) throw new Error(`remote skill DNS resolution returned no addresses for "${hostname}"`)
  for (const item of addresses) {
    if (isLoopback(item.address)) throw new Error("remote skill origin cannot resolve to loopback")
    const literal = item.family === 6 ? `https://[${item.address}]/` : `https://${item.address}/`
    await assertSafeUrl(literal)
  }
}

function safeRemotePath(value: unknown) {
  if (
    typeof value !== "string" ||
    !value ||
    value.includes("\\") ||
    value.includes("\0") ||
    /^[a-z]:/i.test(value) ||
    ARCHIVE.test(value)
  ) {
    throw new Error("remote skill contains an unsafe file path")
  }
  const pieces = value.split("/")
  if (pieces.length - 1 > MAX_DEPTH) throw new Error("remote skill file exceeds the directory depth limit")
  if (
    pieces.some((piece) => {
      if (!piece) return true
      let decoded = piece
      try {
        for (let pass = 0; pass < 3; pass++) {
          const next = decodeURIComponent(decoded)
          if (next === decoded) break
          decoded = next
        }
      } catch {
        return true
      }
      return (
        decoded === "." ||
        decoded === ".." ||
        decoded.includes("/") ||
        decoded.includes("\\") ||
        decoded.includes("\0")
      )
    })
  ) {
    throw new Error("remote skill contains path traversal")
  }
  const name = pieces.at(-1)!.toLowerCase()
  const suffix = name.includes(".") ? name.slice(name.lastIndexOf(".")) : ""
  if (!TEXT_NAMES.has(name) && !ALLOWED_SUFFIXES.has(suffix)) {
    throw new Error("remote skill contains an unsupported file type")
  }
  return pieces.join("/")
}

function parseIndex(raw: Uint8Array): RemoteSkill[] {
  const value = JSON.parse(new TextDecoder().decode(raw))
  if (!value || !Array.isArray(value.skills) || value.skills.length > MAX_SKILLS) {
    throw new Error("remote skill index is invalid or too large")
  }
  let fileCount = 0
  const names = new Set<string>()
  return value.skills.map((item: any) => {
    if (!item || !SAFE_NAME.test(item.name) || typeof item.revision !== "string" || !item.revision.trim()) {
      throw new Error("remote skill identity or pinned revision is invalid")
    }
    if (names.has(item.name)) throw new Error("remote skill index contains a duplicate skill")
    names.add(item.name)
    if (!Array.isArray(item.files) || item.files.length === 0 || item.files.length > MAX_FILES_PER_SKILL) {
      throw new Error("remote skill file list is invalid or too large")
    }
    fileCount += item.files.length
    if (fileCount > MAX_SKILLS * MAX_FILES_PER_SKILL) throw new Error("remote skill index has too many files")
    const hashes = item.hashes && typeof item.hashes === "object" ? item.hashes : {}
    const sizes = item.sizes && typeof item.sizes === "object" ? item.sizes : {}
    const paths = new Set<string>()
    const files: RemoteFile[] = item.files.map((entry: any): RemoteFile => {
      const file = typeof entry === "string" ? { path: entry, sha256: hashes[entry], size: sizes[entry] } : entry
      const remotePath = safeRemotePath(file?.path)
      if (paths.has(remotePath)) throw new Error(`remote skill file is duplicated: ${remotePath}`)
      paths.add(remotePath)
      if (!SHA256.test(String(file?.sha256 ?? ""))) throw new Error(`remote skill file is not hash-pinned: ${remotePath}`)
      const size = file?.size === undefined ? undefined : Number(file.size)
      if (size !== undefined && (!Number.isSafeInteger(size) || size < 0 || size > MAX_FILE_BYTES)) {
        throw new Error(`remote skill file size is invalid: ${remotePath}`)
      }
      return { path: remotePath, sha256: String(file.sha256).toLowerCase(), size }
    })
    if (!files.some((file) => file.path === "SKILL.md")) throw new Error("remote skill entry is missing SKILL.md")
    return { name: item.name, revision: item.revision, files }
  })
}

async function readLimited(response: Response, limit: number) {
  const declared = Number(response.headers.get("content-length"))
  if (Number.isFinite(declared) && declared > limit) throw new Error("remote skill response exceeds its byte limit")
  const reader = response.body?.getReader()
  if (!reader) {
    const bytes = new Uint8Array(await response.arrayBuffer())
    if (bytes.byteLength > limit) throw new Error("remote skill response exceeds its byte limit")
    return bytes
  }
  const chunks: Uint8Array[] = []
  let length = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    length += value.byteLength
    if (length > limit) {
      await reader.cancel()
      throw new Error("remote skill response exceeds its byte limit")
    }
    chunks.push(value)
  }
  const out = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) {
    out.set(chunk, offset)
    offset += chunk.byteLength
  }
  return out
}

async function fetchRemote(
  initialUrl: string,
  allowedOrigin: string,
  limit: number,
  fetchImpl: Fetcher,
) {
  let url = initialUrl
  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects++) {
    await assertRemoteSkillUrl(url, allowedOrigin)
    const response = await fetchImpl(url, { redirect: "manual" })
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get("location")
      if (!location) throw new Error("remote skill redirect omitted its location")
      url = new URL(location, url).href
      continue
    }
    if (!response.ok) throw new Error(`remote skill request failed (${response.status})`)
    return readLimited(response, limit)
  }
  throw new Error("remote skill request exceeded the redirect limit")
}

function sourceFiles(skill: RemoteSkill) {
  return Object.fromEntries(skill.files.map((file) => [file.path, file.sha256]))
}

function stageSkillMarkdown(
  raw: Uint8Array,
  skill: RemoteSkill,
  sourceUri: string,
) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(raw)
  if (text.includes("\0")) throw new Error("remote SKILL.md is not UTF-8 text")
  const parsed = matter(text)
  if (
    parsed.data?.name !== skill.name ||
    typeof parsed.data?.description !== "string" ||
    !parsed.data.description.trim()
  ) {
    throw new Error(`remote SKILL.md identity is invalid: ${skill.name}`)
  }
  const sourceStatus = typeof parsed.data.status === "string" ? parsed.data.status : undefined
  const data = {
    ...parsed.data,
    status: "draft",
    source: "remote",
    source_uri: sourceUri,
    source_revision: skill.revision,
    ...(sourceStatus ? { source_status: sourceStatus } : {}),
  }
  const staged = matter.stringify(parsed.content, data)
  const validated = matter(staged)
  if (
    validated.data?.name !== skill.name ||
    validated.data?.status !== "draft" ||
    validated.data?.source !== "remote"
  ) {
    throw new Error(`remote SKILL.md could not be staged safely: ${skill.name}`)
  }
  return staged
}

function generationDescriptor(
  base: URL,
  skills: RemoteSkill[],
) {
  return {
    version: 2,
    origin: base.origin,
    base: base.href,
    skills: skills.map((skill) => ({
      name: skill.name,
      revision: skill.revision,
      files: sourceFiles(skill),
    })),
  }
}

async function validateGeneration(
  root: string,
  descriptor: ReturnType<typeof generationDescriptor>,
) {
  const fs = await import("fs/promises")
  try {
    const indexPath = pathFor(root, INDEX_MANIFEST)
    const indexStat = await fs.lstat(indexPath)
    if (indexStat.isSymbolicLink() || !indexStat.isFile()) return false
    const stored = JSON.parse(await fs.readFile(indexPath, "utf-8"))
    if (JSON.stringify(stored) !== JSON.stringify(descriptor)) return false

    for (const skill of descriptor.skills) {
      const source = await fs.readFile(pathFor(root, skill.name, SOURCE_SKILL))
      const sourceHash = createHash("sha256").update(source).digest("hex")
      if (sourceHash !== skill.files["SKILL.md"]) return false
      const staged = stageSkillMarkdown(
        source,
        {
          name: skill.name,
          revision: skill.revision,
          files: Object.entries(skill.files).map(([remotePath, sha256]) => ({
            path: remotePath,
            sha256,
          })),
        },
        descriptor.base,
      )
      if (await fs.readFile(pathFor(root, skill.name, "SKILL.md"), "utf-8") !== staged) return false
      for (const [remotePath, sha256] of Object.entries(skill.files)) {
        if (remotePath === "SKILL.md") continue
        const target = pathFor(root, skill.name, ...remotePath.split("/"))
        const stat = await fs.lstat(target)
        if (stat.isSymbolicLink() || !stat.isFile()) return false
        if (createHash("sha256").update(await fs.readFile(target)).digest("hex") !== sha256) return false
      }
      const manifestPath = pathFor(root, skill.name, ".remote-skill.json")
      const manifestStat = await fs.lstat(manifestPath)
      if (manifestStat.isSymbolicLink() || !manifestStat.isFile()) return false
      const manifest = JSON.parse(await fs.readFile(manifestPath, "utf-8"))
      if (
        manifest?.source !== "remote" ||
        manifest?.origin !== descriptor.origin ||
        manifest?.base !== descriptor.base ||
        manifest?.revision !== skill.revision ||
        JSON.stringify(manifest.files) !== JSON.stringify(skill.files)
      ) {
        return false
      }
    }
    return true
  } catch {
    return false
  }
}

function pathFor(...parts: string[]) {
  return join(...parts)
}

export interface Interface {
  readonly pull: (url: string) => Effect.Effect<string[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SkillDiscovery") {}

export const layerWithFetch = (
  fetchImpl: Fetcher = fetch,
): Layer.Layer<Service, never, AppFileSystem.Service | Path.Path> =>
  Layer.effect(
    Service,
    Effect.gen(function* () {
      const log = Log.create({ service: "skill-discovery" })
      const fs = yield* AppFileSystem.Service
      const path = yield* Path.Path
      const cache = path.join(Global.Path.cache, "skills")

      const download = Effect.fn("Discovery.download")(function* (
        url: string,
        origin: string,
        expected: RemoteFile,
      ) {
        const body = yield* Effect.tryPromise(() => fetchRemote(url, origin, MAX_FILE_BYTES, fetchImpl))
        if (expected.size !== undefined && body.byteLength !== expected.size) {
          throw new Error(`remote skill file size changed: ${expected.path}`)
        }
        if (createHash("sha256").update(body).digest("hex") !== expected.sha256) {
          throw new Error(`remote skill file hash changed: ${expected.path}`)
        }
        return body
      })

      const pull = Effect.fn("Discovery.pull")(function* (url: string) {
        if (Flag.MIMOCODE_DISABLE_REMOTE_SKILLS) return []

        return yield* Effect.gen(function* () {
          const base = new URL(url.endsWith("/") ? url : `${url}/`)
          if (
            base.protocol !== "https:" ||
            base.username ||
            base.password ||
            base.search ||
            base.hash
          ) {
            throw new Error("remote skill origins must be credential-free HTTPS URLs")
          }
          const origin = base.origin
          yield* Effect.tryPromise(() => assertRemoteSkillUrl(base.href, origin))
          const indexUrl = new URL("index.json", base).href
          log.info("fetching index", { url: indexUrl })
          const indexBytes = yield* Effect.tryPromise(() =>
            fetchRemote(indexUrl, origin, MAX_INDEX_BYTES, fetchImpl),
          )
          const skills = parseIndex(indexBytes)
          const originKey = createHash("sha256").update(base.href).digest("hex").slice(0, 16)
          const descriptor = generationDescriptor(base, skills)
          const generationKey = createHash("sha256")
            .update(JSON.stringify(descriptor))
            .digest("hex")
          const parent = path.join(cache, originKey, "generations")
          const root = path.join(parent, generationKey)
          const dirs = skills.map((skill) => path.join(root, skill.name))

          if (yield* Effect.tryPromise(() => validateGeneration(root, descriptor))) {
            return dirs
          }
          if (yield* fs.exists(root).pipe(Effect.orDie)) {
            throw new Error("remote skill cache failed integrity validation")
          }

          yield* fs.ensureDir(parent)
          return yield* Effect.acquireUseRelease(
            fs.makeTempDirectory({ directory: parent, prefix: ".stage-" }),
            (stage) =>
              Effect.gen(function* () {
                let totalBytes = 0
                for (const skill of skills) {
                  const skillRoot = path.join(stage, skill.name)
                  for (const file of skill.files) {
                    const fileUrl = new URL(file.path, new URL(`${skill.name}/`, base)).href
                    const body = yield* download(fileUrl, origin, file)
                    totalBytes += body.byteLength
                    if (totalBytes > MAX_TOTAL_BYTES) {
                      throw new Error("remote skill bundle exceeds its total byte limit")
                    }
                    if (file.path === "SKILL.md") {
                      const staged = stageSkillMarkdown(body, skill, base.href)
                      yield* fs.writeWithDirs(path.join(skillRoot, SOURCE_SKILL), body, 0o600)
                      yield* fs.writeWithDirs(path.join(skillRoot, "SKILL.md"), staged, 0o600)
                      continue
                    }
                    yield* fs.writeWithDirs(
                      path.join(skillRoot, ...file.path.split("/")),
                      body,
                      0o600,
                    )
                  }
                  yield* fs.writeWithDirs(
                    path.join(skillRoot, ".remote-skill.json"),
                    JSON.stringify({
                      source: "remote",
                      origin,
                      base: base.href,
                      revision: skill.revision,
                      files: sourceFiles(skill),
                    }),
                    0o600,
                  )
                }
                yield* fs.writeWithDirs(
                  path.join(stage, INDEX_MANIFEST),
                  JSON.stringify(descriptor),
                  0o600,
                )
                if (!(yield* Effect.tryPromise(() => validateGeneration(stage, descriptor)))) {
                  throw new Error("remote skill staging validation failed")
                }

                const published = yield* fs.rename(stage, root).pipe(
                  Effect.as(true),
                  Effect.catch(() => Effect.succeed(false)),
                )
                if (
                  !published &&
                  !(yield* Effect.tryPromise(() => validateGeneration(root, descriptor)))
                ) {
                  throw new Error("remote skill generation could not be published atomically")
                }
                if (!(yield* Effect.tryPromise(() => validateGeneration(root, descriptor)))) {
                  throw new Error("remote skill generation failed post-publish validation")
                }
                return dirs
              }),
            (stage) => fs.remove(stage, { recursive: true, force: true }).pipe(Effect.ignore),
          )
        }).pipe(
          Effect.catchCause((cause) =>
            Effect.sync(() => {
              log.error("remote skill discovery rejected source", { url, cause })
            }).pipe(Effect.andThen(Effect.die(cause))),
          ),
        )
      })

      return Service.of({ pull })
    }),
  )

export const layer = layerWithFetch()

export const defaultLayer: Layer.Layer<Service> = layer.pipe(
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(NodePath.layer),
)

export * as Discovery from "./discovery"
