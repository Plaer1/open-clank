import {expect} from 'bun:test'
import {Effect,Layer} from 'effect'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {ToolRegistry} from '../../src/tool'
import {encodeArgumentAdmission} from '../../src/session/gcf-arguments'
import {createGcfEncoder} from '../../src/session/gcf-native'
import {provideTmpdirInstance} from '../fixture/fixture'
import {testEffect} from '../lib/effect'
const it=testEffect(Layer.mergeAll(ToolRegistry.defaultLayer,CrossSpawnSpawner.defaultLayer))
it.live('original admission and native registry witness stay bounded and current',()=>provideTmpdirInstance(()=>Effect.gen(function*(){
 const route={rootOperationID:'root',providerID:'provider',modelID:'model',connectionID:'conn',modelRouteID:'route'}
 const value={version:1,requested:true,sessionID:'session',...route,hostAccountID:'account',holderID:'holder',generation:1,settingsRevision:'a'.repeat(64),routeMethodKey:'key',profile:'gcf-argument-v1'}
 expect(encodeArgumentAdmission(undefined,'session',route,{providerID:'provider',modelID:'model'})).toBeUndefined()
 expect(encodeArgumentAdmission(value,'session',route,{providerID:'provider',modelID:'model'})).toBeDefined()
 expect(encodeArgumentAdmission({...value,rootOperationID:'other'},'session',route,{providerID:'provider',modelID:'model'})).toBeUndefined()
 const artifact=createGcfEncoder((b)=>({state:'bypass',reason:'not_smaller',inputBytes:b.length,outputBytes:b.length}),'v','a'.repeat(64))
 expect(artifact.decodeAvailability()).toBe('unavailable')
 const registry=yield* ToolRegistry.Service
 const agent={name:'build',mode:'primary' as const,permission:[],options:{}}
 const off=yield* registry.tools({providerID:'opencode' as any,modelID:'claude-sonnet-4' as any,agent})
 expect(registry.argumentDefinitionWitness?.(off.find(x=>x.id==='glob')!)).toBeUndefined()
 const on=yield* registry.tools({providerID:'opencode' as any,modelID:'claude-sonnet-4' as any,agent,argumentWitness:true})
 const glob=on.find(x=>x.id==='glob')!,grep=on.find(x=>x.id==='grep')!
 const w=registry.argumentDefinitionWitness?.(glob),g=registry.argumentDefinitionWitness?.(grep)
 expect(w).toBeDefined();expect(g).toBeDefined();expect(yield* w!.current()).toBe(true)
 expect(glob.parameters.safeParse({pattern:'*.ts'}).success).toBe(true)
 expect(glob.parameters.safeParse({pattern:42}).success).toBe(false)
 const restricted=yield* registry.tools({providerID:'opencode' as any,modelID:'claude-sonnet-4' as any,agent:{...agent,toolAllowlist:['read']},argumentWitness:true})
 expect(restricted.some(x=>x.id==='glob'||x.id==='grep')).toBe(false)
 const deep=(glob.parameters as any).shape.pattern._def, before=deep.type
 try {deep.type='number';expect(yield* w!.current()).toBe(false)} finally {deep.type=before}
 expect(yield* w!.current()).toBe(true)
 yield* registry.reload()
 expect(yield* w!.current()).toBe(false)
})))
