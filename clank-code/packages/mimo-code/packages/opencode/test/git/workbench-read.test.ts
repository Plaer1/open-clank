import { test, expect } from "bun:test"
import { mkdtemp, readFile, rm, writeFile, access } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { ManagedRuntime } from "effect"
import { Git } from "../../src/git"

test("workbench reads ignore hostile Git environment and never invoke clean filters", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "p06-git-"))
  const git = (...args: string[]) => {
    const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe", env: { PATH: process.env.PATH, HOME: root } })
    if (result.exitCode !== 0) throw new Error("fixture setup failed")
  }
  const runtime = ManagedRuntime.make(Git.defaultLayer)
  const prior = process.env.GIT_DIR
  try {
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    await writeFile(path.join(root, "file.txt"), "original\n")
    for (let i = 0; i < 80; i++) await writeFile(path.join(root, `item-${String(i).padStart(3, "0")}.txt`), `item ${i}\n`)
    git("add", ".")
    git("commit", "-qm", "fixture")
    const marker = path.join(root, "filter-ran")
    git("config", "filter.marker.clean", `touch ${marker}; cat`)
    await writeFile(path.join(root, ".gitattributes"), "file.txt filter=marker\n")
    await writeFile(path.join(root, "file.txt"), "changed\n")
    process.env.GIT_DIR = path.join(root, "hostile-nonexistent")
    const read = (operation: "root" | "index" | "tree" | "others" | "status") => runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation })))
    const resolved = await read("root")
    expect(resolved.exitCode).toBe(0)
    expect(resolved.text().split("\n")[0]).toBe(root)
    const inventory = await read("index")
    expect(inventory.exitCode).toBe(0)
    const prefix = Git.inventoryPrefix(inventory.stdout)
    expect(prefix.truncated).toBe(true)
    expect(prefix.data.includes(Buffer.from("item-079.txt"))).toBe(false)
    const selected = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation: "indexEntry", path: "item-079.txt" })))
    expect(selected.text()).toContain("item-079.txt")
    const tree = await read("tree")
    expect(tree.exitCode).toBe(0)
    const oid = tree.text().match(/^100644 blob ([0-9a-f]+)\t/)?.[1]
    expect(oid).toBeTruthy()
    const blob = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation: "blob", path: oid! })))
    expect(blob.text()).toBe("original\n")
    expect((await read("others")).exitCode).toBe(0)
    for (const operation of ["branches", "log", "worktrees"] as const) {
      const result = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation })))
      expect(result.exitCode).toBe(0)
      if (operation === "branches") expect(result.stdout.toString()).toContain("refs/heads/")
      if (operation === "log") expect(result.stdout.toString()).toContain("fixture")
      if (operation === "worktrees") expect(result.stdout.toString()).toContain(`worktree ${root}\0`)
    }
    expect((await read("status")).exitCode).not.toBe(0)
    expect(await access(marker).then(() => true, () => false)).toBe(false)
    expect(await readFile(path.join(root, "file.txt"), "utf8")).toBe("changed\n")
  } finally {
    if (prior === undefined) delete process.env.GIT_DIR; else process.env.GIT_DIR = prior
    await runtime.dispose()
    await rm(root, { recursive: true, force: true })
  }
}, 20000)
