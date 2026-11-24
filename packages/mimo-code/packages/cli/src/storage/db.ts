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
import { queuedGuardColumn, queuedGuardSQL } from "./queued-source-guards"
import profileLayout from "./openclank-profile-layout.json"
import { parseLaunchWitness } from "../workflow/launch-witness"
import { startIndexMigration, stopIndexMigration } from "../history/migration"
import { startMemoryIndexMigration, stopMemoryIndexMigration } from "../memory/migration"


export const NotFoundError = NamedError.create(
  "NotFoundError",
  z.object({
    message: z.string(),
  }),
)

const log = Log.create({ service: "db" })

export function getChannelPath() {
  if (["latest", "beta", "prod"].includes(InstallationChannel) || Flag.OPENCLANK_DISABLE_CHANNEL_DB)
    return path.join(Global.Path.data, "openclank.db")
  const safe = InstallationChannel.replace(/[^a-zA-Z0-9._-]/g, "-")
  return path.join(Global.Path.data, `openclank-${safe}.db`)
}

export const Path = iife(() => {
  if (Flag.OPENCLANK_DB) {
    if (Flag.OPENCLANK_DB === ":memory:" || path.isAbsolute(Flag.OPENCLANK_DB)) return Flag.OPENCLANK_DB
    return path.join(Global.Path.data, Flag.OPENCLANK_DB)
  }
  return getChannelPath()
})

if (Flag.OPEN_CLANK_PRIVATE_RUNTIME) {
  if (process.env.OPEN_CLANK_MANAGED !== "1" || Path !== ":memory:")
    throw new Error("private managed runtime requires an in-memory database")
}

export type Transaction = SQLiteTransaction<"sync", void>

type Client = ReturnType<typeof init>

const queuedSourceColumns = ["source_proof", "source_message_id", "source_part_id"] as const
/** Additive profile applies only to a fresh empty DB or an offline-prepared copy. */
function createQueuedSourceColumns(db: Client) {
  // Producer-only remains the default until the complete guarded lifecycle is ready.
  for (const column of queuedSourceColumns) db.run(`ALTER TABLE user_input_receipt ADD COLUMN ${column} TEXT`)
}

export function admit(db: Client) {
  const count = db.all<{ count: number }>("SELECT count(*) AS count FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")[0]!.count
  if (count === 0) {
    db.run("BEGIN EXCLUSIVE")
    try { db.$client.exec(currentSchema); createQueuedSourceColumns(db); db.run("ALTER TABLE part ADD COLUMN openclank_view_decision TEXT"); db.run("ALTER TABLE part ADD COLUMN openclank_step_recovery TEXT"); for (const stmt of [...profileLayout.labelDDL, ...profileLayout.labelOwnerDDL, ...profileLayout.labelObjects, ...profileLayout.labelOwnerObjects, ...profileLayout.retirementDDL, ...profileLayout.retirementObjects, ...profileLayout.selectedInputDDL, ...profileLayout.selectedInputObjects, ...profileLayout.workflowWitnessDDL, ...profileLayout.workflowWitnessObjects]) db.run(stmt); db.run("COMMIT") }
    catch (error) { db.run("ROLLBACK"); throw error }
    return
  }
  const refuse = (detail: string): never => { throw new Error(`${detail}; stop writers and retain a complete backup. Restore the matching release or prepare an offline conversion. A fresh installation needs a separate empty data directory. No runtime conversion was performed`) }
  // Upstream's history index rebuild (history/migration-batch.ts) drops the three FTS triggers while it runs and
  // recreates them without backticks. Both states belong to this layout; nothing else is relaxed.
  const ftsTriggers = ["history_fts_ai", "history_fts_ad", "history_fts_au"]
  const rebuilding = (() => { try { return db.all("SELECT 1 FROM history_index_migration WHERE phase != 'done' LIMIT 1").length > 0 } catch { return false } })()
  let hasLabelSession = false, hasLabelActor = false, hasLabelOwner = false, hasGuarded = false, retiredSession = false, retiredActor = false, legacyIngress = false, selectedInput = false, workflowWitness = false
  for (const object of currentObjects) {
    const escaped = object.name.replaceAll("'", "''")
    const actual = db.all<{ sql: string }>(`SELECT sql FROM sqlite_schema WHERE name='${escaped}'`)[0]?.sql
    const normalize = (value: string) => value.split(/\s+/).join(" ").trim()
    if (object.type === "table") {
      const rawColumns = db.all<Record<string,unknown>>(`PRAGMA table_info('${escaped}')`).map(({cid,...rest})=>rest).sort((a,b)=>String(a.name).localeCompare(String(b.name)))
      const marker = rawColumns.find(column=>column.name===profileLayout.retirementColumn)
      if (marker && (object.name !== 'session' && object.name !== 'actor_registry' || JSON.stringify(marker)!==JSON.stringify({name:profileLayout.retirementColumn,type:'TEXT',notnull:0,dflt_value:null,pk:0}))) refuse(`Incompatible retirement column ${object.name}`)
      if (marker && object.name === 'session') retiredSession = true
      if (marker && object.name === 'actor_registry') retiredActor = true
      const selected = rawColumns.find(column => column.name === profileLayout.selectedInputColumn)
      if (selected && (object.name !== "user_input_receipt" || JSON.stringify(selected) !== JSON.stringify({name:profileLayout.selectedInputColumn,type:"INTEGER",notnull:0,dflt_value:null,pk:0}))) refuse(`Incompatible selected input column ${object.name}`)
      if (selected) selectedInput = true
      const workflow = rawColumns.find(column => column.name === profileLayout.workflowWitnessColumn)
      if (workflow && (object.name !== "workflow_run" || JSON.stringify(workflow) !== JSON.stringify({name:profileLayout.workflowWitnessColumn,type:"TEXT",notnull:0,dflt_value:null,pk:0}))) refuse(`Incompatible workflow witness column ${object.name}`)
      if (workflow) workflowWitness = true
      const columns = rawColumns.filter(column => column !== marker && column !== selected && column !== workflow)
      const ingressMissing = object.name === "user_input_receipt" && !columns.some(column => column.name === "first_ingress_nonce")
      if (ingressMissing) legacyIngress = true
      const expectedColumns = ingressMissing ? object.columns?.filter(column => column.name !== "first_ingress_nonce") : object.columns
      const legacy = JSON.stringify(columns) === JSON.stringify(expectedColumns)
      const queued = object.name === "user_input_receipt" && JSON.stringify(columns) === JSON.stringify([
        ...(expectedColumns ?? []),
        ...queuedSourceColumns.map((name) => ({name,type:"TEXT",notnull:0,dflt_value:null,pk:0})),
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const guarded = object.name === "user_input_receipt" && JSON.stringify(columns) === JSON.stringify([
        ...(expectedColumns ?? []),
        ...[...queuedSourceColumns, queuedGuardColumn].map((name) => ({name,type:"TEXT",notnull:0,dflt_value:null,pk:0})),
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const viewDecision = object.name === "part" && JSON.stringify(columns) === JSON.stringify([
        ...(object.columns ?? []), {name:"openclank_view_decision",type:"TEXT",notnull:0,dflt_value:null,pk:0},
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const stepRecovery = object.name === "part" && JSON.stringify(columns) === JSON.stringify([
        ...(object.columns ?? []), {name:"openclank_view_decision",type:"TEXT",notnull:0,dflt_value:null,pk:0},
        {name:profileLayout.stepRecoveryColumn,type:"TEXT",notnull:0,dflt_value:null,pk:0},
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const labelSession = object.name === "session" && JSON.stringify(columns) === JSON.stringify([
        ...(object.columns ?? []), {name:"next_display_ordinal",type:"INTEGER",notnull:1,dflt_value:"1",pk:0},
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const labelActor = object.name === "actor_registry" && JSON.stringify(columns) === JSON.stringify([
        ...(object.columns ?? []), ...profileLayout.labelColumns.map((name) => ({name,type:name === "display_ordinal" || name === "display_revision" ? "INTEGER" : "TEXT",notnull:0,dflt_value:null,pk:0})),
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      const labelActorV2 = object.name === "actor_registry" && JSON.stringify(columns) === JSON.stringify([
        ...(object.columns ?? []), ...profileLayout.labelColumns.map((name) => ({name,type:name === "display_ordinal" || name === "display_revision" ? "INTEGER" : "TEXT",notnull:0,dflt_value:null,pk:0})),
        {name:profileLayout.labelOwnerColumn,type:"TEXT",notnull:0,dflt_value:null,pk:0},
      ].sort((a,b)=>String(a.name).localeCompare(String(b.name))))
      if (labelSession) hasLabelSession = true
      if (labelActor || labelActorV2) hasLabelActor = true
      if (labelActorV2) hasLabelOwner = true
      if (guarded) hasGuarded = true
      if (!legacy && !queued && !guarded && !viewDecision && !stepRecovery && !labelSession && !labelActor && !labelActorV2) refuse(`Incompatible Mimo table ${object.name}`)
      if (guarded) for (const expected of queuedGuardSQL) {
        const name = expected.split(" ")[2]!
        const kind = expected.startsWith('CREATE INDEX ') ? 'index' : 'trigger'
        const actualGuard = db.all<{sql:string}>(`SELECT sql FROM sqlite_schema WHERE type='${kind}' AND name='${name}'`)[0]?.sql
        if (!actualGuard || normalize(actualGuard) !== normalize(expected)) refuse(`Incompatible queued source guard ${name}`)
      }
    } else if (ftsTriggers.includes(object.name)
      ? (actual ? normalize(actual).replaceAll("`", "") !== normalize(object.sql).replaceAll("`", "") : !rebuilding)
      : (!actual || normalize(actual) !== normalize(object.sql))) refuse(`Incompatible Mimo schema object ${object.name}`)
  }
  if (hasLabelSession !== hasLabelActor) refuse("Incomplete actor label profile")
  if (retiredSession !== retiredActor) refuse("Incomplete directory retirement profile")
  const normalizeProfileSQL = (value: string) => value.split(/\s+/).join(" ").trim()
  const checkObjects = (prefix: string, expected: readonly string[], required: boolean) => {
    const objects = db.all<{name:string,sql:string}>(`SELECT name,sql FROM sqlite_schema WHERE name LIKE '${prefix}%' ORDER BY name`)
    if (objects.length !== (required ? expected.length : 0)) refuse(`Incompatible ${prefix} profile`)
    if (!required) return
    for (const stmt of expected) {
      const name = /^CREATE (?:UNIQUE )?(?:INDEX|TRIGGER) (\S+)/.exec(stmt)?.[1]
      if (!name) refuse(`Invalid ${prefix} definition`)
      const actual = objects.find((item) => item.name === name)
      if (!actual || normalizeProfileSQL(actual.sql) !== normalizeProfileSQL(stmt)) refuse(`Incompatible ${prefix} object ${name}`)
    }
  }
  checkObjects("queued_guard_", queuedGuardSQL, hasGuarded)
  checkObjects("o03_", [...profileLayout.labelObjects, ...(hasLabelOwner ? profileLayout.labelOwnerObjects : [])], hasLabelActor)
  checkObjects("p06_", profileLayout.retirementObjects, retiredSession)
  checkObjects("o02_selected_input_", profileLayout.selectedInputObjects, selectedInput)
  checkObjects("o05_workflow_witness_", profileLayout.workflowWitnessObjects, workflowWitness)
  if (workflowWitness) for (const row of db.$client.query("SELECT session_id,id,openclank_launch_witness FROM workflow_run WHERE openclank_launch_witness IS NOT NULL").iterate() as Iterable<{session_id:string;id:string;openclank_launch_witness:string}>) {
    if (!parseLaunchWitness(row.openclank_launch_witness,row.session_id,row.id)) refuse("Invalid workflow witness data")
  }
  if (selectedInput && db.all(`SELECT 1 FROM user_input_receipt WHERE selected_execution_revision IS NOT NULL AND (typeof(selected_execution_revision)!='integer' OR selected_execution_revision<0 OR selected_execution_revision>9007199254740991 OR selected_execution_revision != execution_revision) LIMIT 1`).length) refuse("Invalid selected input execution data")
  if (retiredSession) for (const table of ['session','actor_registry']) {
    const invalid=db.all(`SELECT 1 FROM ${table} WHERE openclank_directory_retired_by IS NOT NULL AND (typeof(openclank_directory_retired_by)!='text' OR length(openclank_directory_retired_by)!=64 OR openclank_directory_retired_by GLOB '*[^0-9a-f]*') LIMIT 1`)
    if (invalid.length) refuse(`Invalid directory retirement data in ${table}`)
  }
  const expectedNames = new Set(currentObjects.map((object) => object.name))
  for (const kind of ["memory", "history"]) for (const suffix of ["data", "idx", "docsize", "config"]) expectedNames.add(`${kind}_fts_idx_${suffix}`)
  for (const stmt of [...(hasGuarded ? queuedGuardSQL : []), ...(hasLabelActor ? [...profileLayout.labelObjects, ...(hasLabelOwner ? profileLayout.labelOwnerObjects : [])] : []), ...(retiredSession ? profileLayout.retirementObjects : []), ...(selectedInput ? profileLayout.selectedInputObjects : []), ...(workflowWitness ? profileLayout.workflowWitnessObjects : [])]) {
    const name = /^CREATE (?:UNIQUE )?(?:INDEX|TRIGGER) (\S+)/.exec(stmt)?.[1]
    if (!name) refuse("Invalid optional profile object")
    expectedNames.add(name!)
  }
  const actualNames = db.all<{name:string}>("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
  if (rebuilding) for (const name of ftsTriggers) if (!actualNames.some((item) => item.name === name)) expectedNames.delete(name)
  if (actualNames.length !== expectedNames.size || actualNames.some(({name}) => !expectedNames.has(name))) refuse("Unknown Mimo schema object")
  const stamp = db.all<{ created_at: number }>("SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1")[0]?.created_at
  if (stamp !== (legacyIngress ? 1793751060000 : currentStamp)) refuse(`Incompatible Mimo schema journal ${stamp}`)
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

  // Upstream's index rebuild jobs replace our history backfill (M2-A10). Both are inert without a pending state row,
  // and a fresh store has none.
  if (!Flag.OPENCLANK_SKIP_MIGRATIONS) {
    startMemoryIndexMigration(db)
    startIndexMigration(db)
  }

  return db
})

export function close() {
  stopMemoryIndexMigration(Client())
  stopIndexMigration(Client())
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
