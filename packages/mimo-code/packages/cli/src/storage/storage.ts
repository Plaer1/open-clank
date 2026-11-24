import { Log } from "../util"
import path from "path"
import { Global } from "../global"
import { NamedError } from "@mimo-ai/shared/util/error"
import z from "zod"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Effect, Layer, RcMap, Context, TxReentrantLock } from "effect"

const log = Log.create({ service: "storage" })

export const NotFoundError = NamedError.create(
  "NotFoundError",
  z.object({
    message: z.string(),
  }),
)

export type Error = AppFileSystem.Error | InstanceType<typeof NotFoundError>

export interface Interface {
  readonly remove: (key: string[]) => Effect.Effect<void, AppFileSystem.Error>
  readonly read: <T>(key: string[]) => Effect.Effect<T, Error>
  readonly update: <T>(key: string[], fn: (draft: T) => void) => Effect.Effect<T, Error>
  readonly write: <T>(key: string[], content: T) => Effect.Effect<void, AppFileSystem.Error>
  readonly list: (prefix: string[]) => Effect.Effect<string[][], AppFileSystem.Error>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Storage") {}

function file(dir: string, key: string[]) {
  return path.join(dir, ...key) + ".json"
}

function missing(err: unknown) {
  if (!err || typeof err !== "object") return false
  if ("code" in err && err.code === "ENOENT") return true
  if ("reason" in err && err.reason && typeof err.reason === "object" && "_tag" in err.reason) {
    return err.reason._tag === "NotFound"
  }
  return false
}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service
    const locks = yield* RcMap.make({
      lookup: () => TxReentrantLock.make(),
      idleTimeToLive: 0,
    })
    const state = yield* Effect.cached(
      Effect.gen(function* () {
        const dir = path.join(Global.Path.data, "storage")
        const marker = yield* fs.readFileString(path.join(dir, "migration")).pipe(Effect.orElseSucceed(() => ""))
        const legacyProjects = yield* fs.isDir(path.resolve(dir, "../project"))
        const entries = yield* fs.glob("*", { cwd: dir, include: "all" }).pipe(Effect.orElseSucceed(() => []))
        if ((legacyProjects || entries.length > 0) && marker.trim() !== "2") {
          return yield* Effect.die(new Error("Legacy JSON storage; run .clanker/tools/native/mimo with an explicit layout target"))
        }
        if (!legacyProjects && entries.length === 0) yield* fs.writeWithDirs(path.join(dir, "migration"), "2")
        return { dir }
      }),
    )

    const fail = (target: string): Effect.Effect<never, InstanceType<typeof NotFoundError>> =>
      Effect.fail(new NotFoundError({ message: `Resource not found: ${target}` }))

    const wrap = <A>(target: string, body: Effect.Effect<A, AppFileSystem.Error>) =>
      body.pipe(Effect.catchIf(missing, () => fail(target)))

    const writeJson = Effect.fnUntraced(function* (target: string, content: unknown) {
      yield* fs.writeWithDirs(target, JSON.stringify(content, null, 2))
    })

    const withResolved = <A, E>(
      key: string[],
      fn: (target: string, rw: TxReentrantLock.TxReentrantLock) => Effect.Effect<A, E>,
    ): Effect.Effect<A, E | AppFileSystem.Error> =>
      Effect.scoped(
        Effect.gen(function* () {
          const target = file((yield* state).dir, key)
          return yield* fn(target, yield* RcMap.get(locks, target))
        }),
      )

    const remove: Interface["remove"] = Effect.fn("Storage.remove")(function* (key: string[]) {
      yield* withResolved(key, (target, rw) =>
        TxReentrantLock.withWriteLock(rw, fs.remove(target).pipe(Effect.catchIf(missing, () => Effect.void))),
      )
    })

    const read: Interface["read"] = <T>(key: string[]) =>
      Effect.gen(function* () {
        const value = yield* withResolved(key, (target, rw) =>
          TxReentrantLock.withReadLock(rw, wrap(target, fs.readJson(target))),
        )
        return value as T
      })

    const update: Interface["update"] = <T>(key: string[], fn: (draft: T) => void) =>
      Effect.gen(function* () {
        const value = yield* withResolved(key, (target, rw) =>
          TxReentrantLock.withWriteLock(
            rw,
            Effect.gen(function* () {
              const content = yield* wrap(target, fs.readJson(target))
              fn(content as T)
              yield* writeJson(target, content)
              return content
            }),
          ),
        )
        return value as T
      })

    const write: Interface["write"] = (key: string[], content: unknown) =>
      Effect.gen(function* () {
        yield* withResolved(key, (target, rw) => TxReentrantLock.withWriteLock(rw, writeJson(target, content)))
      })

    const list: Interface["list"] = Effect.fn("Storage.list")(function* (prefix: string[]) {
      const dir = (yield* state).dir
      const cwd = path.join(dir, ...prefix)
      const result = yield* fs
        .glob("**/*", {
          cwd,
          include: "file",
        })
        .pipe(Effect.catch(() => Effect.succeed<string[]>([])))
      return result
        .map((x) => [...prefix, ...x.slice(0, -5).split(path.sep)])
        .toSorted((a, b) => a.join("/").localeCompare(b.join("/")))
    })

    return Service.of({
      remove,
      read,
      update,
      write,
      list,
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(AppFileSystem.defaultLayer))
