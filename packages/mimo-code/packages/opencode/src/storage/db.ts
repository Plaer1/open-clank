import { type SQLiteBunDatabase } from "drizzle-orm/bun-sqlite"
import { currentSchema, currentObjects, currentStamp } from "./current-schema"
import { type SQLiteTransaction } from "drizzle-orm/sqlite-core"
export * from "drizzle-orm"
export type { SQLiteBunDatabase } from "drizzle-orm/bun-sqlite"
import { LocalContext } from "../util"
import { lazy } from "../util/lazy"
import { Global } from "../global"
import { Log } from "../util"
import { NamedError } from "@mimo-ai/shared/util/error"
import z from "zod"
import path from "path"
import { chmodSync } from "fs"
import { Flag } from "../flag/flag"
import { InstallationChannel } from "../installation/version"
import { InstanceState } from "@/effect"
import { iife } from "@/util/iife"
import { init } from "#db"


export const NotFoundError = NamedError.create(
  "NotFoundError",
  z.object({
    message: z.string(),
  }),
)

const log = Log.create({ service: "db" })

export function getChannelPath() {
  if (["latest", "beta", "prod"].includes(InstallationChannel) || Flag.MIMOCODE_DISABLE_CHANNEL_DB)
    return path.join(Global.Path.data, "mimocode.db")
  const safe = InstallationChannel.replace(/[^a-zA-Z0-9._-]/g, "-")
  return path.join(Global.Path.data, `mimocode-${safe}.db`)
}

export const Path = iife(() => {
  if (Flag.MIMOCODE_DB) {
    if (Flag.MIMOCODE_DB === ":memory:" || path.isAbsolute(Flag.MIMOCODE_DB)) return Flag.MIMOCODE_DB
    return path.join(Global.Path.data, Flag.MIMOCODE_DB)
  }
  return getChannelPath()
})

export type Transaction = SQLiteTransaction<"sync", void>

type Client = ReturnType<typeof init>

function admit(db: Client) {
  const count = db.all<{ count: number }>("SELECT count(*) AS count FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")[0]!.count
  if (count === 0) {
    db.run("BEGIN EXCLUSIVE")
    try { db.$client.exec(currentSchema); db.run("COMMIT") }
    catch (error) { db.run("ROLLBACK"); throw error }
    return
  }
  const refuse = (detail: string): never => { throw new Error(`${detail}; stop writers and retain a complete backup. Restore the matching release or prepare an offline conversion. A fresh installation needs a separate empty data directory. No runtime conversion was performed`) }
  for (const object of currentObjects) {
    const escaped = object.name.replaceAll("'", "''")
    const actual = db.all<{ sql: string }>(`SELECT sql FROM sqlite_schema WHERE name='${escaped}'`)[0]?.sql
    const normalize = (value: string) => value.split(/\s+/).join(" ").trim()
    if (object.type === "table") {
      const columns = db.all<Record<string,unknown>>(`PRAGMA table_info('${escaped}')`).map(({cid,...rest})=>rest).sort((a,b)=>String(a.name).localeCompare(String(b.name)))
      if (JSON.stringify(columns) !== JSON.stringify(object.columns)) refuse(`Incompatible Mimo table ${object.name}`)
    } else if (!actual || normalize(actual) !== normalize(object.sql)) refuse(`Incompatible Mimo schema object ${object.name}`)
  }
  const stamp = db.all<{ created_at: number }>("SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1")[0]?.created_at
  if (stamp !== currentStamp) refuse(`Incompatible Mimo schema journal ${stamp}`)
}

export const Client = lazy(() => {
  log.info("opening database", { path: Path })

  const db = init(Path)
  try { admit(db) } catch (error) { db.$client.close(); throw error }
  if (Path !== ":memory:") chmodSync(Path, 0o600)

  // The WAL pragma itself takes a lock. Install the busy handler first so
  // simultaneous engine connections do not fail before reaching title CAS.
  db.run("PRAGMA busy_timeout = 5000")
  db.run("PRAGMA journal_mode = WAL")
  db.run("PRAGMA synchronous = NORMAL")
  db.run("PRAGMA cache_size = -64000")
  db.run("PRAGMA foreign_keys = ON")
  db.run("PRAGMA wal_checkpoint(PASSIVE)")

  return db
})

export function close() {
  Client().$client.close()
  Client.reset()
}

export type TxOrDb = Transaction | Client

const ctx = LocalContext.create<{
  tx: TxOrDb
  effects: (() => void | Promise<void>)[]
}>("database")

export function use<T>(callback: (trx: TxOrDb) => T): T {
  try {
    return callback(ctx.use().tx)
  } catch (err) {
    if (err instanceof LocalContext.NotFound) {
      const effects: (() => void | Promise<void>)[] = []
      const result = ctx.provide({ effects, tx: Client() }, () => callback(Client()))
      for (const effect of effects) effect()
      return result
    }
    throw err
  }
}

export function effect(fn: () => any | Promise<any>) {
  const bound = InstanceState.bind(fn)
  try {
    ctx.use().effects.push(bound)
  } catch {
    bound()
  }
}

type NotPromise<T> = T extends Promise<any> ? never : T

export function transaction<T>(
  callback: (tx: TxOrDb) => NotPromise<T>,
  options?: {
    behavior?: "deferred" | "immediate" | "exclusive"
  },
): NotPromise<T> {
  try {
    return callback(ctx.use().tx)
  } catch (err) {
    if (err instanceof LocalContext.NotFound) {
      const effects: (() => void | Promise<void>)[] = []
      const txCallback = InstanceState.bind((tx: TxOrDb) => ctx.provide({ tx, effects }, () => callback(tx)))
      const result = Client().transaction(txCallback, { behavior: options?.behavior })
      for (const effect of effects) effect()
      return result as NotPromise<T>
    }
    throw err
  }
}
