import { describe, expect } from "bun:test"
import path from "path"
import { Effect, Exit, Layer } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Git } from "../../src/git"
import { Global } from "../../src/global"
import { Storage } from "../../src/storage"
import { tmpdirScoped } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const dir = path.join(Global.Path.data, "storage")

const it = testEffect(Layer.mergeAll(Storage.defaultLayer, AppFileSystem.defaultLayer, CrossSpawnSpawner.defaultLayer))

const scope = Effect.fnUntraced(function* () {
  const root = ["storage_test", crypto.randomUUID()]
  const fs = yield* AppFileSystem.Service
  const svc = yield* Storage.Service
  yield* Effect.addFinalizer(() =>
    fs.remove(path.join(dir, ...root), { recursive: true, force: true }).pipe(Effect.ignore),
  )
  return { root, svc }
})

// remap(root) rewrites any path under Global.Path.data to live under `root` instead.
// Used by remappedFs to build an AppFileSystem that Storage thinks is the real global
// data dir but actually targets a tmp dir — letting migration tests stage legacy layouts.
// NOTE: only the 6 methods below are intercepted. If Storage starts using a different
// AppFileSystem method that touches Global.Path.data, add it here.
function remap(root: string, file: string) {
  if (file === Global.Path.data) return root
  if (file.startsWith(Global.Path.data + path.sep)) return path.join(root, path.relative(Global.Path.data, file))
  return file
}

function remappedFs(root: string) {
  return Layer.effect(
    AppFileSystem.Service,
    Effect.gen(function* () {
      const fs = yield* AppFileSystem.Service
      return AppFileSystem.Service.of({
        ...fs,
        isDir: (file) => fs.isDir(remap(root, file)),
        readJson: (file) => fs.readJson(remap(root, file)),
        writeWithDirs: (file, content, mode) => fs.writeWithDirs(remap(root, file), content, mode),
        readFileString: (file) => fs.readFileString(remap(root, file)),
        remove: (file) => fs.remove(remap(root, file)),
        glob: (pattern, options) =>
          fs.glob(pattern, options?.cwd ? { ...options, cwd: remap(root, options.cwd) } : options),
      })
    }),
  ).pipe(Layer.provide(AppFileSystem.defaultLayer))
}

// Layer.fresh forces a new Storage instance — without it, Effect's in-test layer cache
// returns the outer testEffect's Storage (which uses the real AppFileSystem), not a new
// one built on top of remappedFs.
const remappedStorage = (root: string) =>
  Layer.fresh(Storage.layer.pipe(Layer.provide(remappedFs(root)), Layer.provide(Git.defaultLayer)))

describe("Storage", () => {
  it.live("round-trips JSON content", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const key = [...root, "session_diff", "roundtrip"]
      const value = [{ file: "a.ts", additions: 2, deletions: 1 }]

      yield* svc.write(key, value)
      expect(yield* svc.read<typeof value>(key)).toEqual(value)
    }),
  )

  it.live("maps missing reads to NotFoundError", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const exit = yield* svc.read([...root, "missing", "value"]).pipe(Effect.exit)
      expect(Exit.isFailure(exit)).toBe(true)
    }),
  )

  it.live("update on missing key throws NotFoundError", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const exit = yield* svc
        .update<{ value: number }>([...root, "missing", "key"], (draft) => {
          draft.value += 1
        })
        .pipe(Effect.exit)
      expect(Exit.isFailure(exit)).toBe(true)
    }),
  )

  it.live("write overwrites existing value", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const key = [...root, "overwrite", "test"]

      yield* svc.write<{ v: number }>(key, { v: 1 })
      yield* svc.write<{ v: number }>(key, { v: 2 })

      expect(yield* svc.read<{ v: number }>(key)).toEqual({ v: 2 })
    }),
  )

  it.live("remove on missing key is a no-op", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      yield* svc.remove([...root, "nonexistent", "key"])
    }),
  )

  it.live("list on missing prefix returns empty", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      expect(yield* svc.list([...root, "nonexistent"])).toEqual([])
    }),
  )

  it.live("serializes concurrent updates for the same key", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const key = [...root, "counter", "shared"]

      yield* svc.write(key, { value: 0 })

      yield* Effect.all(
        Array.from({ length: 25 }, () =>
          svc.update<{ value: number }>(key, (draft) => {
            draft.value += 1
          }),
        ),
        { concurrency: "unbounded" },
      )

      expect(yield* svc.read<{ value: number }>(key)).toEqual({ value: 25 })
    }),
  )

  it.live("concurrent reads do not block each other", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const key = [...root, "concurrent", "reads"]

      yield* svc.write(key, { ok: true })

      const results = yield* Effect.all(
        Array.from({ length: 10 }, () => svc.read(key)),
        { concurrency: "unbounded" },
      )

      expect(results).toHaveLength(10)
      for (const r of results) expect(r).toEqual({ ok: true })
    }),
  )

  it.live("nested keys create deep paths", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const key = [...root, "a", "b", "c", "deep"]

      yield* svc.write<{ nested: boolean }>(key, { nested: true })

      expect(yield* svc.read<{ nested: boolean }>(key)).toEqual({ nested: true })
      expect(yield* svc.list([...root, "a"])).toEqual([key])
    }),
  )

  it.live("lists and removes stored entries", () =>
    Effect.gen(function* () {
      const { root, svc } = yield* scope()
      const a = [...root, "list", "a"]
      const b = [...root, "list", "b"]
      const prefix = [...root, "list"]

      yield* svc.write(b, { value: 2 })
      yield* svc.write(a, { value: 1 })

      expect(yield* svc.list(prefix)).toEqual([a, b])

      yield* svc.remove(a)

      expect(yield* svc.list(prefix)).toEqual([b])
      const exit = yield* svc.read(a).pipe(Effect.exit)
      expect(Exit.isFailure(exit)).toBe(true)
    }),
  )

  // Open Clank removed the in-engine legacy JSON migrations (storage.ts): a legacy layout is refused, never converted.
  it.live("refuses a legacy JSON layout instead of migrating it", () =>
    Effect.gen(function* () {
      const fs = yield* AppFileSystem.Service
      const tmp = yield* tmpdirScoped()
      const storage = path.join(tmp, "storage")
      yield* fs.writeWithDirs(path.join(storage, "session", "legacy.json"), "{}")

      const exit = yield* Effect.gen(function* () {
        const svc = yield* Storage.Service
        return yield* svc.list(["session"])
      }).pipe(Effect.provide(remappedStorage(tmp)), Effect.exit)
      expect(Exit.isFailure(exit)).toBe(true)

      const marker = yield* fs.access(path.join(storage, "migration")).pipe(Effect.exit)
      expect(Exit.isFailure(marker)).toBe(true)
    }),
  )
})
