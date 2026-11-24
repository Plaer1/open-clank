import { afterEach, expect, test } from 'bun:test'
import { Layer, ManagedRuntime } from 'effect'
import { Instance } from '../../src/project/instance'
import { Server } from '../../src/server/server'
import { Session } from '../../src/session'
import { Database, sql } from '../../src/storage'
import { tmpdir } from '../fixture/fixture'

afterEach(async()=>{ await Instance.disposeAll() })
test('session workflow observations bound indexed rows before materializing text',async()=>{
 await using tmp=await tmpdir({git:true})
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer))
  try {
   const root=await rt.runPromise(Session.Service.use(s=>s.create({})))
   const other=await rt.runPromise(Session.Service.use(s=>s.create({})))
   Database.use(db=>{
    for(let i=0;i<34;i++) db.run(sql`INSERT INTO workflow_run(id,session_id,name,status,running,succeeded,failed,time_created,time_updated) VALUES (${'wf_'+String(i).padStart(26,'0')},${root.id},'test','running',0,0,0,1,1)`)
    db.run(sql`INSERT INTO workflow_run(id,session_id,name,status,running,succeeded,failed,time_created,time_updated) VALUES (${'wf_'+('x'.repeat(26))},${other.id},'foreign','failed',0,0,0,1,1)`)
    const plan=db.all<{detail:string}>(sql`EXPLAIN QUERY PLAN SELECT rowid,id FROM workflow_run INDEXED BY workflow_run_session_idx WHERE session_id=${root.id} AND rowid>0 ORDER BY rowid LIMIT 33`)
    expect(plan.map(x=>x.detail).join(' ')).toContain('workflow_run_session_idx')
    expect(plan.map(x=>x.detail).join(' ')).not.toContain('TEMP B-TREE')
   })
   const app=Server.Default().app
   const first=await app.request(`/session/${root.id}/observation-page/workflow?limit=32`);expect(first.status).toBe(200)
   const page=await first.json() as any;expect(page.items).toHaveLength(32);expect(page.nextCursor).toBeDefined();expect(page.items[0]).not.toHaveProperty('name')
   const second=await app.request(`/session/${root.id}/observation-page/workflow?limit=32&after=${page.nextCursor}`);expect((await second.json() as any).items).toHaveLength(2)
   Database.use(db=>db.run(sql`UPDATE workflow_run SET id=${'wf_'+('x'.repeat(10000))} WHERE id=${'wf_'+String(0).padStart(26,'0')}`))
   expect((await app.request(`/session/${root.id}/observation-page/workflow?limit=32`)).status).toBe(503)
   Database.use(db=>db.run(sql`UPDATE workflow_run SET id=${'wf_'+String(0).padStart(26,'0')},status=${'s'.repeat(10000)} WHERE length(id)>100`))
   expect((await app.request(`/session/${root.id}/observation-page/workflow?limit=32`)).status).toBe(503)
  } finally { await rt.dispose() }
 }})
})
