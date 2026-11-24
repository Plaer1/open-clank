/** ACP-compatible newline JSON framing; malformed input never reaches diagnostics. */
export function redactedNdJsonStream(output: WritableStream<Uint8Array>, input: ReadableStream<Uint8Array>) {
  const encoder = new TextEncoder(), decoder = new TextDecoder()
  const maxFrame = 32 * 1024 * 1024
  const reader = input.getReader()
  let cancelled = false
  const readable = new ReadableStream<any>({
    async start(controller) {
      let content = '', bytes = 0, dropping = false
      try {
        while (!cancelled) {
          const {value,done} = await reader.read()
          if (done) break
          if (!value) continue
          const parts = decoder.decode(value,{stream:true}).split('\n')
          for (let i=0;i<parts.length;i++) {
            const part=parts[i]!
            if (!dropping) {
              bytes += encoder.encode(part).byteLength
              if (bytes > maxFrame) { dropping=true; content=''; console.error('ACP input frame exceeded its limit.') }
              else content += part
            }
            if (i < parts.length-1) {
              if (!dropping && content.trim()) {
                try { controller.enqueue(JSON.parse(content.trim())) }
                catch { console.error('Malformed ACP input frame.') }
              }
              content=''; bytes=0; dropping=false
            }
          }
        }
      } finally {
        reader.releaseLock()
        if (!cancelled) controller.close()
      }
    },
    async cancel() { cancelled=true; await reader.cancel().catch(()=>{}) },
  })
  const writable = new WritableStream<any>({
    async write(message) {
      const writer=output.getWriter()
      try { await writer.write(encoder.encode(JSON.stringify(message)+'\n')) }
      finally { writer.releaseLock() }
    },
  })
  return {readable,writable}
}
