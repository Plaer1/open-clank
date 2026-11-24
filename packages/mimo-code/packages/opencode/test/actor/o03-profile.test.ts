import { expect, test } from "bun:test"
import { Database as SQLite } from "bun:sqlite"
import { spawnSync } from "node:child_process"
import { join } from "node:path"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { ActorRegistryTable } from "../../src/actor/actor.sql"
import { Database, and, eq, sql } from "../../src/storage"
import { currentSchema } from "../../src/storage/current-schema"
import layout from "../../src/storage/openclank-profile-layout.json"
import { tmpdir } from "../fixture/fixture"

test("fresh and guarded prepared profiles allocate durable session ordinals", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({ directory:tmp.path, fn:async()=>{
    const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
    try {
      const session=await rt.runPromise(Session.Service.use(s=>s.create()))
      const registry=await rt.runPromise(ActorRegistry.Service.use(s=>Effect.succeed(s)))
      const input={sessionID:session.id,mode:"subagent" as const,agent:"worker",description:"Review the plan",contextMode:"none" as const,background:false,lifecycle:"persistent" as const}
      const [a,b]=await Promise.all([rt.runPromise(registry.registerAllocated(input)),rt.runPromise(registry.registerAllocated(input))])
      const rows=()=>Database.use(db=>db.all<{id:string,n:number,label:string}>(sql`SELECT actor_id AS id,display_ordinal AS n,display_label AS label FROM actor_registry WHERE session_id=${session.id} AND display_ordinal IS NOT NULL ORDER BY display_ordinal`))
      expect(rows().map(x=>x.n)).toEqual([1,2]);expect(rows()[0]!.label).toContain("Review the plan")
      Database.use(db=>db.delete(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id,session.id),eq(ActorRegistryTable.actor_id,b.actorID))).run())
      const c=await rt.runPromise(registry.registerAllocated(input));expect(rows().map(x=>x.n)).toEqual([1,3])
      const before=rows().find(x=>x.id===a.actorID)!
      await expect(rt.runPromise(registry.register({...input,actorID:a.actorID}))).rejects.toBeDefined()
      const d=await rt.runPromise(registry.registerAllocated(input));expect(rows().find(x=>x.id===d.actorID)?.n).toBe(4)
      await rt.runPromise(registry.beginExecution(session.id,a.actorID,"pending",0))
      expect(rows().find(x=>x.id===a.actorID)).toEqual(before)
      // Corrupt the greatest row only in the disposable fixture. Admission is
      // schema-only; allocation validates the scoped indexed row at use.
      Database.use(db=>{db.run(sql.raw("DROP TRIGGER o03_actor_label_update"));db.run(sql`UPDATE actor_registry SET display_label=NULL WHERE session_id=${session.id} AND actor_id=${d.actorID}`);db.run(sql.raw(layout.labelObjects.find(x=>x.startsWith("CREATE TRIGGER o03_actor_label_update"))!))})
      await expect(rt.runPromise(registry.registerAllocated(input))).rejects.toBeDefined()
      const counter=Database.use(db=>db.get<{n:number}>(sql`SELECT next_display_ordinal AS n FROM session WHERE id=${session.id}`))
      expect(counter?.n).toBe(5)
      Database.use(db=>{db.run(sql.raw("DROP TRIGGER o03_actor_label_update"));db.run(sql`UPDATE actor_registry SET display_label='Clank #4' WHERE session_id=${session.id} AND actor_id=${d.actorID}`);db.run(sql.raw(layout.labelObjects.find(x=>x.startsWith("CREATE TRIGGER o03_actor_label_update"))!))})
      expect(c.actorID).toBeTruthy()
    } finally {await rt.dispose()}
  }})
  const src=join(tmp.path,"source.sqlite"),backup=join(tmp.path,"backup.sqlite"),dst=join(tmp.path,"shadow.sqlite")
  const db=new SQLite(src);db.exec(currentSchema)
  for (const name of layout.queuedColumns) db.exec(`ALTER TABLE user_input_receipt ADD COLUMN ${name} TEXT`)
  db.exec("ALTER TABLE user_input_receipt ADD COLUMN source_enrollment TEXT")
  db.exec("ALTER TABLE session_prefix_snapshot ADD COLUMN provenance TEXT")
  for (const stmt of layout.queuedGuards) db.exec(stmt)
  db.exec("INSERT INTO project(id,worktree,sandboxes,time_created,time_updated) VALUES ('p','/tmp','[]',1,1)")
  db.exec("INSERT INTO session(id,project_id,slug,directory,title,version,time_created,time_updated) VALUES ('s','p','s','/tmp','s','1',1,1)")
  db.exec("INSERT INTO actor_registry(session_id,actor_id,mode,status,lifecycle,agent,description,context_mode,background,last_turn_time,turn_count,instance_id,time_created,time_updated) VALUES ('s','old','subagent','idle','persistent','worker','legacy','none',0,1,0,'i',1,1)")
  db.close()
  const result=spawnSync(process.env.O03_PYTHON!,[join(process.env.O03_REPO!,"scripts/prepare_mimo_profiles.py"),src,backup,dst,"--writers-stopped"],{encoding:"utf8"})
  expect(result.status).toBe(0);expect(result.stderr).toBe("")
  const old=new SQLite(src,{readonly:true}),copy=new SQLite(dst,{readonly:true})
  expect(old.query("PRAGMA table_info(actor_registry)").all().some((x:any)=>x.name==='display_ordinal')).toBe(false)
  expect(copy.query("SELECT display_ordinal FROM actor_registry WHERE actor_id='old'").get()).toEqual({display_ordinal:null})
  expect(copy.query("SELECT count(*) AS n FROM sqlite_schema WHERE name LIKE 'queued_guard_%'").get()).toEqual({n:layout.queuedGuards.length})
  old.close();copy.close()
  const writable=new SQLite(dst)
  writable.exec("UPDATE session SET next_display_ordinal=next_display_ordinal+1 WHERE id='s'")
  writable.exec("INSERT INTO actor_registry(session_id,actor_id,mode,status,lifecycle,agent,description,context_mode,background,last_turn_time,turn_count,instance_id,time_created,time_updated,display_ordinal,display_label,display_revision,display_origin) VALUES ('s','new','subagent','idle','persistent','worker','new','none',0,1,0,'i',1,1,1,'Clank #1',0,'deterministic')")
  writable.close()
  const again=spawnSync(process.env.O03_PYTHON!,[join(process.env.O03_REPO!,"scripts/prepare_mimo_profiles.py"),dst,join(tmp.path,"backup2"),join(tmp.path,"shadow2"),"--writers-stopped"],{encoding:"utf8"})
  expect(again.status).toBe(0)
  const corrupt=new SQLite(dst);corrupt.exec("DROP TRIGGER o03_actor_label_update");corrupt.exec("UPDATE actor_registry SET display_origin=NULL WHERE actor_id='new'");corrupt.exec(layout.labelObjects.find(s=>s.startsWith('CREATE TRIGGER o03_actor_label_update'))!);corrupt.close()
  const rejectBad=spawnSync(process.env.O03_PYTHON!,[join(process.env.O03_REPO!,"scripts/prepare_mimo_profiles.py"),dst,join(tmp.path,"backup3"),join(tmp.path,"shadow3"),"--writers-stopped"],{encoding:"utf8"})
  expect(rejectBad.status).not.toBe(0)
  const fix=new SQLite(dst);fix.exec("DROP TRIGGER o03_actor_label_update");fix.exec("UPDATE actor_registry SET display_origin='deterministic' WHERE actor_id='new'");fix.exec(layout.labelObjects.find(s=>s.startsWith('CREATE TRIGGER o03_actor_label_update'))!);fix.close()
  const reopen=spawnSync(process.env.O03_BUN!,["-e",`import {Database} from './src/storage'; Database.use(db=>db.all('SELECT 1'));`],{cwd:process.env.O03_PACKAGE!,encoding:"utf8",env:{...process.env,MIMOCODE_DB:dst}})
  expect(reopen.status).toBe(0);expect(reopen.stderr).toBe("")
  const partial=join(tmp.path,"partial.sqlite"),partialDB=new SQLite(partial)
  partialDB.exec(currentSchema);partialDB.exec("ALTER TABLE session ADD COLUMN next_display_ordinal INTEGER NOT NULL DEFAULT 1");partialDB.close()
  const refuse=spawnSync(process.env.O03_PYTHON!,[join(process.env.O03_REPO!,"scripts/prepare_mimo_profiles.py"),partial,join(tmp.path,"bad-backup"),join(tmp.path,"bad-shadow"),"--writers-stopped"],{encoding:"utf8"})
  expect(refuse.status).not.toBe(0)
})
