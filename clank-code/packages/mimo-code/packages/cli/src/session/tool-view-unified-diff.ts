/** Bounded, complete unified-diff review projection; never an applicable patch. */
import { parsePatch } from 'diff'

const MAX_BYTES=51_200, MAX_LINES=4096, MAX_FILES=32, MAX_HUNKS=128, MAX_COUNT=4096
const safePath = '[^\\s"\\\\<>`]+'
const gitHeader = new RegExp(`^diff --git a/${safePath} b/${safePath}$`)
const oldHeader = new RegExp(`^--- (?:${safePath})$`)
const newHeader = new RegExp(`^\\+\\+\\+ (?:${safePath})$`)
const indexHeader = /^index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}(?: [0-7]{6})?$/
const hunkHeader = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: [^\n]*)?$/

/** Returns only the review body; caller supplies the recovery frame and checks total size. */
export function summarizeUnifiedDiff(input:string):string | undefined {
  if (typeof input!=='string' || !input.endsWith('\n') || Buffer.byteLength(input,'utf8')>MAX_BYTES ||
      /[\x00-\x08\x0b-\x1f\x7f-\x9f`<>]/u.test(input) || /\p{Cf}/u.test(input)) return
  for (let j=0;j<input.length;j++) {
    const c=input.charCodeAt(j)
    if (c>=0xd800 && c<=0xdbff) {const n=input.charCodeAt(++j);if (!(n>=0xdc00 && n<=0xdfff)) return}
    else if (c>=0xdc00 && c<=0xdfff) return
  }
  const lines=input.slice(0,-1).split('\n')
  if (!lines.length || lines.length>MAX_LINES) return
  const result:string[]=[]
  let i=0,files=0,hunks=0,omitted=0,changes=0
  while (i<lines.length) {
    if (++files>MAX_FILES) return
    if (gitHeader.test(lines[i]!)) {
      result.push(lines[i++]!)
      if (indexHeader.test(lines[i] ?? '')) result.push(lines[i++]!)
    }
    if (!oldHeader.test(lines[i] ?? '') || !newHeader.test(lines[i+1] ?? '')) return
    result.push(lines[i++]!,lines[i++]!)
    let fileHunks=0,oldEnd=-1,newEnd=-1
    while (i<lines.length && lines[i]!.startsWith('@@')) {
      if (++hunks>MAX_HUNKS) return
      fileHunks++
      const header=lines[i++]!, match=hunkHeader.exec(header)
      if (!match) return
      const oldStart=Number(match[1]), oldCount=Number(match[2] ?? '1'), newStart=Number(match[3]), newCount=Number(match[4] ?? '1')
      if ([oldStart,oldCount,newStart,newCount].some(n=>!Number.isSafeInteger(n) || n<0) ||
          (oldStart===0 && oldCount>0) || (newStart===0 && newCount>0) || oldCount>MAX_COUNT || newCount>MAX_COUNT || !Number.isSafeInteger(oldStart+oldCount) ||
          !Number.isSafeInteger(newStart+newCount) || oldStart<oldEnd || newStart<newEnd) return
      oldEnd=oldStart+oldCount;newEnd=newStart+newCount
      result.push(header)
      let oldSeen=0,newSeen=0,context=0
      const flush=()=>{if (context) {result.push(`[${context} unchanged line${context===1?'':'s'} omitted]`);omitted+=context;context=0}}
      while (i<lines.length && (oldSeen<oldCount || newSeen<newCount)) {
        const line=lines[i++]!, op=line[0]
        if (op===' ') {oldSeen++;newSeen++;context++}
        else if (op==='-') {oldSeen++;flush();result.push(line);changes++}
        else if (op==='+') {newSeen++;flush();result.push(line);changes++}
        else return
        if (oldSeen>oldCount || newSeen>newCount) return
      }
      flush()
      if (oldSeen!==oldCount || newSeen!==newCount) return
    }
    if (!fileHunks) return
  }
  if (!omitted || !changes) return
  try {
    const parsed=parsePatch(input)
    if (parsed.length!==files || parsed.reduce((n,patch)=>n+patch.hunks.length,0)!==hunks) return
  } catch { return }
  return result.join('\n')+'\n'
}
