import { expect } from 'bun:test'
import { Effect, Layer } from 'effect'
import { JsonPreviewTool } from '../../src/tool/json-preview'
import { Truncate } from '../../src/tool'
import { Agent } from '../../src/agent/agent'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import { testEffect } from '../lib/effect'
import { provideTmpdirInstance } from '../fixture/fixture'
import { SessionID, MessageID } from '../../src/session/schema'

const it = testEffect(Layer.mergeAll(Truncate.defaultLayer, Agent.defaultLayer, CrossSpawnSpawner.defaultLayer))
it.live('JSON preview is explicit, bounded and permission checked', () => provideTmpdirInstance(() => Effect.gen(function* () {
  const info = yield* JsonPreviewTool
  const tool = yield* info.init()
  let asks = 0
  const ctx = { sessionID: SessionID.make('ses_preview'), messageID: MessageID.make(''), callID:'call', agent:'build',
    abort:AbortSignal.any([]), messages:[], metadata:()=>Effect.void,
    ask:(request:any)=>Effect.sync(()=>{ expect(request.permission).toBe('json_preview'); asks++ }) }
  const original = '{ "n" : 1e+02, "s" : "é \\u0061" }'
  const result = yield* tool.execute({json:original}, ctx as any)
  expect(result.output).toBe('{"n":1e+02,"s":"é \\u0061"}')
  expect(result.metadata).toMatchObject({state:'preview', inputBytes:Buffer.byteLength(original), outputBytes:Buffer.byteLength(result.output), truncated:false})
  for (const json of ['{"x":1}', '{invalid}', ' '.repeat(51201), '"\\ud800"']) {
    const bypass = yield* tool.execute({json}, ctx as any)
    expect(bypass.metadata.state).toBe('bypass')
    expect(bypass.output).not.toBe(json)
  }
  expect(asks).toBe(5)
})))
