import { describe, test, expect, spyOn } from "bun:test"
import { Effect, Layer, FileSystem } from "effect"
import { NodeFileSystem } from "@effect/platform-node"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { testEffect } from "../lib/effect"
import path from "path"
import * as NFS from "fs/promises"
import { spawn } from "child_process"
import { createServer } from "net"

const live = AppFileSystem.layer.pipe(Layer.provideMerge(NodeFileSystem.layer))
const { effect: it } = testEffect(live)

describe("AppFileSystem", () => {
  describe("isDir", () => {
    it(
      "returns true for directories",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        expect(yield* fs.isDir(tmp)).toBe(true)
      }),
    )

    it(
      "returns false for files",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "test.txt")
        yield* filesys.writeFileString(file, "hello")
        expect(yield* fs.isDir(file)).toBe(false)
      }),
    )

    it(
      "returns false for non-existent paths",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        expect(yield* fs.isDir("/tmp/nonexistent-" + Math.random())).toBe(false)
      }),
    )
  })

  describe("isFile", () => {
    it(
      "returns true for files",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "test.txt")
        yield* filesys.writeFileString(file, "hello")
        expect(yield* fs.isFile(file)).toBe(true)
      }),
    )

    it(
      "returns false for directories",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        expect(yield* fs.isFile(tmp)).toBe(false)
      }),
    )
  })

  describe("readJson / writeJson", () => {
    it(
      "round-trips JSON data",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "data.json")
        const data = { name: "test", count: 42, nested: { ok: true } }

        yield* fs.writeJson(file, data)
        const result = yield* fs.readJson(file)

        expect(result).toEqual(data)
      }),
    )
  })

  describe("ensureDir", () => {
    it(
      "creates nested directories",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const nested = path.join(tmp, "a", "b", "c")

        yield* fs.ensureDir(nested)

        const info = yield* filesys.stat(nested)
        expect(info.type).toBe("Directory")
      }),
    )

    it(
      "is idempotent",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const dir = path.join(tmp, "existing")
        yield* filesys.makeDirectory(dir)

        yield* fs.ensureDir(dir)

        const info = yield* filesys.stat(dir)
        expect(info.type).toBe("Directory")
      }),
    )
  })

  describe("writeWithDirs", () => {
    it(
      "creates parent directories if missing",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "deep", "nested", "file.txt")

        yield* fs.writeWithDirs(file, "hello")

        expect(yield* filesys.readFileString(file)).toBe("hello")
      }),
    )

    it(
      "writes directly when parent exists",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "direct.txt")

        yield* fs.writeWithDirs(file, "world")

        expect(yield* filesys.readFileString(file)).toBe("world")
      }),
    )

    it(
      "writes Uint8Array content",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "binary.bin")
        const content = new Uint8Array([0x00, 0x01, 0x02, 0x03])

        yield* fs.writeWithDirs(file, content)

        const result = yield* filesys.readFile(file)
        expect(new Uint8Array(result)).toEqual(content)
      }),
    )
  })

  describe("findUp", () => {
    it(
      "finds target in start directory",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "target.txt"), "found")

        const result = yield* fs.findUp("target.txt", tmp)
        expect(result).toEqual([path.join(tmp, "target.txt")])
      }),
    )

    it(
      "finds target in parent directories",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "marker"), "root")
        const child = path.join(tmp, "a", "b")
        yield* filesys.makeDirectory(child, { recursive: true })

        const result = yield* fs.findUp("marker", child, tmp)
        expect(result).toEqual([path.join(tmp, "marker")])
      }),
    )

    it(
      "returns empty array when not found",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const result = yield* fs.findUp("nonexistent", tmp, tmp)
        expect(result).toEqual([])
      }),
    )
  })

  describe("up", () => {
    it(
      "finds multiple targets walking up",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "a.txt"), "a")
        yield* filesys.writeFileString(path.join(tmp, "b.txt"), "b")
        const child = path.join(tmp, "sub")
        yield* filesys.makeDirectory(child)
        yield* filesys.writeFileString(path.join(child, "a.txt"), "a-child")

        const result = yield* fs.up({ targets: ["a.txt", "b.txt"], start: child, stop: tmp })

        expect(result).toContain(path.join(child, "a.txt"))
        expect(result).toContain(path.join(tmp, "a.txt"))
        expect(result).toContain(path.join(tmp, "b.txt"))
      }),
    )
  })

  describe("glob", () => {
    it(
      "finds files matching pattern",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "a.ts"), "a")
        yield* filesys.writeFileString(path.join(tmp, "b.ts"), "b")
        yield* filesys.writeFileString(path.join(tmp, "c.json"), "c")

        const result = yield* fs.glob("*.ts", { cwd: tmp })
        expect(result.sort()).toEqual(["a.ts", "b.ts"])
      }),
    )

    it(
      "supports absolute paths",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "file.txt"), "hello")

        const result = yield* fs.glob("*.txt", { cwd: tmp, absolute: true })
        expect(result).toEqual([path.join(tmp, "file.txt")])
      }),
    )
  })

  describe("globMatch", () => {
    it(
      "matches patterns",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        expect(fs.globMatch("*.ts", "foo.ts")).toBe(true)
        expect(fs.globMatch("*.ts", "foo.json")).toBe(false)
        expect(fs.globMatch("src/**", "src/a/b.ts")).toBe(true)
      }),
    )
  })

  describe("globUp", () => {
    it(
      "finds files walking up directories",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        yield* filesys.writeFileString(path.join(tmp, "root.md"), "root")
        const child = path.join(tmp, "a", "b")
        yield* filesys.makeDirectory(child, { recursive: true })
        yield* filesys.writeFileString(path.join(child, "leaf.md"), "leaf")

        const result = yield* fs.globUp("*.md", child, tmp)
        expect(result).toContain(path.join(child, "leaf.md"))
        expect(result).toContain(path.join(tmp, "root.md"))
      }),
    )
  })

  describe("built-in passthrough", () => {
    it(
      "exists works",
      Effect.gen(function* () {
        yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "exists.txt")
        yield* filesys.writeFileString(file, "yes")

        expect(yield* filesys.exists(file)).toBe(true)
        expect(yield* filesys.exists(file + ".nope")).toBe(false)
      }),
    )

    it(
      "remove works",
      Effect.gen(function* () {
        yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "delete-me.txt")
        yield* filesys.writeFileString(file, "bye")

        yield* filesys.remove(file)

        expect(yield* filesys.exists(file)).toBe(false)
      }),
    )
  })

  describe("atomic contract", () => {
    it(
      "canonicalizes a missing target beneath a symlinked parent",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const root = path.join(tmp, "root")
        const outside = path.join(tmp, "outside")
        yield* filesys.makeDirectory(root)
        yield* filesys.makeDirectory(outside)
        yield* Effect.promise(() => NFS.symlink(outside, path.join(root, "link")))

        const canonical = yield* fs.canonicalTarget(path.join(root, "link", "new.txt"))
        expect(canonical).toBe(path.join(outside, "new.txt"))
        expect(AppFileSystem.contains(root, canonical)).toBe(false)
      }),
    )

    it(
      "sends one authenticated Lore action for a multi-target agent batch",
      Effect.gen(function* () {
        // Bun's macOS AF_UNIX client closes before delivering a peer response;
        // the same hook is exercised end-to-end on Linux in the qualification matrix.
        if (process.platform === "darwin") return
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const first = path.join(tmp, "first.txt")
        const second = path.join(tmp, "second.txt")
        const socketPath = path.join("/tmp", `mimo-history-${Date.now()}.sock`)
        yield* filesys.writeFileString(first, "before")
        const serverScript = `
import socket, sys
path = sys.argv[1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)
log = open(path + ".log", "wb")
for _ in range(3):
    conn, _ = server.accept()
    body = b""
    while b"\\n" not in body:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    log.write(body)
    log.flush()
    conn.sendall(b"{}")
    conn.close()
log.close()
server.close()
`
        const historyServer = spawn("python3", ["-c", serverScript, socketPath], { stdio: ["ignore", "ignore", "pipe"] })
        yield* Effect.promise(async () => {
          for (let attempt = 0; attempt < 100; attempt++) {
            if (await NFS.stat(socketPath).then(() => true).catch(() => false)) return
            await new Promise((resolve) => setTimeout(resolve, 10))
          }
          throw new Error("history fixture socket did not start")
        })
        const history: AppFileSystem.HistoryContext = {
          actorId: "agent-1",
          accountId: "acct-1",
          workspaceId: "workspace-1",
          workspaceRoot: tmp,
          socketPath,
          token: "opaque-token",
          status: { status: "paused", history_status: "paused", capture_phase: "unavailable", durable: false, coverage: "NoCapture" },
        }
        try {
          yield* fs.atomicBatch([
            { path: first, content: "after", actionId: "mimo-batch-1", history },
            { path: second, content: "created", actionId: "mimo-batch-1", history },
          ])
          expect(history.status.status).toBe("complete")
        } finally {
          if (historyServer.exitCode === null) historyServer.kill()
          yield* Effect.promise(() => new Promise<void>((resolve) => {
            if (historyServer.exitCode !== null) resolve()
            else historyServer.once("close", () => resolve())
          }))
        }
        const frameBytes = yield* Effect.promise(() => NFS.readFile(socketPath + ".log"))
        const frames = frameBytes.toString("utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line))
        expect(yield* filesys.readFileString(first)).toBe("after")
        expect(yield* filesys.readFileString(second)).toBe("created")
        expect(frames).toHaveLength(3)
        expect(frames.map((frame) => Object.keys(frame)[0])).toEqual(["Prepare", "RecordLive", "Complete"])
        expect(frames.every((frame) => {
          const envelope = frame[Object.keys(frame)[0]!].envelope
          return envelope.auth.actor_id === "agent-1" && envelope.auth.account_id === "acct-1" && envelope.auth.token === "opaque-token" && envelope.action_id === "mimo-batch-1"
        })).toBe(true)
        const request = frames[0].Prepare.envelope.request
        expect(request.operation).toBe("replace")
        expect(request.modified_resource_ids).toHaveLength(2)
        expect(request.coverage.kind).toBe("ObservedAfterOnly")
      }),
    )

    it(
      "preserves BOM, newlines, mode and rejects a stale fingerprint",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const file = path.join(tmp, "script.txt")
        yield* Effect.promise(() => NFS.writeFile(file, Buffer.from("\ufeffalpha\r\nbeta\r\n", "utf8")))
        yield* filesys.chmod(file, 0o751)
        const snapshot = yield* fs.readTextSnapshot(file)

        yield* fs.atomicWrite({
          path: file,
          content: AppFileSystem.encodeText(snapshot, snapshot.text.replace("beta", "BETA")),
          expectedFingerprint: snapshot.fingerprint,
          mode: snapshot.mode,
        })
        expect(yield* Effect.promise(() => NFS.readFile(file))).toEqual(Buffer.from("\ufeffalpha\r\nBETA\r\n", "utf8"))
        expect((yield* Effect.promise(() => NFS.stat(file))).mode & 0o777).toBe(0o751)

        const fresh = yield* fs.readTextSnapshot(file)
        yield* Effect.promise(() => NFS.appendFile(file, "external\r\n"))
        const conflict = yield* Effect.flip(
          fs.atomicWrite({
            path: file,
            content: "ours",
            expectedFingerprint: fresh.fingerprint,
          }),
        )
        expect(conflict).toBeInstanceOf(AppFileSystem.AtomicConflict)
      }),
    )

    it(
      "validates the whole batch before changing the first file",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const first = path.join(tmp, "first.txt")
        const second = path.join(tmp, "second.txt")
        yield* filesys.writeFileString(first, "first-old")
        yield* filesys.writeFileString(second, "second-old")
        const one = yield* fs.readTextSnapshot(first)
        const two = yield* fs.readTextSnapshot(second)
        yield* filesys.writeFileString(second, "external")

        yield* Effect.flip(
          fs.atomicBatch([
            { path: first, content: "first-new", expectedFingerprint: one.fingerprint },
            { path: second, content: "second-new", expectedFingerprint: two.fingerprint },
          ]),
        )
        expect(yield* filesys.readFileString(first)).toBe("first-old")
        expect(yield* filesys.readFileString(second)).toBe("external")
      }),
    )

    it(
      "round-trips UTF-16 byte order and refuses binary text",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const little = path.join(tmp, "little.txt")
        const big = path.join(tmp, "big.txt")
        const binary = path.join(tmp, "binary.txt")
        const oddLittle = path.join(tmp, "odd-little.txt")
        const oddBig = path.join(tmp, "odd-big.txt")
        yield* Effect.promise(() => NFS.writeFile(little, Buffer.from([0xff, 0xfe, 0x61, 0x00, 0x0a, 0x00])))
        yield* Effect.promise(() => NFS.writeFile(big, Buffer.from([0xfe, 0xff, 0x00, 0x61, 0x00, 0x0a])))
        yield* Effect.promise(() => NFS.writeFile(binary, Buffer.from([0x61, 0x00, 0x62])))
        yield* Effect.promise(() => NFS.writeFile(oddLittle, Buffer.from([0xff, 0xfe, 0x61])))
        yield* Effect.promise(() => NFS.writeFile(oddBig, Buffer.from([0xfe, 0xff, 0x00])))

        for (const file of [little, big]) {
          const snapshot = yield* fs.readTextSnapshot(file)
          yield* fs.atomicWrite({
            path: file,
            content: AppFileSystem.encodeText(snapshot, snapshot.text.replace("a", "A")),
            expectedFingerprint: snapshot.fingerprint,
            mode: snapshot.mode,
          })
        }

        expect(yield* Effect.promise(() => NFS.readFile(little))).toEqual(
          Buffer.from([0xff, 0xfe, 0x41, 0x00, 0x0a, 0x00]),
        )
        expect(yield* Effect.promise(() => NFS.readFile(big))).toEqual(
          Buffer.from([0xfe, 0xff, 0x00, 0x41, 0x00, 0x0a]),
        )
        yield* Effect.flip(fs.readTextSnapshot(binary))
        yield* Effect.flip(fs.readTextSnapshot(oddLittle))
        yield* Effect.flip(fs.readTextSnapshot(oddBig))
      }),
    )

    it(
      "rejects canonical aliases in one batch",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const target = path.join(tmp, "target.txt")
        const alias = path.join(tmp, "alias.txt")
        yield* filesys.writeFileString(target, "old")
        yield* Effect.promise(() => NFS.symlink(target, alias))

        const error = yield* Effect.flip(
          fs.atomicBatch([
            { path: target, content: "one" },
            { path: alias, content: "two" },
          ]),
        )
        expect(String(error)).toContain("duplicate target")
        expect(yield* filesys.readFileString(target)).toBe("old")
      }),
    )

    it(
      "updates a symlink target without replacing the link",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const target = path.join(tmp, "target.txt")
        const alias = path.join(tmp, "alias.txt")
        yield* filesys.writeFileString(target, "old")
        yield* Effect.promise(() => NFS.symlink(target, alias))
        const snapshot = yield* fs.readTextSnapshot(alias)

        yield* fs.atomicWrite({
          path: alias,
          content: "new",
          expectedFingerprint: snapshot.fingerprint,
        })

        expect((yield* Effect.promise(() => NFS.lstat(alias))).isSymbolicLink()).toBe(true)
        expect(yield* filesys.readFileString(target)).toBe("new")
      }),
    )

    it(
      "rejects a parent symlink swap before commit",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const inside = path.join(tmp, "inside")
        const outside = path.join(tmp, "outside")
        const alias = path.join(tmp, "alias")
        yield* filesys.makeDirectory(inside)
        yield* filesys.makeDirectory(outside)
        yield* Effect.promise(() => NFS.symlink(inside, alias))
        const requested = path.join(alias, "new.txt")
        const realMkdir = NFS.mkdir.bind(NFS)
        let swapped = false
        const mkdir = spyOn(NFS, "mkdir").mockImplementation(
          (async (target, options) => {
            const result = await realMkdir(target, options)
            if (!swapped && target === inside) {
              swapped = true
              await NFS.unlink(alias)
              await NFS.symlink(outside, alias)
            }
            return result
          }) as typeof NFS.mkdir,
        )

        try {
          const failure = yield* Effect.flip(
            fs.atomicWrite({
              path: requested,
              content: "blocked",
              requireMissing: true,
            }),
          )
          expect(failure).toBeInstanceOf(AppFileSystem.AtomicConflict)
          expect(yield* filesys.exists(path.join(inside, "new.txt"))).toBe(false)
          expect(yield* filesys.exists(path.join(outside, "new.txt"))).toBe(false)
        } finally {
          mkdir.mockRestore()
        }
      }),
    )

    it(
      "never overwrites an external create race",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const target = path.join(tmp, "new.txt")
        const realLink = NFS.link.bind(NFS)
        const link = spyOn(NFS, "link").mockImplementation(async (source, destination) => {
          await NFS.writeFile(target, "external")
          return realLink(source, destination)
        })

        try {
          const failure = yield* Effect.flip(
            fs.atomicWrite({
              path: target,
              content: "ours",
              requireMissing: true,
            }),
          )
          expect(failure).toBeInstanceOf(AppFileSystem.AtomicConflict)
          expect(yield* filesys.readFileString(target)).toBe("external")
        } finally {
          link.mockRestore()
        }
      }),
    )

    it(
      "rolls back a failure at every commit position",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()

        for (const failAt of [1, 2, 3]) {
          const dir = path.join(tmp, `case-${failAt}`)
          yield* filesys.makeDirectory(dir)
          const targets = [0, 1, 2].map((index) => path.join(dir, `file-${index}.txt`))
          for (const [index, target] of targets.entries()) {
            yield* filesys.writeFileString(target, `old-${index}`)
          }
          const realRename = NFS.rename.bind(NFS)
          let installs = 0
          const rename = spyOn(NFS, "rename").mockImplementation(async (source, destination) => {
            if (String(source).includes(".tmp.")) {
              installs += 1
              if (installs === failAt) throw new Error(`install ${failAt} failed`)
            }
            return realRename(source, destination)
          })

          try {
            yield* Effect.flip(
              fs.atomicBatch(
                targets.map((target, index) => ({
                  path: target,
                  content: `new-${index}`,
                })),
              ),
            )
            for (const [index, target] of targets.entries()) {
              expect(yield* filesys.readFileString(target)).toBe(`old-${index}`)
            }
          } finally {
            rename.mockRestore()
          }
        }
      }),
    )

    it(
      "retains a recoverable backup when rollback itself fails",
      Effect.gen(function* () {
        const fs = yield* AppFileSystem.Service
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const first = path.join(tmp, "first.txt")
        const second = path.join(tmp, "second.txt")
        yield* filesys.writeFileString(first, "first-old")
        yield* filesys.writeFileString(second, "second-old")
        const realRename = NFS.rename.bind(NFS)
        const rename = spyOn(NFS, "rename").mockImplementation(async (source, destination) => {
          if (String(source).includes(".tmp.") && destination === second) {
            throw new Error("commit failed")
          }
          if (String(source).includes(".bak.") && destination === first) {
            throw new Error("rollback failed")
          }
          return realRename(source, destination)
        })

        try {
          const failure = yield* Effect.flip(
            fs.atomicBatch([
              { path: first, content: "first-new" },
              { path: second, content: "second-new" },
            ]),
          )
          expect(failure).toBeInstanceOf(AppFileSystem.AtomicRollbackError)
          const entries = yield* filesys.readDirectory(tmp)
          const backup = entries.find((name) => name.startsWith(".first.txt.bak."))
          expect(backup).toBeDefined()
          expect(yield* filesys.readFileString(path.join(tmp, backup!))).toBe("first-old")
          expect(yield* filesys.readFileString(second)).toBe("second-old")
        } finally {
          rename.mockRestore()
        }
      }),
    )
  })

  describe("pure helpers", () => {
    it(
      "schedules disjoint resources concurrently and serializes canonical overlaps",
      Effect.gen(function* () {
        const filesys = yield* FileSystem.FileSystem
        const tmp = yield* filesys.makeTempDirectoryScoped()
        const firstPath = path.join(tmp, "first.txt")
        const secondPath = path.join(tmp, "second.txt")
        const aliasPath = path.join(tmp, "alias.txt")
        yield* filesys.writeFileString(firstPath, "first")
        yield* filesys.writeFileString(secondPath, "second")
        yield* Effect.promise(() => NFS.symlink(firstPath, aliasPath))

        const first = yield* Effect.promise(() =>
          AppFileSystem.acquireFileResources({ reads: [firstPath] }),
        )
        const overlap = AppFileSystem.acquireFileResources({ writes: [firstPath] })
        const overlapReady = yield* Effect.promise(() =>
          Promise.race([
            overlap.then(() => true),
            new Promise<false>((resolve) => queueMicrotask(() => resolve(false))),
          ]),
        )
        expect(overlapReady).toBe(false)

        const disjoint = AppFileSystem.acquireFileResources({ writes: [secondPath] })
        const disjointLease = yield* Effect.promise(() => disjoint)
        disjointLease.release()

        const alias = AppFileSystem.acquireFileResources({ writes: [aliasPath] })
        const aliasReady = yield* Effect.promise(() =>
          Promise.race([
            alias.then(() => true),
            new Promise<false>((resolve) => queueMicrotask(() => resolve(false))),
          ]),
        )
        expect(aliasReady).toBe(false)

        first.release()
        const overlapLease = yield* Effect.promise(() => overlap)
        overlapLease.release()
        const aliasLease = yield* Effect.promise(() => alias)
        aliasLease.release()

        const held = yield* Effect.promise(() =>
          AppFileSystem.acquireFileResources({ writes: [firstPath] }),
        )
        const controller = new AbortController()
        const cancelled = AppFileSystem.acquireFileResources(
          { reads: [firstPath] },
          controller.signal,
        )
        controller.abort()
        const wasCancelled = yield* Effect.promise(() =>
          cancelled.then(
            () => false,
            () => true,
          ),
        )
        expect(wasCancelled).toBe(true)
        held.release()
        const afterCancellation = yield* Effect.promise(() =>
          AppFileSystem.acquireFileResources({ writes: [firstPath] }),
        )
        afterCancellation.release()
      }),
    )

    test("mimeType returns correct types", () => {
      expect(AppFileSystem.mimeType("file.json")).toBe("application/json")
      expect(AppFileSystem.mimeType("image.png")).toBe("image/png")
      expect(AppFileSystem.mimeType("unknown.qzx")).toBe("application/octet-stream")
    })

    test("contains checks path containment", () => {
      expect(AppFileSystem.contains("/a/b", "/a/b/c")).toBe(true)
      expect(AppFileSystem.contains("/a/b", "/a/c")).toBe(false)
    })

    test("overlaps detects overlapping paths", () => {
      expect(AppFileSystem.overlaps("/a/b", "/a/b/c")).toBe(true)
      expect(AppFileSystem.overlaps("/a/b/c", "/a/b")).toBe(true)
      expect(AppFileSystem.overlaps("/a", "/b")).toBe(false)
    })
  })
})
