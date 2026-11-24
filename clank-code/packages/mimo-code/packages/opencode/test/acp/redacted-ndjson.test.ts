import { test, expect } from 'bun:test'
import { redactedNdJsonStream } from '../../src/acp/redacted-ndjson'

test('chunked valid and malformed frames do not disclose input', async () => {
  const log:string[]=[]; const prior=console.error; console.error=(...args)=>{log.push(args.join(' '))}
  try {
    const encoder=new TextEncoder()
    const input=new ReadableStream<Uint8Array>({start(c){c.enqueue(encoder.encode('{"id":'));c.enqueue(encoder.encode('1}\nSECRET-invalid\n{"id":2}\n'));c.close()}})
    const output=new WritableStream<Uint8Array>()
    const reader=redactedNdJsonStream(output,input).readable.getReader()
    expect((await reader.read()).value).toEqual({id:1})
    expect((await reader.read()).value).toEqual({id:2})
    expect((await reader.read()).done).toBe(true)
    expect(log).toEqual(['Malformed ACP input frame.'])
  } finally {console.error=prior}
})
