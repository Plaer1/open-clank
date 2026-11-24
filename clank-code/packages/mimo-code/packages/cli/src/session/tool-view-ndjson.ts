/** Closed, bounded NDJSON string-object grammar; preserves semantic field order. */
export function compactNdjsonLogs(input:string):string|undefined {
  if (Buffer.byteLength(input,'utf8')>51200 || !input.endsWith('\n') || input.includes('\r')) return
  const lines=input.slice(0,-1).split('\n')
  if(lines.length<2 || lines.length>200)return
  let columns:string[]|undefined
  const rows:string[][]=[]
  for(const line of lines) {
    let i=0
    const ws=()=>{while(line[i]===' '||line[i]==='\t')i++}
    const string=():string|undefined=>{
      if(line[i]!== '"')return
      const start=i++
      while(i<line.length){const c=line[i++];if(c==='\\'){if(i>=line.length)return;i++;continue}if(c==='"'){
        try {const value=JSON.parse(line.slice(start,i));if(typeof value!=='string'||/[\x00-\x1f\x7f-\x9f]/u.test(value)||!value.isWellFormed())return;return value}catch{return}
      }}
    }
    ws();if(line[i++]!=='{')return
    const keys:string[]=[], values:string[]=[], seen=new Set<string>()
    ws();if(line[i]==='}')return
    for(;;){ws();const key=string();if(key===undefined||seen.has(key))return;seen.add(key);ws();if(line[i++]!==':')return;ws();const value=string();if(value===undefined)return;keys.push(key);values.push(value);ws();const c=line[i++];if(c==='}')break;if(c!==',')return}
    ws();if(i!==line.length)return
    if(!columns){if(!['timestamp','level','message'].every(key=>seen.has(key)))return;columns=keys}
    else if(columns.length!==keys.length || columns.some((key,n)=>key!==keys[n]))return
    rows.push(values)
  }
  return JSON.stringify({columns,rows})
}
