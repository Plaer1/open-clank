import { expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"

test("ready-only instance never boots or awaits initialization and rejects disposal drift", async () => {
  await using tmp = await tmpdir()
  let calls=0, releaseInit!:()=>void, enteredInit!:()=>void
  const entered=new Promise<void>(resolve=>{enteredInit=resolve})
  const init=new Promise<void>(resolve=>{releaseInit=resolve})
  expect(await Instance.provideIfReady({directory:tmp.path,fn:()=>++calls})).toEqual({ready:false})
  const initializing=Instance.provide({directory:tmp.path,init:async()=>{enteredInit();await init},fn:()=>undefined})
  await entered
  expect(await Instance.provideIfReady({directory:tmp.path,fn:()=>++calls})).toEqual({ready:false})
  expect(calls).toBe(0)
  releaseInit();await initializing
  expect(await Instance.provideIfReady({directory:tmp.path,fn:()=>++calls})).toEqual({ready:true,value:1})
  let enteredRead!:()=>void, releaseRead!:()=>void
  const reading=new Promise<void>(resolve=>{enteredRead=resolve}), blocked=new Promise<void>(resolve=>{releaseRead=resolve})
  const read=Instance.provideIfReady({directory:tmp.path,fn:async()=>{enteredRead();await blocked;return 'stale'}})
  await reading
  await Instance.disposeDirectory(tmp.path)
  releaseRead()
  expect(await read).toEqual({ready:false})
  expect(await Instance.provideIfReady({directory:tmp.path,fn:()=>++calls})).toEqual({ready:false})
})
