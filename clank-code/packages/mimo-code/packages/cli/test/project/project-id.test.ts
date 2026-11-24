import { describe, expect, test } from "bun:test"
import * as fs from "fs/promises"
import * as path from "path"
import * as os from "os"
import { randomUUID } from "node:crypto"
import { execFileSync, spawnSync } from "node:child_process"
import { resolveMainGitDir, resolveProjectId } from "../../src/project/project-id"
import { ProjectID } from "../../src/project/schema"

async function directory() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "mimo-pid-"))
  const realpath = await fs.realpath(dir)
  return {
    path: realpath,
    [Symbol.asyncDispose]: () => fs.rm(realpath, { recursive: true }),
  }
}

function withCeilings<T>(value: string | undefined, fn: () => T): T {
  const previous = process.env.GIT_CEILING_DIRECTORIES
  if (value === undefined) delete process.env.GIT_CEILING_DIRECTORIES
  else process.env.GIT_CEILING_DIRECTORIES = value
  try {
    return fn()
  } finally {
    if (previous === undefined) delete process.env.GIT_CEILING_DIRECTORIES
    else process.env.GIT_CEILING_DIRECTORIES = previous
  }
}

function git(dir: string, ...args: string[]) {
  return execFileSync("git", args, { cwd: dir, encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"] }).trim()
}

function gitCommonDir(dir: string) {
  const result = spawnSync("git", ["rev-parse", "--git-common-dir"], {
    cwd: dir,
    encoding: "utf-8",
    env: { ...process.env },
  })
  if (result.error) throw result.error
  if (result.status !== 0) return null
  return path.resolve(dir, result.stdout.trim())
}

function expectDiscovery(dir: string, expected: string | null) {
  expect(gitCommonDir(dir)).toBe(expected)
  expect(resolveMainGitDir(dir)).toBe(expected)
}

describe("resolveMainGitDir", () => {
  test("returns .git directory for main repo", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    expectDiscovery(tmp.path, path.join(tmp.path, ".git"))
  })

  test("returns null for non-git directory within an explicit ceiling", async () => {
    await using tmp = await directory()
    const child = path.join(tmp.path, "child")
    await fs.mkdir(child)
    withCeilings(tmp.path, () => expectDiscovery(child, null))
  })

  test("keeps ancestor discovery when ceilings are unset or empty", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const child = path.join(tmp.path, "nested", "child")
    await fs.mkdir(child, { recursive: true })
    for (const ceiling of [undefined, ""]) {
      withCeilings(ceiling, () => expectDiscovery(child, path.join(tmp.path, ".git")))
    }
  })

  test("handles multiple ceiling paths with the platform delimiter", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const boundary = path.join(tmp.path, "boundary")
    const child = path.join(boundary, "child")
    const sibling = path.join(tmp.path, "sibling")
    await fs.mkdir(child, { recursive: true })
    await fs.mkdir(sibling)
    for (const ceilings of [[sibling, boundary], [boundary, sibling]]) {
      withCeilings(ceilings.join(path.delimiter), () => expectDiscovery(child, null))
    }
    withCeilings(sibling, () => expectDiscovery(child, path.join(tmp.path, ".git")))
  })

  test("checks .git when starting at the ceiling but excludes it from upward discovery", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const child = path.join(tmp.path, "child")
    await fs.mkdir(child)
    withCeilings(tmp.path, () => {
      expectDiscovery(tmp.path, path.join(tmp.path, ".git"))
      expectDiscovery(child, null)
    })
  })

  test("uses Git canonicalization for symlink ceilings and its empty-entry marker", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const boundary = path.join(tmp.path, "boundary")
    const child = path.join(boundary, "child")
    const link = path.join(tmp.path, "link")
    await fs.mkdir(child, { recursive: true })
    await fs.symlink(boundary, link, process.platform === "win32" ? "junction" : "dir")
    withCeilings(link, () => expectDiscovery(child, null))
    withCeilings(path.delimiter + link, () => expectDiscovery(child, path.join(tmp.path, ".git")))
    withCeilings(path.join(boundary, ".") + path.sep, () => expectDiscovery(child, null))
    withCeilings("boundary", () => expectDiscovery(child, path.join(tmp.path, ".git")))
  })

  test("resolves a real worktree to main .git with and without a ceiling", async () => {
    await using tmp = await directory()
    const main = path.join(tmp.path, "main")
    const worktree = path.join(tmp.path, "worktree")
    await fs.mkdir(main)
    git(main, "init")
    git(main, "worktree", "add", "--orphan", "-b", "test-worktree", worktree)
    const child = path.join(worktree, "child")
    await fs.mkdir(child)
    expect((await fs.stat(path.join(worktree, ".git"))).isFile()).toBe(true)
    withCeilings(undefined, () => expectDiscovery(child, path.join(main, ".git")))
    withCeilings(tmp.path, () => expectDiscovery(child, path.join(main, ".git")))
    withCeilings(worktree, () => {
      expectDiscovery(worktree, path.join(main, ".git"))
      expectDiscovery(child, null)
    })
  })
})

describe("resolveProjectId", () => {
  test("generates new UUID for git project without cache", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const id = resolveProjectId(tmp.path)
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
    const cached = await fs.readFile(path.join(tmp.path, ".git", "openclank-project-id"), "utf-8")
    expect(cached.trim()).toBe(id)
  })

  test("reuses cached UUID for git project", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const id = randomUUID()
    await fs.writeFile(path.join(tmp.path, ".git", "openclank-project-id"), id)
    expect(resolveProjectId(tmp.path)).toBe(ProjectID.make(id))
  })

  test("generates and reuses a local UUID without inheriting an ancestor beyond the ceiling", async () => {
    await using tmp = await directory()
    git(tmp.path, "init")
    const ancestorId = resolveProjectId(tmp.path)
    const child = path.join(tmp.path, "child")
    await fs.mkdir(child)
    const id = withCeilings(tmp.path, () => {
      expectDiscovery(child, null)
      const id = resolveProjectId(child)
      expect(resolveProjectId(child)).toBe(id)
      return id
    })
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
    expect(id).not.toBe(ancestorId)
    expect((await fs.readFile(path.join(child, ".openclank-project-id"), "utf-8")).trim()).toBe(id)
    expect((await fs.readFile(path.join(tmp.path, ".git", "openclank-project-id"), "utf-8")).trim()).toBe(ancestorId)
  })

  test("shares UUID between a real worktree and its main repository", async () => {
    await using tmp = await directory()
    const main = path.join(tmp.path, "main")
    const worktree = path.join(tmp.path, "worktree")
    await fs.mkdir(main)
    git(main, "init")
    git(main, "worktree", "add", "--orphan", "-b", "test-worktree", worktree)
    withCeilings(tmp.path, () => expect(resolveProjectId(worktree)).toBe(resolveProjectId(main)))
    expect(await fs.stat(path.join(main, ".git", "openclank-project-id"))).toBeDefined()
    await fs.stat(path.join(worktree, ".openclank-project-id")).then(
      () => { throw new Error("worktree has a local project ID") },
      (error: NodeJS.ErrnoException) => expect(error.code).toBe("ENOENT"),
    )
  })
})
