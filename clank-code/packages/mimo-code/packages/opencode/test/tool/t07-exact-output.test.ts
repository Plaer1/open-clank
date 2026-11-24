import { expect } from 'bun:test'
import { Effect, Layer } from 'effect'
import { HistoryTool } from '../../src/tool/history'
import { History } from '../../src/history'
import { Truncate } from '../../src/tool'
import { Agent } from '../../src/agent/agent'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import { provideTmpdirInstance } from '../fixture/fixture'
import { testEffect } from '../lib/effect'
import { SessionID, MessageID } from '../../src/session/schema'
import { ManagedProvider } from '../../src/acp/managed-provider'
import { installManagedSessionBinding } from '../../src/memory/session-scope'
const it = testEffect(Layer.mergeAll(History.defaultLayer, Truncate.defaultLayer, Agent.defaultLayer, CrossSpawnSpawner.defaultLayer))
it.live('exact history output uses bounded model-visible JSON pages', () => provideTmpdirInstance(() => Effect.gen(function* () {
  process.env.OPEN_CLANK_MANAGED='1'
  installManagedSessionBinding('ses_test',{owner:'alice',stableChatID:'chat',engineSessionID:'ses_test',engineAliases:[],memoryWorkspaceID:'global',authorityWorkspaceID:'authority',copalWorkspace:'copal',physicalCwd:'/work',bindingRevision:1,mapRevision:1,mappingRevision:1,memoryEnabled:true,transition:null})
  const raw='\n'.repeat(2100)+'é😀'
  const first='\n'.repeat(2100)+'é'
  const total=Buffer.byteLength(raw,'utf8'), next=Buffer.byteLength(first,'utf8')
  let captured: any
  ManagedProvider.installHostConnection({async extMethod(method: string, params: Record<string,unknown>) {
    captured=params; const second=params.offset===next; return {ok:true,operation:'exact_output',result:{state:'ready',page:{revision:1,contentHash:'a'.repeat(64),outputSHA256:'b'.repeat(64),outputBytes:total,offset:second ? next : 0,nextOffset:second ? null : next,text:second ? '😀' : first}}}
  }} as any)
  const info=yield* HistoryTool;const tool=yield* info.init()
  const ctx={sessionID:SessionID.make('ses_test'),messageID:MessageID.make(''),callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{sessionID:'ses_test',rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'}}}
  const result=yield* tool.execute({operation:'exact_output',message_id:'m',part_id:'p',revision:1,content_hash:'a'.repeat(64)},ctx as any)
  const page=JSON.parse(result.output);expect(page.text).toBe(first);expect(page.nextOffset).toBe(next)
  expect(result.metadata).toMatchObject({truncated:false});expect(JSON.stringify(result.metadata)).not.toContain(first)
  expect(captured).toMatchObject({operation:'exact_output',actorID:'main',rootOperationID:'root'})
  const result2=yield* tool.execute({operation:'exact_output',message_id:'m',part_id:'p',revision:1,content_hash:'a'.repeat(64),byte_offset:page.nextOffset},ctx as any)
  const page2=JSON.parse(result2.output);expect(page.text+page2.text).toBe(raw);expect(page2.nextOffset).toBeNull();expect(page2.offset).toBe(next)
  ManagedProvider.resetForTest(); delete process.env.OPEN_CLANK_MANAGED
})))
