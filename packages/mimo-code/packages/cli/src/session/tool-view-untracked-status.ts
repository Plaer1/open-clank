/** Closed untracked-status-shaped text grammar; no command or repository authority. */
export function summarizeUntrackedStatus(input:string):{body:string;count:number}|undefined {
  if(typeof input!=='string' || Buffer.byteLength(input,'utf8')>51200 || !input.endsWith('\n') || input.includes('\r'))return
  const lines=input.slice(0,-1).split('\n')
  if(lines.length<2 || lines.length>2048)return
  const paths:string[]=[], seen=new Set<string>()
  for(const line of lines){
    if(!line.startsWith('?? '))return
    const path=line.slice(3)
    if(!path || path.length>1024 || !/^[A-Za-z0-9._/-]+$/.test(path) || path.startsWith('/'))return
    const parts=path.split('/')
    if(parts[parts.length-1]==='')parts.pop()
    if(!parts.length || parts.some(part=>!part || part==='.' || part==='..') || seen.has(path))return
    seen.add(path);paths.push(path)
  }
  return {body:JSON.stringify(paths)+'\n',count:paths.length}
}
