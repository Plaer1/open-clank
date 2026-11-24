import { test, expect } from 'bun:test'
import { Instance } from '../../src/project/instance'
import { Database, sql } from '../../src/storage'
import { SessionID } from '../../src/session/schema'
import { observe } from '../../src/session/goal-store'
import { tmpdir } from '../fixture/fixture'
test('bounded scoped goal status reads without history or mutation', async () => {
 await using tmp=await tmpdir();await Instance.provide({directory:tmp.path,fn:async()=>{
  const scope={owner:'alice',workspace:'memory-space',project:'project',sessionID:SessionID.make('ses_o04_goal')}
  expect(observe(scope)).toEqual({state:'available',stateRevision:0,active:null,queue:[]})
  const make=(id:string)=>({id,revision:1,status:'queued',...scope,objective:'PRIVATE',evidence:[{observation:'SECRET'}]})
  const data=JSON.stringify({revision:4,active:{...make('active'),status:'active'},queue:Array.from({length:33},(_,i)=>make('q'+i)),history:[{objective:'OLD'}]})
  Database.use(db=>db.run(sql`INSERT INTO goal_state(owner,workspace,project,session_id,revision,data,time_updated) VALUES (${scope.owner},${scope.workspace},${scope.project},${scope.sessionID},4,${data},1)`))
  const page=observe(scope);expect(page.state).toBe('partial');expect(page.queue).toHaveLength(32);expect(page.active?.status).toBe('active');expect(JSON.stringify(page)).not.toContain('PRIVATE');expect(JSON.stringify(page)).not.toContain('SECRET')
  expect(observe({...scope,workspace:'files-space'}).stateRevision).toBe(0)
  const foreign=JSON.stringify({revision:4,active:{...make('x'),owner:'foreign'},queue:[]});Database.use(db=>db.run(sql`UPDATE goal_state SET data=${foreign} WHERE session_id=${scope.sessionID}`));expect(()=>observe(scope)).toThrow()
  Database.use(db=>db.run(sql`UPDATE goal_state SET data=${'x'.repeat(262145)} WHERE session_id=${scope.sessionID}`));expect(()=>observe(scope)).toThrow()
  Database.use(db=>db.run(sql`UPDATE goal_state SET data=${'{broken'} WHERE session_id=${scope.sessionID}`));expect(()=>observe(scope)).toThrow()
  expect(Database.use(db=>db.get<{data:string}>(sql`SELECT data FROM goal_state WHERE session_id=${scope.sessionID}`))?.data).toBe('{broken')
 }})
})
