import { afterAll, describe, expect, test } from "bun:test"
import crypto from "node:crypto"
import path from "node:path"
import fs from "node:fs/promises"
import { Effect, Layer, ManagedRuntime } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Agent } from "../../src/agent/agent"
import { Global } from "../../src/global"
import { Instance } from "../../src/project/instance"
import { SessionID, MessageID } from "../../src/session/schema"
import { ManageFilesTool } from "../../src/tool/manage-files"
import { Tool, Truncate } from "../../src/tool"
import { tmpdir } from "../fixture/fixture"

const runtime = ManagedRuntime.make(
  Layer.mergeAll(AppFileSystem.defaultLayer, Truncate.defaultLayer, Agent.defaultLayer),
)

const ctx = {
  sessionID: SessionID.make("ses_manage_files"),
  messageID: MessageID.make(""),
  callID: "",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [],
  metadata: () => Effect.void,
  ask: () => Effect.void,
}

async function execute(args: Tool.InferParameters<typeof ManageFilesTool>) {
  return Effect.runPromise((await init()).execute(args, ctx))
}

async function init() {
  const info = await runtime.runPromise(ManageFilesTool)
  return runtime.runPromise(info.init())
}

afterAll(async () => {
  await runtime.dispose()
  await Instance.disposeAll()
})

describe("tool.manage_files", () => {
  test("managed sessions fail closed without an owner", async () => {
    await using fixture = await tmpdir()
    const previousManaged = process.env.OPEN_CLANK_MANAGED
    const previousOwner = process.env.OPEN_CLANK_OWNER
    const previousMemoryOwner = process.env.FM_OWNER
    process.env.OPEN_CLANK_MANAGED = "1"
    delete process.env.OPEN_CLANK_OWNER
    delete process.env.FM_OWNER
    try {
      await Instance.provide({
        directory: fixture.path,
        fn: () => expect(execute({ action: "list_trash" })).rejects.toThrow("authenticated owner"),
      })
    } finally {
      if (previousManaged === undefined) delete process.env.OPEN_CLANK_MANAGED
      else process.env.OPEN_CLANK_MANAGED = previousManaged
      if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
      else process.env.OPEN_CLANK_OWNER = previousOwner
      if (previousMemoryOwner === undefined) delete process.env.FM_OWNER
      else process.env.FM_OWNER = previousMemoryOwner
    }
  })

  test("delete is recoverable and trash listing is paginated", async () => {
    await using fixture = await tmpdir()
    const previousData = Global.Path.data
    const previousOwner = process.env.OPEN_CLANK_OWNER
    Global.Path.data = path.join(fixture.path, ".data")
    process.env.OPEN_CLANK_OWNER = "alice"
    try {
      await Instance.provide({
        directory: fixture.path,
        fn: async () => {
          const source = path.join(fixture.path, "note.txt")
          await fs.writeFile(source, "recover me")

          const deleted = await execute({ action: "delete", path: source })
          expect(deleted.metadata.recoverable).toBe(true)
          expect(await Bun.file(source).exists()).toBe(false)

          const ownerKey = crypto.createHash("sha256").update("alice").digest("hex").slice(0, 20)
          const workspaceKey = crypto
            .createHash("sha256")
            .update(AppFileSystem.resolve(fixture.path))
            .digest("hex")
            .slice(0, 20)
          const trash = path.join(Global.Path.data, "file-trash", ownerKey, workspaceKey)
          await fs.writeFile(path.join(trash, `${"f".repeat(24)}.json`), "{broken")

          const listed = await execute({ action: "list_trash", cursor: 0, limit: 1 })
          expect(listed.metadata.items).toHaveLength(1)
          expect(listed.metadata.page.total).toBe(1)

          process.env.OPEN_CLANK_OWNER = "bob"
          const hidden = await execute({ action: "list_trash" })
          expect(hidden.metadata.items).toHaveLength(0)
          await expect(execute({ action: "restore", trash_id: deleted.metadata.trash_id })).rejects.toThrow()

          process.env.OPEN_CLANK_OWNER = "alice"
          await execute({ action: "restore", trash_id: deleted.metadata.trash_id })
          expect(await Bun.file(source).text()).toBe("recover me")
        },
      })
    } finally {
      Global.Path.data = previousData
      if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
      else process.env.OPEN_CLANK_OWNER = previousOwner
    }
  }, 30_000)

  test("move never overwrites an existing destination", async () => {
    await using fixture = await tmpdir()
    await Instance.provide({
      directory: fixture.path,
      fn: async () => {
        const source = path.join(fixture.path, "source.txt")
        const destination = path.join(fixture.path, "destination.txt")
        await fs.writeFile(source, "source")
        await fs.writeFile(destination, "destination")

        await expect(execute({ action: "move", path: source, destination })).rejects.toThrow(
          "destination already exists",
        )
        expect(await Bun.file(source).text()).toBe("source")
        expect(await Bun.file(destination).text()).toBe("destination")
      },
    })
  })
})
