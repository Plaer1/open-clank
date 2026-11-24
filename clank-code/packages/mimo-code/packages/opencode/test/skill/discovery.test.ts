import { describe, test, expect, beforeAll, afterAll } from "bun:test"
import { NodePath } from "@effect/platform-node"
import { Effect, Layer } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Discovery } from "../../src/skill/discovery"
import { Global } from "../../src/global"
import { Filesystem } from "../../src/util"
import { createHash } from "crypto"
import { readdir, rm } from "fs/promises"
import path from "path"

const CLOUDFLARE_SKILLS_URL = "https://93.184.216.34/.well-known/skills/"
type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>
let downloadCount = 0
let requestCount = 0

const fixturePath = path.join(import.meta.dir, "../fixture/skills")
const cacheDir = path.join(Global.Path.cache, "skills")

beforeAll(async () => {
  await rm(cacheDir, { recursive: true, force: true })
})

afterAll(async () => {
  await rm(cacheDir, { recursive: true, force: true })
})

describe("Discovery.pull", () => {
  const fixtureFetch = async (request: string | URL | Request) => {
    requestCount++
    const url = new URL(request instanceof Request ? request.url : request.toString())
    if (url.pathname.startsWith("/.well-known/skills/")) {
      const filePath = url.pathname.replace("/.well-known/skills/", "")
      const fullPath = path.join(fixturePath, filePath)
      if (await Filesystem.exists(fullPath)) {
        if (!fullPath.endsWith("index.json")) downloadCount++
        return new Response(Bun.file(fullPath))
      }
    }
    return new Response("Not Found", { status: 404 })
  }

  const pull = (
    url: string,
    fetchImpl: Fetcher = fixtureFetch,
  ) => {
    const testLayer = Discovery.layerWithFetch(fetchImpl).pipe(
      Layer.provide(AppFileSystem.defaultLayer),
      Layer.provide(NodePath.layer),
    )
    return Effect.runPromise(
      Discovery.Service.use((service) => service.pull(url)).pipe(Effect.provide(testLayer)),
    )
  }

  test("downloads skills from cloudflare url", async () => {
    const dirs = await pull(CLOUDFLARE_SKILLS_URL)
    expect(dirs.length).toBeGreaterThan(0)
    for (const dir of dirs) {
      expect(dir).toStartWith(cacheDir)
      const md = path.join(dir, "SKILL.md")
      expect(await Filesystem.exists(md)).toBe(true)
    }
  })

  test("url without trailing slash works", async () => {
    const dirs = await pull(CLOUDFLARE_SKILLS_URL.replace(/\/$/, ""))
    expect(dirs.length).toBeGreaterThan(0)
    for (const dir of dirs) {
      const md = path.join(dir, "SKILL.md")
      expect(await Filesystem.exists(md)).toBe(true)
    }
  })

  test("surfaces a missing remote index", async () => {
    await expect(
      pull(new URL("invalid-url/", CLOUDFLARE_SKILLS_URL).href),
    ).rejects.toThrow()
  })

  test("surfaces an invalid remote index", async () => {
    await expect(
      pull(CLOUDFLARE_SKILLS_URL, async () => new Response("not json")),
    ).rejects.toThrow()
  })

  test("remote discovery can be disabled without making a request", async () => {
    const previous = process.env.MIMOCODE_DISABLE_REMOTE_SKILLS
    const before = requestCount
    process.env.MIMOCODE_DISABLE_REMOTE_SKILLS = "1"
    try {
      expect(await pull(CLOUDFLARE_SKILLS_URL)).toEqual([])
      expect(requestCount).toBe(before)
    } finally {
      if (previous === undefined) delete process.env.MIMOCODE_DISABLE_REMOTE_SKILLS
      else process.env.MIMOCODE_DISABLE_REMOTE_SKILLS = previous
    }
  })

  test("downloads reference files alongside SKILL.md", async () => {
    const dirs = await pull(CLOUDFLARE_SKILLS_URL)
    // find a skill dir that should have reference files (e.g. agents-sdk)
    const agentsSdk = dirs.find((dir) => path.basename(dir) === "agents-sdk")
    expect(agentsSdk).toBeDefined()
    if (agentsSdk) {
      const refs = path.join(agentsSdk, "references")
      expect(await Filesystem.exists(path.join(agentsSdk, "SKILL.md"))).toBe(true)
      // agents-sdk has reference files per the index
      const refDir = await Array.fromAsync(new Bun.Glob("**/*.md").scan({ cwd: refs, onlyFiles: true }))
      expect(refDir.length).toBeGreaterThan(0)
    }
  })

  test("caches downloaded files on second pull", async () => {
    // clear dir and downloadCount
    await rm(cacheDir, { recursive: true, force: true })
    downloadCount = 0

    // first pull to populate cache
    const first = await pull(CLOUDFLARE_SKILLS_URL)
    expect(first.length).toBeGreaterThan(0)
    const firstCount = downloadCount
    expect(firstCount).toBeGreaterThan(0)

    // second pull should return same results from cache
    const second = await pull(CLOUDFLARE_SKILLS_URL)
    expect(second.length).toBe(first.length)
    expect(second.sort()).toEqual(first.sort())

    // second pull should NOT increment download count
    expect(downloadCount).toBe(firstCount)
  })

  test("rejects insecure, loopback, and cross-origin redirects before download", async () => {
    const before = requestCount
    await expect(pull("http://93.184.216.34/skills/")).rejects.toThrow()
    await expect(pull("https://127.0.0.1/skills/")).rejects.toThrow()
    await expect(pull("https://10.0.0.1/skills/")).rejects.toThrow()
    expect(requestCount).toBe(before)

    let redirectCalls = 0
    const redirectPrivate = (async () =>
      (++redirectCalls,
      new Response(null, {
          status: 302,
          headers: { location: "https://169.254.169.254/latest/meta-data/" },
        }))) as Fetcher
    await expect(pull(CLOUDFLARE_SKILLS_URL, redirectPrivate)).rejects.toThrow()
    expect(redirectCalls).toBe(1)
    const redirectOtherOrigin = (async () =>
      (++redirectCalls,
      new Response(null, {
          status: 302,
          headers: { location: "https://93.184.216.35/skills/" },
        }))) as Fetcher
    await expect(pull(CLOUDFLARE_SKILLS_URL, redirectOtherOrigin)).rejects.toThrow()
    expect(redirectCalls).toBe(2)
  })

  test("rejects mixed DNS, traversal, unpinned, and oversized manifests", async () => {
    await expect(
      Discovery.assertRemoteSkillUrl(
        "https://mixed.example/skills/",
        "https://mixed.example",
        async () => [
          { address: "93.184.216.34", family: 4 },
          { address: "169.254.169.254", family: 4 },
        ],
      ),
    ).rejects.toThrow()

    const index = (skill: object) =>
      (async () =>
        new Response(JSON.stringify({ skills: [skill] }), {
          headers: { "content-type": "application/json" },
        })) as Fetcher
    await expect(
      pull(
        CLOUDFLARE_SKILLS_URL,
        index({
          name: "bad",
          revision: "1",
          files: [{ path: "../SKILL.md", sha256: "a".repeat(64) }],
        }),
      ),
    ).rejects.toThrow()
    await expect(
      pull(
        CLOUDFLARE_SKILLS_URL,
        index({ name: "bad", revision: "1", files: ["SKILL.md"] }),
      ),
    ).rejects.toThrow()
    await expect(
      pull(
        CLOUDFLARE_SKILLS_URL,
        index({
          name: "archive",
          revision: "1",
          files: [
            { path: "SKILL.md", sha256: "a".repeat(64) },
            { path: "payload.zip", sha256: "b".repeat(64) },
          ],
        }),
      ),
    ).rejects.toThrow()
    await expect(
      pull(
        CLOUDFLARE_SKILLS_URL,
        (async () =>
          new Response("{}", {
            headers: { "content-length": String(256 * 1024 + 1) },
          })) as Fetcher,
      ),
    ).rejects.toThrow()
  })

  test("writes a credential-free pinned provenance manifest", async () => {
    const dirs = await pull(CLOUDFLARE_SKILLS_URL)
    const manifest = JSON.parse(
      await Bun.file(path.join(dirs[0]!, ".remote-skill.json")).text(),
    )
    expect(manifest.origin).toBe("https://93.184.216.34")
    expect(manifest.base).toBe(CLOUDFLARE_SKILLS_URL)
    expect(manifest.revision).toBe("fixture-1")
    expect(manifest.files["SKILL.md"]).toMatch(/^[a-f0-9]{64}$/)
    expect(JSON.stringify(manifest)).not.toContain("@")
    const staged = await Bun.file(path.join(dirs[0]!, "SKILL.md")).text()
    expect(staged).toContain("status: draft")
    expect(staged).toContain("source: remote")
  })

  test("rejects userinfo, query, fragment, downgrade, and Windows drive paths before file fetch", async () => {
    const before = requestCount
    await expect(pull("https://user@93.184.216.34/skills/")).rejects.toThrow()
    await expect(pull("https://93.184.216.34/skills/?token=secret")).rejects.toThrow()
    await expect(pull("https://93.184.216.34/skills/#fragment")).rejects.toThrow()
    expect(requestCount).toBe(before)

    let calls = 0
    const windowsPath = (async () => {
      calls++
      return new Response(JSON.stringify({
        skills: [{
          name: "drive",
          revision: "pinned-1",
          files: [
            { path: "SKILL.md", sha256: "a".repeat(64) },
            { path: "C:relative.txt", sha256: "b".repeat(64) },
          ],
        }],
      }))
    }) as Fetcher
    await expect(pull(CLOUDFLARE_SKILLS_URL, windowsPath)).rejects.toThrow()
    expect(calls).toBe(1)

    let downgradeCalls = 0
    const downgrade = (async () => {
      downgradeCalls++
      return new Response(null, {
        status: 302,
        headers: { location: "http://93.184.216.34/skills/index.json" },
      })
    }) as Fetcher
    await expect(pull(CLOUDFLARE_SKILLS_URL, downgrade)).rejects.toThrow()
    expect(downgradeCalls).toBe(1)
  })

  test("accepts exact depth and file-count boundaries and rejects the first excess", async () => {
    const skill = new TextEncoder().encode(`---
name: boundaries
description: deterministic boundaries
---

# Procedure
- verify
`)
    const files = [
      { path: "SKILL.md", body: skill },
      { path: "a/b/c/d/guide.txt", body: new TextEncoder().encode("deep") },
      ...Array.from({ length: 62 }, (_, index) => ({
        path: `references/${index}.txt`,
        body: new TextEncoder().encode(String(index)),
      })),
    ]
    const index = {
      skills: [{
        name: "boundaries",
        revision: "pinned-boundary",
        files: files.map((file) => ({
          path: file.path,
          sha256: createHash("sha256").update(file.body).digest("hex"),
          size: file.body.byteLength,
        })),
      }],
    }
    const fetcher = (async (input) => {
      const url = new URL(input.toString())
      if (url.pathname.endsWith("/index.json")) {
        return new Response(JSON.stringify(index))
      }
      const prefix = "/boundaries/"
      const offset = url.pathname.lastIndexOf(prefix)
      const remotePath = url.pathname.slice(offset + prefix.length)
      const file = files.find((item) => item.path === remotePath)
      return file
        ? new Response(file.body)
        : new Response("missing", { status: 404 })
    }) as Fetcher
    const exactUrl = "https://93.184.216.34/boundary-exact/"
    expect((await pull(exactUrl, fetcher)).length).toBe(1)

    const tooDeep = structuredClone(index)
    tooDeep.skills[0]!.files[1]!.path = "a/b/c/d/e/guide.txt"
    await expect(
      pull(
        "https://93.184.216.34/boundary-depth/",
        (async () => new Response(JSON.stringify(tooDeep))) as Fetcher,
      ),
    ).rejects.toThrow()

    const tooMany = structuredClone(index)
    tooMany.skills[0]!.files.push({
      path: "references/excess.txt",
      sha256: "f".repeat(64),
      size: 1,
    })
    await expect(
      pull(
        "https://93.184.216.34/boundary-count/",
        (async () => new Response(JSON.stringify(tooMany))) as Fetcher,
      ),
    ).rejects.toThrow()
  })

  test("accepts the exact individual and total byte caps and rejects one total byte more", async () => {
    const makeSkill = (size: number) => {
      const prefix = new TextEncoder().encode(`---
name: byte-caps
description: exact byte caps
---

# Procedure
- verify
`)
      const out = new Uint8Array(size)
      out.set(prefix)
      out.fill("x".charCodeAt(0), prefix.byteLength)
      return out
    }
    const skill = makeSkill(400_000)
    const exactFiles = [
      { path: "SKILL.md", body: skill },
      ...Array.from({ length: 4 }, (_, index) => ({
        path: `references/${index}.txt`,
        body: new Uint8Array(400_000).fill(120),
      })),
    ]
    const makeFetcher = (basePath: string, files: typeof exactFiles) => {
      const index = {
        skills: [{
          name: "byte-caps",
          revision: `pinned-${basePath}`,
          files: files.map((file) => ({
            path: file.path,
            sha256: createHash("sha256").update(file.body).digest("hex"),
            size: file.body.byteLength,
          })),
        }],
      }
      return (async (input) => {
        const url = new URL(input.toString())
        if (url.pathname.endsWith("/index.json")) return new Response(JSON.stringify(index))
        const marker = "/byte-caps/"
        const remotePath = url.pathname.slice(url.pathname.lastIndexOf(marker) + marker.length)
        const file = files.find((item) => item.path === remotePath)
        return file ? new Response(file.body) : new Response("missing", { status: 404 })
      }) as Fetcher
    }

    const exactUrl = "https://93.184.216.34/cap-exact/"
    expect((await pull(exactUrl, makeFetcher("exact", exactFiles))).length).toBe(1)

    const excessFiles = [
      ...exactFiles,
      { path: "references/excess.txt", body: new Uint8Array([120]) },
    ]
    await expect(
      pull(
        "https://93.184.216.34/cap-excess/",
        makeFetcher("excess", excessFiles),
      ),
    ).rejects.toThrow()
  })

  test("an injected mid-bundle failure leaves no published or staged generation", async () => {
    const url = "https://93.184.216.34/injected-failure/"
    const base = new URL(url)
    const originKey = createHash("sha256").update(base.href).digest("hex").slice(0, 16)
    const parent = path.join(cacheDir, originKey, "generations")
    await rm(path.join(cacheDir, originKey), { recursive: true, force: true })
    const skill = new TextEncoder().encode(`---
name: injected
description: injected failure
status: published
---

# Procedure
- verify
`)
    const guide = new TextEncoder().encode("guide")
    const index = {
      skills: [{
        name: "injected",
        revision: "pinned-failure",
        files: [
          { path: "SKILL.md", sha256: createHash("sha256").update(skill).digest("hex"), size: skill.byteLength },
          { path: "references/guide.txt", sha256: createHash("sha256").update(guide).digest("hex"), size: guide.byteLength },
        ],
      }],
    }
    const fetcher = (async (input) => {
      const remote = new URL(input.toString())
      if (remote.pathname.endsWith("/index.json")) return new Response(JSON.stringify(index))
      if (remote.pathname.endsWith("/SKILL.md")) return new Response(skill)
      return new Response("injected", { status: 503 })
    }) as Fetcher

    await expect(pull(url, fetcher)).rejects.toThrow()
    const entries = await readdir(parent).catch(() => [])
    expect(entries).toEqual([])
  })

  test("concurrent readers see no generation until the complete draft bundle is atomically published", async () => {
    const url = "https://93.184.216.34/concurrent-visibility/"
    const base = new URL(url)
    const originKey = createHash("sha256").update(base.href).digest("hex").slice(0, 16)
    const parent = path.join(cacheDir, originKey, "generations")
    await rm(path.join(cacheDir, originKey), { recursive: true, force: true })
    const skill = new TextEncoder().encode(`---
name: visibility
description: atomic visibility
status: published
---

# Procedure
- verify
`)
    const guide = new TextEncoder().encode("guide")
    const index = {
      skills: [{
        name: "visibility",
        revision: "pinned-visibility",
        files: [
          { path: "SKILL.md", sha256: createHash("sha256").update(skill).digest("hex"), size: skill.byteLength },
          { path: "references/guide.txt", sha256: createHash("sha256").update(guide).digest("hex"), size: guide.byteLength },
        ],
      }],
    }
    let releaseGuide!: () => void
    let markGuide!: () => void
    const guideReleased = new Promise<void>((resolve) => {
      releaseGuide = resolve
    })
    const guideReached = new Promise<void>((resolve) => {
      markGuide = resolve
    })
    const fetcher = (async (input) => {
      const remote = new URL(input.toString())
      if (remote.pathname.endsWith("/index.json")) return new Response(JSON.stringify(index))
      if (remote.pathname.endsWith("/SKILL.md")) return new Response(skill)
      markGuide()
      await guideReleased
      return new Response(guide)
    }) as Fetcher

    const pending = pull(url, fetcher)
    await guideReached
    try {
      const visible = (await readdir(parent).catch(() => []))
        .filter((entry) => !entry.startsWith("."))
      expect(visible).toEqual([])
    } finally {
      releaseGuide()
    }
    const dirs = await pending
    expect(dirs).toHaveLength(1)
    const staged = await Bun.file(path.join(dirs[0]!, "SKILL.md")).text()
    expect(staged).toContain("status: draft")
    expect(staged).toContain("source_status: published")
    expect(await Bun.file(path.join(dirs[0]!, "references/guide.txt")).text()).toBe("guide")
    expect((await readdir(parent)).filter((entry) => entry.startsWith(".stage-"))).toEqual([])
  })
})
