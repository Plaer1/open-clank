/** Conservative elision of complete, canonical base64-shaped lines only. */
import {createHash} from 'node:crypto'
export function elideBase64Lines(input:string):{body:string;omitted:number}|undefined {
  if (typeof input!=='string' || !input.endsWith('\n') || Buffer.byteLength(input,'utf8')>51200 || Buffer.from(input,'utf8').toString('utf8')!==input || /[\x00-\x09\x0b-\x1f\x7f`<>]/.test(input)) return
  const lines=input.slice(0,-1).split('\n')
  if (lines.length>4096) return
  let omitted=0
  const out:string[]=[]
  for (const line of lines) {
    if (line.length<128 || !/^[A-Za-z0-9+/=]+$/.test(line)) {out.push(line);continue}
    if (line.length%4!==0 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(line)) return
    const decoded=Buffer.from(line,'base64')
    if (decoded.length>38400 || decoded.toString('base64')!==line) return
    const hash=createHash('sha256').update(decoded).digest('hex')
    out.push(`[base64-shaped line omitted; decoded bytes=${decoded.length}; sha256=${hash}]`);omitted++
  }
  return omitted ? {body:out.join('\n')+'\n',omitted} : undefined
}
