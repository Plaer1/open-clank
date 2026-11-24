import {expect,test} from 'bun:test'
import {createGcfEncoder} from '../../src/session/gcf-native'
const policy='gcf-generic-v3-safe-integers-v1'
const encoder=(input:Buffer)=>({state:'bypass',reason:'not_smaller',inputBytes:input.length,outputBytes:input.length})
test('decoder capability is bounded, copied and shares exact revocation owner',()=>{
 const old=createGcfEncoder(encoder,'v','a'.repeat(64))
 expect(old.decodeAvailability()).toBe('unavailable')
 expect(old.decode(Buffer.from('x'),policy)).toEqual({state:'refused',reason:'unavailable',inputBytes:1})
 let owned:Buffer|undefined
 const artifact=createGcfEncoder(encoder,'v','a'.repeat(64),(input)=>{owned=input;return {state:'decoded',inputBytes:input.length,outputBytes:2,output:Buffer.from('{}')}})
 const input=Buffer.from('abc');const out=artifact.decode(input,policy)
 expect(artifact.decodeAvailability()).toBe('available');expect(out.state).toBe('decoded');expect(owned).not.toBe(input)
 if(out.state==='decoded'){owned!.fill(0);expect(out.output.toString()).toBe('{}')}
 expect(artifact.decode(input,'wrong').state).toBe('refused')
 expect(artifact.decode(new Uint8Array(new SharedArrayBuffer(4)),policy)).toEqual({state:'refused',reason:'shared_input',inputBytes:4})
 expect(artifact.decode(new Uint8Array(51201),policy)).toEqual({state:'refused',reason:'too_large',inputBytes:51201})
 const bad=createGcfEncoder(encoder,'v','b'.repeat(64),(buf)=>({state:'decoded',inputBytes:buf.length,outputBytes:1,output:Buffer.from('x'),extra:true}))
 expect(bad.decode(input,policy)).toEqual({state:'refused',reason:'codec_failure',inputBytes:3})
 let reentrant:ReturnType<typeof createGcfEncoder>
 reentrant=createGcfEncoder(encoder,'v','c'.repeat(64),(buf)=>{reentrant.revoke();return {state:'decoded',inputBytes:buf.length,outputBytes:2,output:Buffer.from('{}')}})
 expect(reentrant.decode(input,policy)).toEqual({state:'refused',reason:'unavailable',inputBytes:3})
 let getterRevoked:ReturnType<typeof createGcfEncoder>
 getterRevoked=createGcfEncoder(encoder,'v','d'.repeat(64),(buf)=>({state:'decoded',inputBytes:buf.length,outputBytes:2,get output(){getterRevoked.revoke();return Buffer.from('{}')}}))
 expect(getterRevoked.decode(input,policy)).toEqual({state:'refused',reason:'unavailable',inputBytes:3})
 artifact.revoke();expect(artifact.decodeAvailability()).toBe('unavailable');expect(artifact.identity()).toBeUndefined()
})
