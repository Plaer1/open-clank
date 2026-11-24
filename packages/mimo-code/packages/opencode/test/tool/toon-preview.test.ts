import { expect } from 'bun:test'
import { Effect, Layer } from 'effect'
import { ToonPreviewTool } from '../../src/tool/toon-preview'
import { Truncate } from '../../src/tool'
import { Agent } from '../../src/agent/agent'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import { testEffect } from '../lib/effect'
import { provideTmpdirInstance } from '../fixture/fixture'
import { SessionID, MessageID } from '../../src/session/schema'

const it = testEffect(Layer.mergeAll(Truncate.defaultLayer, Agent.defaultLayer, CrossSpawnSpawner.defaultLayer))
it.live('TOON preview converts explicitly with permission and bounded refusal', () => provideTmpdirInstance(() => Effect.gen(function* () {
  const info = yield* ToonPreviewTool
  const tool = yield* info.init()
  let asks = 0
  const ctx = { sessionID: SessionID.make('ses_toon'), messageID: MessageID.make(''), callID:'call', agent:'build',
    abort:AbortSignal.any([]), messages:[], metadata:()=>Effect.void,
    ask:(request:any)=>Effect.sync(()=>{ expect(request.permission).toBe('toon_preview'); asks++ }) }
  const encoded = yield* tool.execute({direction:'encode',text:'{"n":1,"s":"é"}'},ctx as any)
  expect(encoded.metadata).toMatchObject({state:'preview',inputBytes:Buffer.byteLength('{"n":1,"s":"é"}'),outputBytes:Buffer.byteLength(encoded.output),truncated:false})
  const decoded = yield* tool.execute({direction:'decode',text:encoded.output},ctx as any)
  expect(decoded.output).toBe('{"n":1,"s":"é"}')
  for (const text of ['{"n":1.5}', '{bad}', ' '.repeat(51201)]) {
    const refused = yield* tool.execute({direction:'encode',text},ctx as any)
    expect(refused.metadata.state).toBe('refused')
  }
  expect(asks).toBe(5)
})))
