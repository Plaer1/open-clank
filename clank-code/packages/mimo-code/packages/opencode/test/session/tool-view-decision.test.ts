import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { Database as SQLite } from 'bun:sqlite'
import path from 'node:path'
import { sql } from 'drizzle-orm'
import { Database } from '../../src/storage'
import { ProjectTable } from '../../src/project/project.sql'
import { SessionTable, MessageTable, PartTable } from '../../src/session/session.sql'
import { Session } from '../../src/session'
import { AppRuntime } from '../../src/effect/app-runtime'
import { freezeToolView } from '../../src/session/tool-view-decision'
import { Instance } from '../../src/project/instance'
import { tmpdir } from '../fixture/fixture'

test('optional decision CAS and real projector invalidation', async () => {
  await using tmp=await tmpdir()
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const now=Date.now()
    const part={id:'prt_view',sessionID:'ses_view',messageID:'msg_view',type:'tool',tool:'x',callID:'call',state:{status:'completed',input:{},output:'  {"a": 1} ',title:'',metadata:{truncated:false},time:{start:now,end:now+1}}} as any
    Database.use(db=>{
      db.insert(ProjectTable).values({id:'proj_view' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run()
      db.insert(SessionTable).values({id:'ses_view' as any,project_id:'proj_view' as any,slug:'v',directory:tmp.path,title:'v',version:'1',time_created:now,time_updated:now}).run()
      db.insert(MessageTable).values({id:'msg_view' as any,session_id:'ses_view' as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run()
    })
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(part)))
    const admission={on:true,hostAccountID:'account',holderID:'holder',clockID:'d'.repeat(32),providerID:'provider',modelID:'model',routeMethodKey:'method',connectionID:'connection',modelRouteID:'route',profile:'json-whitespace-v1' as const,sessionID:'ses_view',messageID:'msg_view',partID:'prt_view',rootOperationID:'root',actorID:'main',innerTurnID:'turn',generation:1,settingsRevision:'a'.repeat(64)}
    expect(freezeToolView(admission,{state:'raw'})).toBeUndefined()
    const source=path.join(tmp.path,'source.sqlite'), backup=path.join(tmp.path,'backup.sqlite'), prepared=path.join(tmp.path,'prepared.sqlite')
    Database.use(db=>db.run(sql`VACUUM INTO ${source}`))
    const repo=path.resolve(import.meta.dir,'../../../../../..')
    const result=Bun.spawnSync({cmd:['/usr/bin/python3',path.join(repo,'scripts/prepare_tool_view_profile.py'),source,backup,prepared,'--writers-stopped'],cwd:repo})
    expect(result.exitCode).toBe(0)
    const preparedDB=new SQLite(prepared,{readonly:true})
    expect((preparedDB.query("PRAGMA table_info('part')").all() as any[]).some(row=>row.name==='openclank_view_decision')).toBe(true)
    preparedDB.close()
    const sourceDB=new SQLite(source,{readonly:true})
    expect((sourceDB.query("PRAGMA table_info('part')").all() as any[]).some(row=>row.name==='openclank_view_decision')).toBe(false)
    sourceDB.close()
    Database.use(db=>db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`))
    const data=Database.use(db=>db.get<{data:string}>(sql`SELECT data FROM part WHERE id='prt_view'`)!.data)
    const candidate={version:1,state:'candidate',...Object.fromEntries(Object.entries(admission).filter(([key])=>key!=='on')),dataSHA256:createHash('sha256').update(data).digest('hex'),contentSHA256:'f'.repeat(64)}
    Database.use(db=>db.run(sql`UPDATE part SET openclank_view_decision=${JSON.stringify(candidate)} WHERE id='prt_view'`))
    expect(freezeToolView({...admission,on:false},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,rootOperationID:'other'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,hostAccountID:'foreign'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,holderID:'other'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,clockID:'e'.repeat(32)},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,providerID:'foreign'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,modelID:'foreign'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,routeMethodKey:'foreign'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...admission,profile:'foreign' as any},{state:'raw'})).toBeUndefined()
    Database.use(db=>db.run(sql`UPDATE part SET data=${'x'.repeat(262145)} WHERE id='prt_view'`))
    expect(freezeToolView(admission,{state:'raw'})).toBeUndefined()
    Database.use(db=>db.run(sql`UPDATE part SET data=${data} WHERE id='prt_view'`))
    expect(freezeToolView(admission,{state:'raw'})?.state).toBe('raw')
    expect(freezeToolView(admission,{state:'view',sourceRevision:1,sourceHash:'b'.repeat(64),viewSHA256:'c'.repeat(64)})?.state).toBe('raw')
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(part)))
    expect(Database.use(db=>db.get<{v:string | null}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id='prt_view'`)!.v)).not.toBeNull()
    const copy=path.join(tmp.path,'restart.sqlite')
    Database.use(db=>db.run(sql`VACUUM INTO ${copy}`))
    const reopened=new SQLite(copy,{readonly:true})
    expect(JSON.parse((reopened.query("SELECT openclank_view_decision FROM part WHERE id='prt_view'").get() as any).openclank_view_decision).state).toBe('raw')
    reopened.close()
    const changed=structuredClone(part);changed.state.output='changed'
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(changed)))
    expect(Database.use(db=>db.get<{v:string | null}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id='prt_view'`)!.v)).toBeNull()
    expect(freezeToolView(admission,{state:'raw'})).toBeUndefined()
    const imported=structuredClone(part); imported.id='prt_import'; imported.state.metadata.openclank_view_decision=JSON.stringify(candidate)
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(imported)))
    expect(Database.use(db=>db.get<{v:string | null}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id='prt_import'`)!.v)).toBeNull()
    Database.use(db=>db.delete(PartTable).run())
  }})
})
