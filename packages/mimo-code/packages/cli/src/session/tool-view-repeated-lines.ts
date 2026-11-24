/** Bounded exact repeated-line representations, never a canonical source. */
const MAX_BYTES=51200, MAX_LINES=4096, MAX_LINE_BYTES=8192
function validText(value:string):boolean {
  if (Buffer.byteLength(value,'utf8')>MAX_BYTES || /[\u0000-\u0009\u000b-\u001f\u007f-\u009f<>`]|\p{Cf}/u.test(value)) return false
  for(let i=0;i<value.length;i++) {const n=value.charCodeAt(i);if(n>=0xd800&&n<=0xdbff){const next=value.charCodeAt(++i);if(!(next>=0xdc00&&next<=0xdfff))return false}else if(n>=0xdc00&&n<=0xdfff)return false}
  return true
}
export function decodeRepeatedLines(encoded:string):string | undefined {
  if (Buffer.byteLength(encoded,'utf8')>102400) return
  try {
    const v=JSON.parse(encoded)
    if (!v || typeof v!=='object' || Array.isArray(v)) return
    if (v.version===2) {
      if (Object.keys(v).sort().join(',')!=='dictionary,order,version' || !Array.isArray(v.dictionary) || !v.dictionary.length || v.dictionary.length>MAX_LINES || !Array.isArray(v.order) || !v.order.length || v.order.length>MAX_LINES) return
      if (JSON.stringify({version:2,dictionary:v.dictionary,order:v.order})!==encoded) return
      const seen=new Set<string>()
      for (const line of v.dictionary) {
        if(typeof line!=='string' || !line || !validText(line) || Buffer.byteLength(line,'utf8')>MAX_LINE_BYTES || line.slice(0,-1).includes('\n') || seen.has(line)) return
        seen.add(line)
      }
      let bytes=0, next=0, result='', nonadjacent=false
      const last=new Map<number,number>()
      for (let i=0;i<v.order.length;i++) {
        const index=v.order[i]
        if(!Number.isSafeInteger(index)||index<0||index>=v.dictionary.length)return
        if(!last.has(index)) {if(index!==next++)return} else if(i-last.get(index)!>1) nonadjacent=true
        const line=v.dictionary[index]
        if(!line.endsWith('\n') && i!==v.order.length-1)return
        bytes+=Buffer.byteLength(line,'utf8');if(bytes>MAX_BYTES)return
        result+=line;last.set(index,i)
      }
      return next===v.dictionary.length && nonadjacent ? result : undefined
    }
    if (Object.keys(v).sort().join(',')!=='lines,version' || v.version!==1 || !Array.isArray(v.lines) || !v.lines.length || v.lines.length>MAX_LINES) return
    let bytes=0, count=0, result=''
    for(let i=0;i<v.lines.length;i++) {
      const row=v.lines[i]
      if (!Array.isArray(row) || row.length!==2 || !Number.isSafeInteger(row[0]) || row[0]<1 || row[0]>MAX_LINES || typeof row[1]!=='string' || !row[1] || !validText(row[1]) || Buffer.byteLength(row[1],'utf8')>MAX_LINE_BYTES || row[1].slice(0,-1).includes('\n') || (!row[1].endsWith('\n') && (i!==v.lines.length-1 || row[0]!==1))) return
      count+=row[0]; bytes+=Buffer.byteLength(row[1],'utf8')*row[0]
      if(count>MAX_LINES || bytes>MAX_BYTES)return
      result+=row[1].repeat(row[0])
    }
    return result
  } catch {return}
}
function representations(input:string):string[] {
  if (!input || !validText(input)) return []
  const lines=input.match(/[^\n]*\n|[^\n]+$/g)
  if (!lines || lines.length>MAX_LINES) return []
  const rows:Array<[number,string]>=[], dictionary:string[]=[], order:number[]=[], positions=new Map<string,number>(), last=new Map<number,number>()
  let nonadjacent=false
  for(const line of lines) {
    if(Buffer.byteLength(line,'utf8')>MAX_LINE_BYTES)return []
    const previous=rows.at(-1)
    if(previous && previous[1]===line)previous[0]++
    else rows.push([1,line])
    let index=positions.get(line)
    if(index===undefined){index=dictionary.length;positions.set(line,index);dictionary.push(line)}
    else if(order.length-last.get(index)!>1) nonadjacent=true
    last.set(index,order.length);order.push(index)
  }
  const candidates:string[]=[]
  if(rows.some(row=>row[0]>1)) candidates.push(JSON.stringify({version:1,lines:rows}))
  if(nonadjacent)candidates.push(JSON.stringify({version:2,dictionary,order}))
  return candidates.filter(value=>decodeRepeatedLines(value)===input)
}
export function encodeRepeatedLines(input:string):string | undefined {
  return representations(input).sort((a,b)=>Buffer.byteLength(a,'utf8')-Buffer.byteLength(b,'utf8'))[0]
}
export function framedRepeatedLines(input:string,ref:{messageID:string;partID:string;revision:number;contentHash:string}):string | undefined {
  if(!Number.isSafeInteger(ref.revision)||ref.revision<1||!/^[a-f0-9]{64}$/.test(ref.contentHash)||[ref.messageID,ref.partID].some(v=>!v||v.length>256))return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frames=representations(input).map(encoded=>{
    const mode=JSON.parse(encoded).version===1 ? 'each [count,line] repeats that complete line count times' : 'order indexes the exact lines in dictionary, in sequence'
    return `[Exact repeated-line view: ${mode}. Original: history(${invocation}); follow nextOffset for further pages.]\n${encoded}`
  })
  frames.sort((a,b)=>Buffer.byteLength(a,'utf8')-Buffer.byteLength(b,'utf8'))
  const frame=frames[0]
  return frame && Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}
