import { describe, expect, test } from "bun:test"
import path from "path"
import fs from "fs/promises"
import { Global } from "../../src/global"
import { SessionID } from "../../src/session/schema"
import { ProjectID } from "../../src/project/schema"
import { notesPath, globalMemoryPath, memoryPath, assertCurrentProjectMemory } from "../../src/session/checkpoint-paths"

async function sameFile(a: string, b: string) {
  const [aStat, bStat] = await Promise.all([
    fs.stat(a).catch(() => undefined),
    fs.stat(b).catch(() => undefined),
  ])
  if (!aStat || !bStat) return false
  return aStat.dev === bStat.dev && aStat.ino === bStat.ino
}

describe("notesPath (F14)", () => {
  test("resolves to <data>/memory/sessions/<sid>/notes.md", () => {
    const sid = SessionID.make("ses_test_xyz")
    expect(notesPath(sid)).toBe(path.join(Global.Path.data, "memory", "sessions", sid, "notes.md"))
  })
})

describe("globalMemoryPath", () => {
  test("returns <data>/memory/global/MEMORY.md", () => {
    expect(globalMemoryPath()).toBe(
      path.join(Global.Path.data, "memory", "global", "MEMORY.md"),
    )
  })
})

describe("current project memory admission", () => {
  test("refuses a legacy filename without renaming it", async () => {
    const pid = ProjectID.make(`p_test_${Date.now()}`)
    const dir = path.dirname(memoryPath(pid))
    await fs.mkdir(dir, {recursive:true})
    await fs.writeFile(path.join(dir,"memory.md"),"legacy")
    await expect(assertCurrentProjectMemory(pid)).rejects.toThrow("Legacy project memory")
    expect(await fs.readdir(dir)).toEqual(["memory.md"])
    await fs.rm(dir,{recursive:true,force:true})
  })
})
