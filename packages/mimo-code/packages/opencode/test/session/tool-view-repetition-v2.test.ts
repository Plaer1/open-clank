import {expect,test} from 'bun:test'
import {encodeRepeatedLines,decodeRepeatedLines,framedRepeatedLines} from '../../src/session/tool-view-repeated-lines'
test('nonadjacent exact dictionary remains bounded and v1 compatible',()=>{
 const a='same long source line alpha '.repeat(5)+'\n',b='different source line beta '.repeat(5)+'\n'
 const input=(a+b).repeat(20)
 const encoded=encodeRepeatedLines(input)!
 expect(JSON.parse(encoded).version).toBe(2);expect(decodeRepeatedLines(encoded)).toBe(input)
 const frame=framedRepeatedLines(input,{messageID:'m',partID:'p',revision:1,contentHash:'a'.repeat(64)})!
 expect(frame).toContain('order indexes the exact lines in dictionary');expect(Buffer.byteLength(frame)).toBeLessThan(Buffer.byteLength(input))
 expect(decodeRepeatedLines('{"version":1,"lines":[[2,"x\\n"]]}')).toBe('x\nx\n')
 expect(decodeRepeatedLines('{"version":2,"dictionary":["x\\n"],"order":[0,2,0]}')).toBeUndefined()
 expect(decodeRepeatedLines('{"version":2,"dictionary":["x\\n","y\\n"],"order":[-0,1,0]}')).toBeUndefined()
 expect(decodeRepeatedLines('{"version":2,"version":2,"dictionary":["x\\n","y\\n"],"order":[0,1,0]}')).toBeUndefined()
 expect(decodeRepeatedLines(JSON.stringify({version:2,dictionary:['x\n','x\n'],order:[0,1,0]}))).toBeUndefined()
 expect(decodeRepeatedLines(JSON.stringify({version:2,dictionary:['x'.repeat(8193)+'\n','y\n'],order:[0,1,0]}))).toBeUndefined()
})
