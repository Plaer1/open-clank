/** Private Linux index primitive. Callers must independently hold and recheck
 * original Files/account/worker/Once/HEAD authority; no HTTP entry point. */
import { constants } from 'node:fs'
import { open, realpath, lstat, mkdir, unlink, rename, rmdir, access, mkdtemp, readdir } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const MAX_INDEX=4*1024*1024, MAX_FILE=262144
type HashKind='sha1'|'sha256'
type Entry={name:Buffer;mode:number;oid:Buffer;raw:Buffer}
type DirectoryIdentity={device:number;inode:number}
export type PhysicalIndexPreview={version:1;sha256:string;device:number;inode:number;size:number;objectFormat:HashKind;root:DirectoryIdentity;git:DirectoryIdentity;objects:DirectoryIdentity}
export type PhysicalIndexInspection={preview:PhysicalIndexPreview;entries:{path:string;mode:number;oid:string}[]}
export type PhysicalCommitPreview={version:1;physicalIndex:PhysicalIndexPreview;indexEntries:PhysicalIndexInspection['entries'];headEntries:PhysicalIndexInspection['entries'];headOID:string;headRef:string;config:{device:number;inode:number;size:number;sha256:string};executable:{path:string;device:number;inode:number;size:number;sha256:string;version:string}}
export type CommitIdentity={name:string;email:string;seconds:number;timezone:string}
export type CommitAction={message:string;author:CommitIdentity;committer:CommitIdentity}
export type CommitPhase='before_lock'|'before_tree'|'before_commit'|'before_publish'
export type CommitResult={state:'applied'|'refused'|'unknown';tree:'none'|'written_or_present'|'unknown';commit:'none'|'written_or_present'|'unknown';ref:'not_issued'|'applied'|'unknown';commitOID?:string;candidateRetained:boolean;candidateIdentity?:{token:string;device:number;inode:number};cleanup:'complete'|'unknown'}
export type IndexAction={path:string;operation:'stage'|'unstage';mode:number;bytes?:Uint8Array;headOID?:string|null}
export type IndexPhase='before_lock'|'before_object'|'before_publish'
export type IndexResult={state:'applied'|'refused'|'unknown';object:'none'|'written_or_present'|'unknown';candidateRetained:boolean;candidateIdentity?:{token:string;device:number;inode:number}}
const hash=(kind:HashKind,data:Uint8Array)=>createHash(kind).update(data).digest()
const proc=(fd:number,name='')=>`/proc/self/fd/${fd}${name ? '/'+name : ''}`
const directoryIdentity=(value:{dev:number;ino:number;isDirectory:()=>boolean}):DirectoryIdentity=>{
  if(!value.isDirectory() || !Number.isSafeInteger(value.dev) || value.dev<0 || !Number.isSafeInteger(value.ino) || value.ino<=0)throw Error('directory unavailable')
  return {device:value.dev,inode:value.ino}
}
const sameDirectory=(a:DirectoryIdentity,b:DirectoryIdentity)=>a.device===b.device && a.inode===b.inode
async function namespaces(root:string,home:Awaited<ReturnType<typeof open>>,git:Awaited<ReturnType<typeof open>>,objects:Awaited<ReturnType<typeof open>>) {
  const held={root:directoryIdentity(await home.stat()),git:directoryIdentity(await git.stat()),objects:directoryIdentity(await objects.stat())}
  if(await realpath(root)!==root || !sameDirectory(held.root,directoryIdentity(await lstat(root))) ||
    !sameDirectory(held.git,directoryIdentity(await lstat(proc(home.fd,'.git')))) ||
    !sameDirectory(held.objects,directoryIdentity(await lstat(proc(git.fd,'objects')))))throw Error('repository namespace changed')
  return held
}
const validPath=(value:string)=>typeof value==='string' && !!value && Buffer.byteLength(value)<=4096 && Buffer.from(value).toString('utf8')===value && !value.startsWith('/') && !value.includes('\0') && value.split('/').every(p=>p && p!=='.' && p!=='..' && !['.git','.clanker','.clankers','.references'].includes(p))
async function rooted(root:string) {
  if(process.platform!=='linux' || !root.startsWith('/') || await realpath(root)!==root) throw Error('repository unavailable')
  const home=await open(root,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
  try {
    if(await realpath(proc(home.fd))!==root)throw Error('repository changed')
    const git=await open(proc(home.fd,'.git'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    return {home,git}
  } catch(e) {await home.close();throw e}
}
async function readBounded(file:Awaited<ReturnType<typeof open>>,size:number) {
  const data=Buffer.alloc(size);let offset=0
  while(offset<size) {const {bytesRead}=await file.read(data,offset,size-offset,offset);if(bytesRead===0)throw Error('file changed');offset+=bytesRead}
  return data
}
async function readIndex(gitfd:number) {
  const file=await open(proc(gitfd,'index'),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK)
  try {
    const before=await file.stat()
    if(!before.isFile() || !Number.isSafeInteger(before.size) || before.size<32 || before.size>MAX_INDEX || !Number.isSafeInteger(before.dev) || before.dev<0 || !Number.isSafeInteger(before.ino) || before.ino<=0)throw Error('index unavailable')
    const bytes=await readBounded(file,before.size);const after=await file.stat()
    if(bytes.length!==before.size || before.dev!==after.dev || before.ino!==after.ino || before.size!==after.size || before.mtimeMs!==after.mtimeMs || before.ctimeMs!==after.ctimeMs)throw Error('index changed')
    return {bytes,device:before.dev,inode:before.ino,size:before.size}
  } finally {await file.close()}
}
function parse(bytes:Buffer) {
  if(bytes.subarray(0,4).toString('ascii')!=='DIRC' || bytes.readUInt32BE(4)!==2)throw Error('unsupported index')
  const formats=(['sha1','sha256'] as const).filter(kind=>{const n=kind==='sha1'?20:32;return bytes.length>=12+n && hash(kind,bytes.subarray(0,-n)).equals(bytes.subarray(-n))})
  if(formats.length!==1)throw Error('unsupported index checksum')
  const kind=formats[0]!, n=kind==='sha1'?20:32, count=bytes.readUInt32BE(8)
  if(count>65536)throw Error('index too large')
  let pos=12;const entries:Entry[]=[]
  for(let i=0;i<count;i++) {
    const start=pos, fixed=40+n+2
    if(start+fixed>bytes.length-n)throw Error('invalid index')
    const mode=bytes.readUInt32BE(start+24), flags=bytes.readUInt16BE(start+40+n)
    if(![0o100644,0o100755].includes(mode) || (flags&0xf000)!==0)throw Error('unsupported entry')
    const end=bytes.indexOf(0,start+fixed)
    if(end<0 || end>=bytes.length-n)throw Error('invalid index')
    const name=bytes.subarray(start+fixed,end), decoded=name.toString('utf8')
    if(!validPath(decoded) || !Buffer.from(decoded).equals(name) || (flags&0x0fff)!==Math.min(name.length,0x0fff))throw Error('unsupported path')
    pos=start+Math.ceil((end+1-start)/8)*8
    if(pos>bytes.length-n || bytes.subarray(end,pos).some(v=>v!==0))throw Error('invalid index padding')
    if(entries.length && Buffer.compare(entries[entries.length-1]!.name,name)>=0)throw Error('unsupported index ordering')
    entries.push({name:Buffer.from(name),mode,oid:Buffer.from(bytes.subarray(start+40,start+40+n)),raw:Buffer.from(bytes.subarray(start,pos))})
  }
  // Only the ordinary optional cache-tree extension is recognized. Split,
  // sparse, unknown and mandatory extensions remain unavailable.
  while(pos<bytes.length-n) {
    if(pos+8>bytes.length-n || bytes.subarray(pos,pos+4).toString('ascii')!=='TREE')throw Error('unsupported index extension')
    const size=bytes.readUInt32BE(pos+4);pos+=8
    if(size>bytes.length-n-pos)throw Error('invalid index extension')
    pos+=size
  }
  if(pos!==bytes.length-n)throw Error('invalid index extension')
  return {kind,entries}
}
async function runGit(rootfd:number,gitfd:number,dirfd:number,objectsfd:number,args:string[],input:Buffer, index:boolean, executable="git", maxOutput=4096, identity?:{author:CommitIdentity;committer:CommitIdentity}, network=false, remoteFD?:number, deadlineAt?:number, localTimeoutMs=3000, commonDir=false) {
  if(deadlineAt!==undefined && performance.now()>=deadlineAt)throw Error('git deadline')
  const environment:Record<string,string>={PATH:process.env.PATH ?? '/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ATTR_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_LITERAL_PATHSPECS:'1',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
  if(commonDir) {if(remoteFD===undefined)throw Error('git common directory unavailable');environment.GIT_COMMON_DIR='/proc/self/fd/7'}
  if(network)environment.GIT_ALLOW_PROTOCOL='file:https'
  if(index)environment.GIT_INDEX_FILE='/proc/self/fd/3/index'
  if(identity)for(const [prefix,value] of [['GIT_AUTHOR',identity.author],['GIT_COMMITTER',identity.committer]] as const) {environment[prefix+'_NAME']=value.name;environment[prefix+'_EMAIL']=value.email;environment[prefix+'_DATE']=`@${value.seconds} ${value.timezone}`} 
  const child=spawn(executable,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.hooksPath=/dev/null','-c','credential.helper=',...args],{cwd:proc(rootfd),env:environment,stdio:['pipe','pipe','pipe',dirfd,objectsfd,gitfd,rootfd,...(remoteFD===undefined?[]:[remoteFD])]})
  let output=Buffer.alloc(0),oversized=false, timedOut=false,spawnError=false
  const closed=new Promise<number|null>((resolve)=>child.once('close',resolve))
  child.on('error',()=>{spawnError=true})
  const {stdin,stdout,stderr}=child
  if(!stdin || !stdout || !stderr) {
    child.kill('SIGKILL')
    await closed
    throw Error('git pipes unavailable')
  }
  const collect=(chunk:Buffer)=>{if(output.length+chunk.length>maxOutput){oversized=true;child.kill('SIGKILL')}else output=Buffer.concat([output,chunk])}
  stdout.on('data',collect);stderr.on('data',collect);stdin.on('error',()=>{})
  const timer=setTimeout(()=>{timedOut=true;child.kill('SIGKILL')},Math.max(1,Math.min(network ? 45000 : localTimeoutMs,deadlineAt===undefined?Infinity:deadlineAt-performance.now())))
  try {
    stdin.end(input)
    const exit=await closed
    if(exit!==0 || oversized || timedOut || spawnError)throw Error('git operation unavailable')
    return output
  } finally {clearTimeout(timer)}
}
async function observePhysicalIndex(root:string):Promise<{preview:PhysicalIndexPreview;entries:Entry[]}> {
  const {home,git}=await rooted(root)
  let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const original=await namespaces(root,home,git,objects),value=await readIndex(git.fd),parsed=parse(value.bytes),after=await namespaces(root,home,git,objects)
    if(!sameDirectory(original.root,after.root) || !sameDirectory(original.git,after.git) || !sameDirectory(original.objects,after.objects))throw Error('repository namespace changed')
    return {preview:{version:1,sha256:hash('sha256',value.bytes).toString('hex'),device:value.device,inode:value.inode,size:value.size,objectFormat:parsed.kind,...original},entries:parsed.entries}
  } finally {if(objects)await objects.close();await git.close();await home.close()}
}

export async function previewPhysicalIndex(root:string):Promise<PhysicalIndexPreview> {
  return (await observePhysicalIndex(root)).preview
}

/** Private projection from one checked observation; no index or object writes. */
export async function inspectPhysicalIndex(root:string):Promise<PhysicalIndexInspection> {
  const observed=await observePhysicalIndex(root)
  const result:PhysicalIndexInspection={preview:observed.preview,entries:observed.entries.map(entry=>({path:entry.name.toString('utf8'),mode:entry.mode,oid:entry.oid.toString('hex')}))}
  if(Buffer.byteLength(JSON.stringify(result),'utf8')>MAX_INDEX)throw Error('index projection too large')
  return result
}

async function fileWitness(path:string,limit:number) {
  const file=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK)
  try {const a=await file.stat();if(!a.isFile() || !Number.isSafeInteger(a.size) || a.size<0 || a.size>limit || !Number.isSafeInteger(a.dev) || !Number.isSafeInteger(a.ino) || a.ino<=0)throw Error('file unavailable')
    const bytes=await readBounded(file,a.size),b=await file.stat(),current=await lstat(path)
    if(a.dev!==b.dev || a.ino!==b.ino || a.size!==b.size || a.mtimeMs!==b.mtimeMs || a.ctimeMs!==b.ctimeMs || !current.isFile() || current.dev!==a.dev || current.ino!==a.ino)throw Error('file changed')
    return {device:a.dev,inode:a.ino,size:a.size,sha256:hash('sha256',bytes).toString('hex')}
  } finally {await file.close()}
}
async function absent(path:string) {try {await lstat(path);throw Error('unsupported repository state')} catch(e) {if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e}}
export async function gitExecutable() {
  for(const dir of (process.env.PATH ?? '/usr/bin:/bin').split(':')) {
    if(!dir.startsWith('/'))continue
    try {const path=await realpath(dir+'/git');await access(path,constants.X_OK);return {path,...await fileWitness(path,128*1024*1024)}} catch { /* try next PATH entry */ }
  }
  throw Error('git unavailable')
}
function records(bytes:Buffer,kind:'index'|'tree',width:number):PhysicalIndexInspection['entries'] {
  if(bytes.length && bytes[bytes.length-1]!==0)throw Error('incomplete listing')
  const result:PhysicalIndexInspection['entries']=[]
  const decoded=new TextDecoder('utf-8',{fatal:true}).decode(bytes)
  for(const raw of decoded.split('\0').filter(Boolean)) {
    const match=(kind==='index'?/^(100644|100755) ([0-9a-f]+) 0\t([^\0]+)$/:/^(100644|100755) blob ([0-9a-f]+)\t([^\0]+)$/).exec(raw)
    if(!match || match[2]!.length!==width || !validPath(match[3]!))throw Error('unsupported listing')
    result.push({path:match[3]!,mode:parseInt(match[1]!,8),oid:match[2]!})
    if(result.length>65536)throw Error('listing too large')
  }
  result.sort((a,b)=>Buffer.compare(Buffer.from(a.path),Buffer.from(b.path)))
  if(result.some((v,i)=>i>0 && result[i-1]!.path===v.path))throw Error('duplicate listing')
  return result
}
export async function previewPhysicalCommit(root:string,deadlineAt?:number):Promise<PhysicalCommitPreview> {
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const namespace=await namespaces(root,home,git,objects), configPath=proc(git.fd,'config'),config=await fileWitness(configPath,128*1024), executable=await gitExecutable()
    const guards=['config.worktree','shallow','objects/info/alternates','info/grafts','MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-apply','rebase-merge','sequencer']
    for(const name of guards)await absent(proc(git.fd,name))
    const call=(args:string[],limit=4096)=>runGit(home.fd,git.fd,git.fd,objects!.fd,args,Buffer.alloc(0),false,executable.path,limit,undefined,false,undefined,deadlineAt)
    const text=async (args:string[])=>{const value=new TextDecoder('utf-8',{fatal:true}).decode(await call(args));if(!value.endsWith('\n') || value.slice(0,-1).includes('\n') || value.includes('\0'))throw Error('ambiguous output');return value.slice(0,-1)}
    const version=await text(['--version']), vm=/^git version (\d+)\.(\d+)(?:\.[0-9]+)?$/.exec(version)
    if(!vm || Number(vm[1])!==2 || Number(vm[2])<50)throw Error('git version unavailable')
    const first=await readIndex(git.fd), parsed=parse(first.bytes),width=parsed.kind==='sha1'?40:64
    const configBytes=await call(['config','--local','--no-includes','--null','--list'],128*1024)
    if(configBytes.length && configBytes[configBytes.length-1]!==0)throw Error('config unavailable')
    const allowed=/^(?:core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)|extensions\.objectformat|user\.(?:name|email)|remote\.[^\n]+\.(?:url|fetch|pushurl)|branch\.[^\n]+\.(?:remote|merge))$/
    for(const row of new TextDecoder('utf-8',{fatal:true}).decode(configBytes).split('\0').filter(Boolean)) {const n=row.indexOf('\n'),key=(n<0?row:row.slice(0,n)).toLowerCase(),value=n<0?'':row.slice(n+1)
      if(!allowed.test(key) || (key==='core.bare' && value.toLowerCase()!=='false') || (key==='extensions.objectformat' && value!==parsed.kind))throw Error('unsupported config')}
    const [top,common,format,refs,headOID,headRef]=await Promise.all([text(['rev-parse','--show-toplevel']),text(['rev-parse','--git-common-dir']),text(['rev-parse','--show-object-format']),text(['rev-parse','--show-ref-format']),text(['rev-parse','--verify','HEAD']),text(['symbolic-ref','--quiet','--no-recurse','HEAD'])])
    if(![root,'/proc/self/fd/6'].includes(top) || common!=='/proc/self/fd/5' || format!==parsed.kind || refs!=='files' || !new RegExp('^[0-9a-f]{'+width+'}$').test(headOID) || !headRef.startsWith('refs/heads/') || Buffer.byteLength(headRef,'utf8')>1024 || /[\x00-\x1f\x7f]/.test(headRef))throw Error('repository unavailable')
    await call(['check-ref-format',headRef])
    const direct=async()=>{const bytes=await call(['for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)',headRef]);if(!bytes.equals(Buffer.from(`${headRef}\0${headOID}\0\n`)))throw Error('branch is not direct')}
    await direct()
    if(await text(['cat-file','-t',headOID])!=='commit')throw Error('HEAD is not a commit')
    const indexEntries=parsed.entries.map(e=>({path:e.name.toString('utf8'),mode:e.mode,oid:e.oid.toString('hex')}))
    const listed=records(await call(['ls-files','--stage','-z'],MAX_INDEX),'index',width),headEntries=records(await call(['ls-tree','-r','-z','--full-tree',headOID],MAX_INDEX),'tree',width)
    if(!isDeepStrictEqual(indexEntries,listed))throw Error('index listing changed')
    const physicalIndex:PhysicalIndexPreview={version:1,sha256:hash('sha256',first.bytes).toString('hex'),device:first.device,inode:first.inode,size:first.size,objectFormat:parsed.kind,...namespace}
    const after=await readIndex(git.fd), afterNamespace=await namespaces(root,home,git,objects)
    for(const name of guards)await absent(proc(git.fd,name))
    if(after.device!==first.device || after.inode!==first.inode || after.size!==first.size || !after.bytes.equals(first.bytes) || !isDeepStrictEqual(namespace,afterNamespace) || !isDeepStrictEqual(config,await fileWitness(configPath,128*1024)) || !isDeepStrictEqual(executable,{path:executable.path,...await fileWitness(executable.path,128*1024*1024)}) || await text(['rev-parse','--verify','HEAD'])!==headOID || await text(['symbolic-ref','--quiet','--no-recurse','HEAD'])!==headRef)throw Error('repository changed')
    await direct()
    const result:PhysicalCommitPreview={version:1,physicalIndex,indexEntries,headEntries,headOID,headRef,config,executable:{...executable,version}}
    if(Buffer.byteLength(JSON.stringify(result),'utf8')>MAX_INDEX)throw Error('preview too large')
    return result
  } finally {if(objects)await objects.close();await git.close();await home.close()}
}

/** Private reviewed commit primitive. No caller authority is inferred here. */
export async function applyPhysicalCommit(root:string,expectedPreview:PhysicalCommitPreview,action:CommitAction,candidateToken:string,recheck:(phase:CommitPhase)=>Promise<boolean>):Promise<CommitResult> {
  let tree:CommitResult['tree']='none',commit:CommitResult['commit']='none',ref:CommitResult['ref']='not_issued',commitOID:string|undefined,candidateRetained=false,cleanup:CommitResult['cleanup']='complete'
  let candidateIdentity:CommitResult['candidateIdentity'],lock:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,directory:Awaited<ReturnType<typeof open>>|undefined
  let lockCreated=false
  let lockIdentity:{device:number;inode:number}|undefined,ownedCandidate:{device:number;inode:number}|undefined
  const result=():CommitResult=>({state:ref==='applied'?'applied':ref==='unknown'?'unknown':'refused',tree,commit,ref,...(commitOID?{commitOID}:{}),candidateRetained,...(candidateIdentity?{candidateIdentity}:{}),cleanup})
  try {
    expectedPreview=JSON.parse(JSON.stringify(expectedPreview));action=JSON.parse(JSON.stringify(action))
    const ident=(i:CommitIdentity)=>i && typeof i.name==='string' && typeof i.email==='string' && i.name.length>0 && i.email.length>0 && Buffer.byteLength(i.name)<=256 && Buffer.byteLength(i.email)<=256 && !/[\x00-\x1f\x7f<>]/.test(i.name+i.email) && Buffer.from(i.name).toString('utf8')===i.name && Buffer.from(i.email).toString('utf8')===i.email && Number.isSafeInteger(i.seconds) && i.seconds>=0 && i.seconds<=253402300799 && /^[+-](?:0\d|1[0-4])[0-5]\d$/.test(i.timezone)
    if(typeof root!=='string' || !/^[0-9a-f]{32}$/.test(candidateToken) || typeof recheck!=='function' || typeof action.message!=='string' || !action.message || !action.message.endsWith('\n') || action.message.includes('\0') || Buffer.byteLength(action.message)>16384 || Buffer.from(action.message).toString('utf8')!==action.message || !ident(action.author) || !ident(action.committer))return result()
    const same=async()=>isDeepStrictEqual(await previewPhysicalCommit(root),expectedPreview)
    if(!await recheck('before_lock') || !await same())return result()
    const {home,git}=await rooted(root)
    try {
      objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
      lock=await open(proc(git.fd,'index.lock'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
      lockCreated=true
      const ls=await lock.stat();lockIdentity={device:ls.dev,inode:ls.ino}
      const anchored=async()=>{const held=await lock!.stat(),current=await lstat(proc(git.fd,'index.lock'));if(!lockIdentity || !held.isFile() || !current.isFile() || held.dev!==lockIdentity.device || held.ino!==lockIdentity.inode || current.dev!==lockIdentity.device || current.ino!==lockIdentity.inode)throw Error('lock changed');const value=await namespaces(root,home,git,objects!);if(!isDeepStrictEqual(value,{root:expectedPreview.physicalIndex.root,git:expectedPreview.physicalIndex.git,objects:expectedPreview.physicalIndex.objects}))throw Error('namespace changed')}
      await anchored();if(!await same())throw Error('preview changed')
      const dirname=`openclank-commit-${candidateToken}`
      await mkdir(proc(git.fd,dirname),{mode:0o700});candidateRetained=true
      directory=await open(proc(git.fd,dirname),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
      const di=directoryIdentity(await directory.stat());candidateIdentity={token:candidateToken,...di}
      const call=(args:string[],input=Buffer.alloc(0),index=false,limit=4096,withIdentity=false)=>runGit(home.fd,git.fd,directory!.fd,objects!.fd,args,input,index,expectedPreview.executable.path,limit,withIdentity?{author:action.author,committer:action.committer}:undefined)
      if(!await recheck('before_tree') || !await same())throw Error('authority changed');await anchored()
      await call(['read-tree','--empty'],Buffer.alloc(0),true)
      const wire=Buffer.concat(expectedPreview.indexEntries.map(e=>Buffer.concat([Buffer.from(`${e.mode.toString(8)} ${e.oid} 0\t`),Buffer.from(e.path),Buffer.from([0])])))
      if(wire.length>MAX_INDEX)throw Error('index input too large')
      if(wire.length)await call(['update-index','-z','--index-info'],wire,true)
      const candidate=await readIndex(directory.fd);ownedCandidate={device:candidate.device,inode:candidate.inode}
      await anchored();ownedCandidate=undefined
      tree='unknown';const treeText=new TextDecoder('utf-8',{fatal:true}).decode(await call(['write-tree'],Buffer.alloc(0),true))
      const width=expectedPreview.physicalIndex.objectFormat==='sha1'?40:64
      if(!new RegExp(`^[0-9a-f]{${width}}\\n$`).test(treeText))throw Error('tree unavailable')
      const treeOID=treeText.slice(0,-1);tree='written_or_present'
      const settledCandidate=await readIndex(directory.fd),settledParsed=parse(settledCandidate.bytes)
      if(settledParsed.kind!==expectedPreview.physicalIndex.objectFormat || !isDeepStrictEqual(settledParsed.entries.map(e=>({path:e.name.toString('utf8'),mode:e.mode,oid:e.oid.toString('hex')})),expectedPreview.indexEntries))throw Error('candidate changed')
      ownedCandidate={device:settledCandidate.device,inode:settledCandidate.inode}
      if(!isDeepStrictEqual(records(await call(['ls-tree','-r','-z','--full-tree',treeOID],Buffer.alloc(0),false,MAX_INDEX),'tree',width),expectedPreview.indexEntries))throw Error('tree mismatch')
      if(!await recheck('before_commit') || !await same())throw Error('authority changed');await anchored()
      commit='unknown';const output=new TextDecoder('utf-8',{fatal:true}).decode(await call(['commit-tree','--no-gpg-sign',treeOID,'-p',expectedPreview.headOID,'-F','-'],Buffer.from(action.message),false,4096,true))
      if(!new RegExp(`^[0-9a-f]{${width}}\\n$`).test(output))throw Error('commit unavailable')
      commitOID=output.slice(0,-1);commit='written_or_present'
      const raw=await call(['cat-file','commit',commitOID],Buffer.alloc(0),false,32768),header=(label:string,i:CommitIdentity)=>`${label} ${i.name} <${i.email}> ${i.seconds} ${i.timezone}\n`
      const expected=Buffer.from(`tree ${treeOID}\nparent ${expectedPreview.headOID}\n${header('author',action.author)}${header('committer',action.committer)}\n${action.message}`)
      if(!raw.equals(expected) || hash(expectedPreview.physicalIndex.objectFormat,Buffer.concat([Buffer.from(`commit ${raw.length}\0`),raw])).toString('hex')!==commitOID)throw Error('commit mismatch')
      if(!await recheck('before_publish') || !await same())throw Error('authority changed');await anchored()
      const transaction=Buffer.from(`start\0option no-deref\0update ${expectedPreview.headRef}\0${commitOID}\0${expectedPreview.headOID}\0prepare\0commit\0`)
      ref='unknown';const acknowledgement=await call(['update-ref','-m','openclank reviewed commit','--stdin','-z'],transaction,false,4096,true);if(!acknowledgement.equals(Buffer.from('start: ok\nprepare: ok\ncommit: ok\n')))throw Error('ref acknowledgement unavailable');ref='applied'
    } catch { /* retain truthful effect states */ }
    finally {
      if(directory) {if(ownedCandidate) {const current=await lstat(proc(directory.fd,'index')).catch(()=>undefined);if(current?.isFile() && current.dev===ownedCandidate.device && current.ino===ownedCandidate.inode) {try {await unlink(proc(directory.fd,'index'))} catch {cleanup='unknown'}} else cleanup='unknown'}
        await directory.close().catch(()=>{cleanup='unknown'});directory=undefined
        if(candidateIdentity) {try {const current=directoryIdentity(await lstat(proc(git.fd,`openclank-commit-${candidateToken}`)));if(current.device!==candidateIdentity.device || current.inode!==candidateIdentity.inode)throw Error('candidate changed');await rmdir(proc(git.fd,`openclank-commit-${candidateToken}`));candidateRetained=false;candidateIdentity=undefined} catch {cleanup='unknown'}}}
      if(lock)await lock.close().catch(()=>{cleanup='unknown'})
      if(lockIdentity) {try {const current=await lstat(proc(git.fd,'index.lock'));if(!current.isFile() || current.dev!==lockIdentity.device || current.ino!==lockIdentity.inode)throw Error('lock changed');await unlink(proc(git.fd,'index.lock'))} catch {cleanup='unknown'}}
      if((lockCreated && !lockIdentity) || candidateRetained)cleanup='unknown'
      if(objects)await objects.close().catch(()=>{cleanup='unknown'});await git.close().catch(()=>{cleanup='unknown'});await home.close().catch(()=>{cleanup='unknown'})
    }
  } catch { /* no effect unless states above say otherwise */ }
  return result()
}

/** Read-only, plain-blob candidate. This is not a Git checkout or mutation grant.
 * Raw object OIDs/sizes are provisional until the journal-owned byte stream
 * verifies each object and computes the container's SHA-256 commitments. */
export async function previewPhysicalWorktree(root:string,targetOID:string,deadlineAt?:number) {
  const source=await previewPhysicalCommit(root,deadlineAt),width=source.physicalIndex.objectFormat==='sha1'?40:64
  if(!new RegExp(`^[0-9a-f]{${width}}$`).test(targetOID))throw Error('worktree commit unavailable')
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expected={root:source.physicalIndex.root,git:source.physicalIndex.git,objects:source.physicalIndex.objects}
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expected))throw Error('repository changed')
    const call=(args:string[],input=Buffer.alloc(0),limit=MAX_INDEX)=>runGit(home.fd,git.fd,git.fd,objects!.fd,args,input,false,source.executable.path,limit,undefined,false,undefined,deadlineAt)
    if((await call(['cat-file','-t',targetOID],Buffer.alloc(0),4096)).toString('ascii')!=='commit\n')throw Error('worktree commit unavailable')
    const treeText=(await call(['rev-parse','--verify',`${targetOID}^{tree}`],Buffer.alloc(0),4096)).toString('ascii')
    if(!new RegExp(`^[0-9a-f]{${width}}\\n$`).test(treeText))throw Error('worktree tree unavailable')
    const entries=records(await call(['ls-tree','-r','-z','--full-tree',targetOID]),'tree',width)
    const dirs=new Set<string>()
    for(const entry of entries) {
      if(entry.path.includes('\\'))throw Error('worktree path unavailable')
      const parts=entry.path.split('/');for(let n=1;n<parts.length;n++)dirs.add(parts.slice(0,n).join('/'))
    }
    // Include the explicit external-Git-owner .git marker in the eventual container.
    if(entries.length+dirs.size+1>8192)throw Error('worktree entry bound')
    for(let base=0;base<entries.length;base+=64) {
      const group=entries.slice(base,base+64),paths=group.map(e=>e.path),names=['filter','text','eol','working-tree-encoding','ident']
      const data=await call(['check-attr','-z',`--source=${targetOID}`,...names,'--',...paths])
      if(!data.length || data[data.length-1]!==0)throw Error('worktree attributes unavailable')
      const fields=new TextDecoder('utf-8',{fatal:true}).decode(data).split('\0');fields.pop()
      if(fields.length!==paths.length*names.length*3)throw Error('worktree attributes incomplete')
      const seen=new Set<string>()
      for(let i=0;i<fields.length;i+=3) {
        const [path,name,value]=fields.slice(i,i+3),key=JSON.stringify([path,name])
        if(!paths.includes(path!) || !names.includes(name!) || !['unspecified','unset'].includes(value!) || seen.has(key))throw Error('worktree conversion unavailable')
        seen.add(key)
      }
    }
    let total=0;const sizes:number[]=[]
    if(entries.length) {
      const input=Buffer.from(entries.map(e=>e.oid+'\n').join(''))
      const output=new TextDecoder('utf-8',{fatal:true}).decode(await call(['cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)'],input))
      if(!output.endsWith('\n'))throw Error('worktree object metadata unavailable')
      const lines=output.slice(0,-1).split('\n');if(lines.length!==entries.length)throw Error('worktree object metadata incomplete')
      for(let i=0;i<lines.length;i++) {
        const match=/^([0-9a-f]+) blob (0|[1-9][0-9]*)$/.exec(lines[i]!)
        if(!match || match[1]!==entries[i]!.oid)throw Error('worktree object unavailable')
        const size=Number(match[2]);if(!Number.isSafeInteger(size) || size>32*1024*1024)throw Error('worktree blob bound')
        total+=size;if(!Number.isSafeInteger(total) || total>512*1024*1024)throw Error('worktree byte bound');sizes.push(size)
      }
    }
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expected) || !isDeepStrictEqual(await previewPhysicalCommit(root,deadlineAt),source))throw Error('repository changed')
    return {version:1 as const,profile:'plain_blob' as const,source,targetOID,treeOID:treeText.slice(0,-1),entries:entries.map((entry,i):{path:string;mode:33188|33261;oid:string;size:number}=>{if(entry.mode!==33188 && entry.mode!==33261)throw Error('worktree mode unavailable');return {...entry,mode:entry.mode,size:sizes[i]!}}),directories:[...dirs].sort((a,b)=>Buffer.compare(Buffer.from(a),Buffer.from(b))),totalBytes:total}
  } finally {if(objects)await objects.close();await git.close();await home.close()}
}

/** Internal-only Source stream; the caller must retain the original utility,
 * authenticate each callback and implement abortAndDrain without releasing
 * custody while an issued callback can still mutate. If drained is false the
 * caller must retain that original utility. No wire is mounted here. */
export type WorktreeStageEvent =
  | {kind:'begin';targetOID:string;treeOID:string;entries:{path:string;mode:33188|33261;oid:string;size:number}[];directories:string[];totalBytes:number}
  | {kind:'blob_chunk';ordinal:number;offset:number;contentBase64:string}
  | {kind:'blob_end';ordinal:number;gitOID:string;sha256:string}
  | {kind:'seal_target'}
export type WorktreeStageReply = {state:'accepted';nextOrdinal:number;nextOffset:number}|{state:'sealed';targetManifestSHA256:string}|{state:'refused'|'unknown'}
export type WorktreeStageTransport = {invoke:(event:WorktreeStageEvent)=>Promise<WorktreeStageReply>;abortAndDrain:()=>Promise<void>}
export async function streamPhysicalWorktree(root:string,expected:Awaited<ReturnType<typeof previewPhysicalWorktree>>,timeoutMs:number,transport:WorktreeStageTransport):Promise<{state:'sealed';targetManifestSHA256:string}|{state:'refused'}|{state:'unknown';drained:boolean}> {
  if(!Number.isSafeInteger(timeoutMs) || timeoutMs<=0 || timeoutMs>300_000)return {state:'refused'}
  const deadlineAt=performance.now()+timeoutMs
  let issued=false,child:ReturnType<typeof spawn>|undefined,timer:ReturnType<typeof setTimeout>|undefined,childClosed:Promise<number|null>|undefined
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined
  let pending:Promise<WorktreeStageReply>|undefined,expired=false,expire!:()=>void
  const timeout=new Promise<never>((_,reject)=>{expire=()=>{expired=true;child?.kill('SIGKILL');reject(Error('worktree deadline'))}})
  // Attach immediately so an expiry during local preflight never becomes an
  // unhandled rejection. A caller retains custody through abortAndDrain.
  timeout.catch(()=>{})
  try {
    if(!isDeepStrictEqual(await previewPhysicalWorktree(root,expected.targetOID,deadlineAt),expected) || performance.now()>=deadlineAt)return {state:'refused'}
    const manifestEntries=new Map<string,unknown>();let objectOffset=0
    for(const path of expected.directories)manifestEntries.set(path,{kind:'directory',path,mode:0o755})
    for(const entry of expected.entries){manifestEntries.set(entry.path,{kind:'file',path:entry.path,mode:entry.mode&0o777,offset:objectOffset,length:entry.size,sha256:'0'.repeat(64)});objectOffset+=entry.size}
    if(objectOffset!==expected.totalBytes || objectOffset>512*1024*1024-1024*1024)return {state:'refused'}
    const entries=[...manifestEntries].sort((a,b)=>Buffer.compare(Buffer.from(a[0]),Buffer.from(b[0]))).map(([,entry])=>entry)
    entries.push({kind:'git_metadata',path:'.git',mode:0o644,offset:objectOffset,length:1024*1024,sha256:'0'.repeat(64),owner:'source_worktree'})
    const manifest={version:2,source:{commit_oid:expected.targetOID,tree_oid:expected.treeOID,target_manifest_sha256:'0'.repeat(64)},entries}
    const begin:WorktreeStageEvent={kind:'begin',targetOID:expected.targetOID,treeOID:expected.treeOID,entries:expected.entries,directories:expected.directories,totalBytes:expected.totalBytes}
    if(Buffer.byteLength(JSON.stringify(manifest))>4*1024*1024 || Buffer.byteLength(JSON.stringify(begin))>3*1024*1024)return {state:'refused'}
    const rootedValue=await rooted(root);home=rootedValue.home;git=rootedValue.git
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expectedNS={root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || performance.now()>=deadlineAt)return {state:'refused'}
    const env:Record<string,string>={PATH:process.env.PATH??'/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ATTR_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_LITERAL_PATHSPECS:'1',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
    child=spawn(expected.source.executable.path,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.hooksPath=/dev/null','-c','credential.helper=','cat-file','--batch'],{cwd:proc(home.fd),env,stdio:['pipe','pipe','pipe',git.fd,objects.fd,git.fd,home.fd]})
    childClosed=new Promise<number|null>(resolve=>child!.once('close',resolve))
    const {stdin,stdout,stderr}=child;if(!stdin || !stdout || !stderr)throw Error('Git pipes unavailable')
    let failed=false;child.on('error',()=>{failed=true});stdin.on('error',()=>{failed=true});stderr.resume()
    timer=setTimeout(expire,Math.max(1,deadlineAt-performance.now()))
    const source=stdout[Symbol.asyncIterator]();let buffered=Buffer.alloc(0)
    const take=async (wanted:number):Promise<Buffer>=>{
      while(!buffered.length) {const next=await Promise.race([source.next(),timeout]);if(next.done)throw Error('Git object stream ended');buffered=Buffer.from(next.value);if(buffered.length>1024*1024)throw Error('Git stream chunk unavailable')}
      const count=Math.min(wanted,buffered.length),value=buffered.subarray(0,count);buffered=buffered.subarray(count);return value
    }
    const line=async ()=>{const parts:Buffer[]=[];for(let n=0;n<256;n++){const value=await take(1);if(value[0]===10)return Buffer.concat(parts).toString('ascii');parts.push(value)}throw Error('Git header unavailable')}
    const dispatch=async (event:WorktreeStageEvent)=>{if(expired || performance.now()>=deadlineAt)throw Error('worktree deadline');issued=true;pending=transport.invoke(event);try{return await Promise.race([pending,timeout])}finally{if(!expired)pending=undefined}}
    const accepted=async (event:WorktreeStageEvent,ordinal:number,offset:number)=>{const reply=await dispatch(event);if(reply.state!=='accepted' || reply.nextOrdinal!==ordinal || reply.nextOffset!==offset)throw Error('stage acknowledgement unavailable')}
    await accepted(begin,0,0);stdin.end(Buffer.from(expected.entries.map(entry=>entry.oid+'\n').join('')))
    for(let ordinal=0;ordinal<expected.entries.length;ordinal++) {
      const entry=expected.entries[ordinal]!;if(await line()!==`${entry.oid} blob ${entry.size}`)throw Error('Git object changed')
      const oid=createHash(expected.source.physicalIndex.objectFormat).update(`blob ${entry.size}\0`),sha=createHash('sha256');let offset=0
      while(offset<entry.size) {const bytes=await take(Math.min(128*1024,entry.size-offset));oid.update(bytes);sha.update(bytes);await accepted({kind:'blob_chunk',ordinal,offset,contentBase64:bytes.toString('base64')},ordinal,offset+bytes.length);offset+=bytes.length}
      if((await take(1))[0]!==10 || oid.digest('hex')!==entry.oid)throw Error('Git object verification failed')
      await accepted({kind:'blob_end',ordinal,gitOID:entry.oid,sha256:sha.digest('hex')},ordinal+1,0)
    }
    if(buffered.length || !(await Promise.race([source.next(),timeout])).done || await Promise.race([childClosed,timeout])!==0 || failed)throw Error('Git stream unavailable')
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await previewPhysicalWorktree(root,expected.targetOID,deadlineAt),expected) || expired || performance.now()>=deadlineAt)throw Error('repository changed')
    const sealed=await dispatch({kind:'seal_target'});if(sealed.state!=='sealed' || !/^[0-9a-f]{64}$/.test(sealed.targetManifestSHA256))throw Error('stage seal unavailable')
    return {state:'sealed',targetManifestSHA256:sealed.targetManifestSHA256}
  } catch {
    if(issued) {try {await transport.abortAndDrain();if(pending)await Promise.allSettled([pending]);return {state:'unknown',drained:true}} catch {return {state:'unknown',drained:false}}}
    return {state:'refused'}
  } finally {if(timer)clearTimeout(timer);if(child && child.exitCode===null){child.stdin?.destroy();child.stdout?.resume();child.stderr?.resume();child.kill('SIGKILL')}if(childClosed)await childClosed;if(objects)await objects.close().catch(()=>{});if(git)await git.close().catch(()=>{});if(home)await home.close().catch(()=>{})}
}

type PrivateIdentity={device:string;inode:string}
const privateIdentityOf=(v:{dev:bigint;ino:bigint;isDirectory:()=>boolean}):PrivateIdentity=>{
  if(!v.isDirectory() || v.dev<0n || v.dev>0xffffffffffffffffn || v.ino<=0n || v.ino>0xffffffffffffffffn)throw Error('private identity unavailable')
  return {device:v.dev.toString(),inode:v.ino.toString()}
}
async function privateFileWitness(path:string,limit=4096) {
  const file=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK)
  try {
    const before=await file.stat({bigint:true})
    if(!before.isFile() || before.nlink!==1n || before.dev<0n || before.dev>0xffffffffffffffffn || before.ino<=0n || before.ino>0xffffffffffffffffn || before.size<=0n || before.size>BigInt(limit))throw Error('private file unavailable')
    const bytes=await readBounded(file,Number(before.size)),after=await file.stat({bigint:true}),current=await lstat(path,{bigint:true})
    if(!current.isFile() || before.dev!==after.dev || before.ino!==after.ino || before.size!==after.size || before.mtimeNs!==after.mtimeNs || before.ctimeNs!==after.ctimeNs || before.dev!==current.dev || before.ino!==current.ino)throw Error('private file changed')
    return {witness:{device:before.dev.toString(),inode:before.ino.toString(),size:Number(before.size),sha256:hash('sha256',bytes).toString('hex')},bytes}
  } finally {await file.close()}
}

/** A Source-only private registration. The caller must verify the operation's
 * journal witness in its retained Host callback immediately before `authorize`
 * returns true. It must never supply the public destination as `privatePath`.
 * Git failure cleanup can affect only this operation-owned private directory.
 */
export async function registerPrivateWorktree(root:string,expected:Awaited<ReturnType<typeof previewPhysicalWorktree>>,privatePath:string,journalDirectory:string,operationID:string,privateIdentity:PrivateIdentity,deadlineAt:number,authorize:()=>Promise<boolean>) {
  let issued=false,home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,privateDir:Awaited<ReturnType<typeof open>>|undefined
  try {
    if(!/^[A-Za-z0-9_-]{16,80}$/.test(operationID) || !journalDirectory.startsWith('/') || privatePath!==join(journalDirectory,`${operationID}.worktree-private`) || await realpath(journalDirectory)!==journalDirectory || await realpath(privatePath)!==privatePath || performance.now()>=deadlineAt)return {state:'refused' as const}
    privateDir=await open(privatePath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const privateStat=await privateDir.stat({bigint:true});if(!isDeepStrictEqual(privateIdentityOf(privateStat),privateIdentity) || (privateStat.mode&0o777n)!==0o700n || (await readdir(privatePath)).length!==0)return {state:'refused' as const}
    const value=await rooted(root);home=value.home;git=value.git;objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expectedNS={root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await previewPhysicalWorktree(root,expected.targetOID,deadlineAt),expected) || !isDeepStrictEqual(privateIdentityOf(await lstat(privatePath,{bigint:true})),privateIdentity) || performance.now()>=deadlineAt || await authorize()!==true)return {state:'refused' as const}
    // After issue, a failure or missing ACK can have created Git metadata.
    issued=true
    await runGit(home.fd,git.fd,git.fd,objects.fd,['worktree','add','--no-checkout','--detach','--',privatePath,expected.targetOID],Buffer.alloc(0),false,expected.source.executable.path,64*1024,undefined,false,undefined,deadlineAt)
    if(!isDeepStrictEqual(privateIdentityOf(await privateDir.stat({bigint:true})),privateIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(privatePath,{bigint:true})),privateIdentity) || !isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS))throw Error('private registration changed')
    const markerPath=join(privatePath,'.git'),markerRead=await privateFileWitness(markerPath),markerWitness=markerRead.witness,marker=markerRead.bytes
    const prefix=join(root,'.git','worktrees')+'/',text=new TextDecoder('utf-8',{fatal:true}).decode(marker)
    if(!text.startsWith('gitdir: '+prefix) || !text.endsWith('\n') || text.slice(8,-1).includes('\n'))throw Error('registration marker unavailable')
    const adminPath=text.slice(8,-1),name=adminPath.slice(prefix.length)
    if(!name || Buffer.byteLength(name,'utf8')>255 || /[\x00-\x1f\x7f\\/]/.test(name) || name==='.' || name==='..' || await realpath(adminPath)!==adminPath)throw Error('registration admin unavailable')
    const admin=await open(adminPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    try {
      const adminIdentity=privateIdentityOf(await admin.stat({bigint:true})),backlinkPath=join(adminPath,'gitdir'),headPath=join(adminPath,'HEAD')
      const backlinkRead=await privateFileWitness(backlinkPath),headRead=await privateFileWitness(headPath)
      if(!backlinkRead.bytes.equals(Buffer.from(privatePath+'/.git\n')) || !headRead.bytes.equals(Buffer.from(expected.targetOID+'\n')) || !isDeepStrictEqual((await privateFileWitness(markerPath)).witness,markerWitness) || !isDeepStrictEqual((await privateFileWitness(backlinkPath)).witness,backlinkRead.witness) || !isDeepStrictEqual((await privateFileWitness(headPath)).witness,headRead.witness))throw Error('registration metadata unavailable')
      if(!isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(privatePath,{bigint:true})),privateIdentity))throw Error('registration identity changed')
      return {state:'registered' as const,privateIdentity,marker:{...markerWitness,bytesBase64:marker.toString('base64')},adminName:name,adminIdentity,backlink:backlinkRead.witness,head:headRead.witness}
    } finally {await admin.close()}
  } catch {return {state:issued?'unknown' as const:'refused' as const}}
  finally {if(privateDir)await privateDir.close().catch(()=>{});if(objects)await objects.close().catch(()=>{});if(git)await git.close().catch(()=>{});if(home)await home.close().catch(()=>{})}
}

/** Populate only the original private linked-worktree index via Git plumbing.
 * The retained Host callback must durably record this issue before returning. */
export async function populatePrivateWorktreeIndex(root:string,privatePath:string,expected:Awaited<ReturnType<typeof previewPhysicalWorktree>>,registration:Extract<Awaited<ReturnType<typeof registerPrivateWorktree>>,{state:'registered'}>,deadlineAt:number,authorize:()=>Promise<boolean>) {
  let issued=false,home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,admin:Awaited<ReturnType<typeof open>>|undefined,privateDir:Awaited<ReturnType<typeof open>>|undefined
  try {
    const adminPath=join(root,'.git','worktrees',registration.adminName)
    if(performance.now()>=deadlineAt || await realpath(adminPath)!==adminPath || await realpath(privatePath)!==privatePath)return {state:'refused' as const}
    const value=await rooted(root);home=value.home;git=value.git;objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    admin=await open(adminPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);privateDir=await open(privatePath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expectedNS={root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}
    const marker=await privateFileWitness(proc(privateDir.fd,'.git')),head=await privateFileWitness(proc(admin.fd,'HEAD')),backlink=await privateFileWitness(proc(admin.fd,'gitdir'))
    const {bytesBase64,...markerIdentity}=registration.marker
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await privateDir.stat({bigint:true})),registration.privateIdentity) || !isDeepStrictEqual(marker.witness,markerIdentity) || marker.bytes.toString('base64')!==bytesBase64 || !isDeepStrictEqual(head.witness,registration.head) || !isDeepStrictEqual(backlink.witness,registration.backlink) || !isDeepStrictEqual(await previewPhysicalWorktree(root,expected.targetOID,deadlineAt),expected) || performance.now()>=deadlineAt || await authorize()!==true)return {state:'refused' as const}
    // Git writes only its own private admin index; it never checks out a file.
    issued=true
    await runGit(privateDir.fd,admin.fd,admin.fd,objects.fd,['read-tree',expected.targetOID],Buffer.alloc(0),false,expected.source.executable.path,4096,undefined,false,undefined,deadlineAt,300000)
    const listing=records(await runGit(privateDir.fd,admin.fd,admin.fd,objects.fd,['ls-files','--stage','-z'],Buffer.alloc(0),false,expected.source.executable.path,MAX_INDEX,undefined,false,undefined,deadlineAt,300000),'index',expected.targetOID.length)
    const tree=expected.entries.map(entry=>({path:entry.path,mode:entry.mode,oid:entry.oid}))
    if(!isDeepStrictEqual(listing,tree) || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await privateDir.stat({bigint:true})),registration.privateIdentity) || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'HEAD'))).witness,registration.head) || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'gitdir'))).witness,registration.backlink))throw Error('private index changed')
    const index=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024),parsed=parse(index.bytes)
    const exact=parsed.entries.map(entry=>({path:entry.name.toString('utf8'),mode:entry.mode,oid:entry.oid.toString('hex')}))
    if(parsed.kind!==expected.source.physicalIndex.objectFormat || !isDeepStrictEqual(exact,tree))throw Error('private index bytes changed')
    return {state:'indexed' as const,index:index.witness}
  } catch {return {state:issued?'unknown' as const:'refused' as const}}
  finally {if(privateDir)await privateDir.close().catch(()=>{});if(admin)await admin.close().catch(()=>{});if(objects)await objects.close().catch(()=>{});if(git)await git.close().catch(()=>{});if(home)await home.close().catch(()=>{})}
}

type RegisteredWorktreeWitness = Extract<Awaited<ReturnType<typeof registerPrivateWorktree>>,{state:'registered'}> & {index:{device:string;inode:string;size:number;sha256:string}}
/** Witnesses are retained by this Source child while the callback runs. The
 * caller must bind them to the original reviewed Host operation and journal. */
export type GuardedPublishedWorktreeProof = {
  adminName:string; adminIdentity:PrivateIdentity; worktreeIdentity:PrivateIdentity; commonGitIdentity:PrivateIdentity; worktreesIdentity:PrivateIdentity;
  targetOID:string; treeOID:string;
  marker:RegisteredWorktreeWitness['marker']; backlink:RegisteredWorktreeWitness['backlink'];
  head:RegisteredWorktreeWitness['head']; index:RegisteredWorktreeWitness['index'];
  locks:{head:PrivateIdentity;backlink:PrivateIdentity;index:PrivateIdentity};
}

/** Read-only semantic/index guard for the unchanged-worktree inverse. The
 * callback is not mounted until its Host and Native journal owners are joined.
 * Lock files remain held while an issued callback drains, including on timeout. */
export async function guardPublishedWorktree(
  root:string,worktreePath:string,expected:Awaited<ReturnType<typeof previewPhysicalWorktree>>,
  registration:RegisteredWorktreeWitness, finalBacklink:RegisteredWorktreeWitness['backlink'], timeoutMs:number,
  original:{originalOperationID:string;originalVersionID:string},
  transport:{invoke:(event:GuardedPublishedWorktreeProof & {kind:'before_quarantine'|'before_admin_relocation'|'admin_relocated';originalOperationID:string;originalVersionID:string})=>Promise<{state:'quarantined'|'admin_relocated'|'completed'|'refused'|'unknown'}>;abortAndDrain:()=>Promise<void>},
):Promise<{state:'completed'|'refused'|'unknown';drained?:boolean}> {
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined
  let worktrees:Awaited<ReturnType<typeof open>>|undefined,admin:Awaited<ReturnType<typeof open>>|undefined,target:Awaited<ReturnType<typeof open>>|undefined
  const locks:{name:string;file:Awaited<ReturnType<typeof open>>;id:PrivateIdentity}[]=[]
  let issued=false,cleanup=true,result:{state:'completed'|'refused'|'unknown';drained?:boolean}={state:'refused'}
  const deadline=performance.now()+timeoutMs
  const lockIdentity=(stat:{dev:bigint;ino:bigint;nlink:bigint;isFile:()=>boolean}):PrivateIdentity=>{
    if(!stat.isFile() || stat.nlink!==1n || stat.dev<0n || stat.dev>0xffffffffffffffffn || stat.ino<=0n || stat.ino>0xffffffffffffffffn)throw Error('lock unavailable')
    return {device:stat.dev.toString(),inode:stat.ino.toString()}
  }
  try {
    if(!original.originalOperationID || !original.originalVersionID || Buffer.byteLength(original.originalOperationID,'utf8')>256 || Buffer.byteLength(original.originalVersionID,'utf8')>256
      || !Number.isSafeInteger(timeoutMs) || timeoutMs<1 || timeoutMs>300000 || !worktreePath.startsWith('/') || await realpath(worktreePath)!==worktreePath
      || !registration.adminName || Buffer.byteLength(registration.adminName,'utf8')>255 || /[\x00-\x1f\x7f\\/]/.test(registration.adminName)
      || registration.adminName==='.' || registration.adminName==='..')throw Error('worktree unavailable')
    const rootedValue=await rooted(root);home=rootedValue.home;git=rootedValue.git
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    worktrees=await open(proc(git.fd,'worktrees'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    admin=await open(proc(worktrees.fd,registration.adminName),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    target=await open(worktreePath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expectedNS={root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}
    const {version:_version,...expectedExecutable}=expected.source.executable
    const commonGitIdentity=privateIdentityOf(await git.stat({bigint:true})),worktreesIdentity=privateIdentityOf(await worktrees.stat({bigint:true}))
    if(!isDeepStrictEqual(privateIdentityOf(await lstat(proc(git.fd,'worktrees'),{bigint:true})),worktreesIdentity))throw Error('worktree registry changed')
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await gitExecutable(),expectedExecutable)
      || !isDeepStrictEqual(await fileWitness(proc(git.fd,'config'),128*1024),expected.source.config)
      || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(proc(worktrees.fd,registration.adminName),{bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await target.stat({bigint:true})),registration.privateIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(worktreePath,{bigint:true})),registration.privateIdentity))throw Error('worktree namespace changed')
    for(const name of ['HEAD.lock','gitdir.lock','index.lock']) {
      const path=proc(admin.fd,name),file=await open(path,constants.O_RDWR|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600)
      const id=lockIdentity(await file.stat({bigint:true}));locks.push({name,file,id})
      if(!isDeepStrictEqual(lockIdentity(await lstat(path,{bigint:true})),id))throw Error('lock changed')
    }
    const marker=await privateFileWitness(proc(target.fd,'.git')),head=await privateFileWitness(proc(admin.fd,'HEAD'))
    const backlink=await privateFileWitness(proc(admin.fd,'gitdir')),index=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)
    const {bytesBase64,...markerIdentity}=registration.marker
    if(!isDeepStrictEqual(marker.witness,markerIdentity) || marker.bytes.toString('base64')!==bytesBase64
      || !head.bytes.equals(Buffer.from(expected.targetOID+'\n')) || !isDeepStrictEqual(backlink.witness,finalBacklink)
      || !backlink.bytes.equals(Buffer.from(worktreePath+'/.git\n')))throw Error('worktree metadata changed')
    const tree=expected.entries.map(entry=>({path:entry.path,mode:entry.mode,oid:entry.oid})),parsed=parse(index.bytes)
    const indexed=parsed.entries.map(entry=>({path:entry.name.toString('utf8'),mode:entry.mode,oid:entry.oid.toString('hex')}))
    const listed=records(await runGit(target.fd,admin.fd,admin.fd,objects.fd,['ls-files','--stage','-z'],Buffer.alloc(0),false,expectedExecutable.path,MAX_INDEX,undefined,false,undefined,deadline,300000),'index',expected.targetOID.length)
    if(parsed.kind!==expected.source.physicalIndex.objectFormat || !isDeepStrictEqual(indexed,tree) || !isDeepStrictEqual(listed,tree)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)).witness,index.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'HEAD'))).witness,head.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'gitdir'))).witness,backlink.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(target.fd,'.git'))).witness,marker.witness)
      || !isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await gitExecutable(),expectedExecutable)
      || !isDeepStrictEqual(await fileWitness(proc(git.fd,'config'),128*1024),expected.source.config)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(proc(git.fd,'worktrees'),{bigint:true})),worktreesIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(proc(worktrees.fd,registration.adminName),{bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(worktreePath,{bigint:true})),registration.privateIdentity) || performance.now()>=deadline)throw Error('worktree guard changed')
    for(const lock of locks)if(!isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed')
    const proof:GuardedPublishedWorktreeProof={adminName:registration.adminName,adminIdentity:registration.adminIdentity,worktreeIdentity:registration.privateIdentity,commonGitIdentity,worktreesIdentity,
      targetOID:expected.targetOID,treeOID:expected.treeOID,marker:registration.marker,backlink:backlink.witness,head:head.witness,index:index.witness,
      locks:{head:locks[0]!.id,backlink:locks[1]!.id,index:locks[2]!.id}}
    const absent=async (path:string)=>{try {await lstat(path);return false} catch (error) {return (error as NodeJS.ErrnoException).code==='ENOENT'}}
    const stillHeld=async ()=>{
      if(!admin || !target || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity)
        || !isDeepStrictEqual(privateIdentityOf(await target.stat({bigint:true})),registration.privateIdentity)
        || !isDeepStrictEqual((await privateFileWitness(proc(target.fd,'.git'))).witness,marker.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'HEAD'))).witness,head.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'gitdir'))).witness,backlink.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)).witness,index.witness))throw Error('held worktree changed')
      for(const lock of locks)if(!isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed')
    }
    const phase=async (kind:'before_quarantine'|'before_admin_relocation'|'admin_relocated', expectedState:'quarantined'|'admin_relocated'|'completed')=>{
      if(performance.now()>=deadline)throw Error('worktree guard deadline')
      issued=true
      const pending=transport.invoke({kind,...original,...proof})
      let timer:ReturnType<typeof setTimeout>|undefined
      try {
        const timed=new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(Error('worktree guard deadline')),Math.max(1,deadline-performance.now()))})
        const answer=await Promise.race([pending,timed]);if(answer.state!==expectedState)throw Error('worktree phase unavailable')
      } catch (error) {
        try {await transport.abortAndDrain()} catch { /* retain locks until the issued call settles */ }
        await Promise.allSettled([pending]);throw error
      } finally {if(timer)clearTimeout(timer)}
    }
    await phase('before_quarantine','quarantined');await stillHeld()
    if(!await absent(worktreePath))throw Error('public worktree name changed')
    await phase('before_admin_relocation','admin_relocated');await stillHeld()
    if(!await absent(worktreePath) || !await absent(proc(worktrees.fd,registration.adminName)))throw Error('public worktree registration changed')
    await phase('admin_relocated','completed');result={state:'completed',drained:true}
  } catch {result={state:issued?'unknown':'refused',...(issued?{drained:true}:{})}}
  finally {
    for(const lock of locks.reverse()) {
      try {if(!admin || !isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed');await unlink(proc(admin.fd,lock.name))} catch {cleanup=false}
      await lock.file.close().catch(()=>{cleanup=false})
    }
    if(admin)await admin.close().catch(()=>{cleanup=false});if(worktrees)await worktrees.close().catch(()=>{cleanup=false});if(target)await target.close().catch(()=>{cleanup=false})
    if(objects)await objects.close().catch(()=>{cleanup=false});if(git)await git.close().catch(()=>{cleanup=false});if(home)await home.close().catch(()=>{cleanup=false})
  }
  return cleanup?result:{state:'unknown',drained:true}
}


/** Redo retains the Source lock owner while Native restores only exact journaled inodes. */
export async function guardQuarantinedWorktree(
  root:string,worktreePath:string,adminPath:string,destinationPath:string,expected:Awaited<ReturnType<typeof previewPhysicalWorktree>>,
  registration:RegisteredWorktreeWitness, finalBacklink:RegisteredWorktreeWitness['backlink'], timeoutMs:number,
  inverse:{inverseOperationID:string;inverseVersionID:string},
  transport:{invoke:(event:GuardedPublishedWorktreeProof & {kind:'before_admin_restore'|'before_tree_restore'|'redo_restored';inverseOperationID:string;inverseVersionID:string})=>Promise<{state:'admin_restored'|'tree_restored'|'completed'|'refused'|'unknown'}>;abortAndDrain:()=>Promise<void>},
):Promise<{state:'completed'|'refused'|'unknown';drained?:boolean}> {
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined
  let worktrees:Awaited<ReturnType<typeof open>>|undefined,admin:Awaited<ReturnType<typeof open>>|undefined,target:Awaited<ReturnType<typeof open>>|undefined
  const locks:{name:string;file:Awaited<ReturnType<typeof open>>;id:PrivateIdentity}[]=[]
  let issued=false,cleanup=true,result:{state:'completed'|'refused'|'unknown';drained?:boolean}={state:'refused'}
  const deadline=performance.now()+timeoutMs
  const lockIdentity=(stat:{dev:bigint;ino:bigint;nlink:bigint;isFile:()=>boolean}):PrivateIdentity=>{
    if(!stat.isFile() || stat.nlink!==1n || stat.dev<0n || stat.dev>0xffffffffffffffffn || stat.ino<=0n || stat.ino>0xffffffffffffffffn)throw Error('lock unavailable')
    return {device:stat.dev.toString(),inode:stat.ino.toString()}
  }
  try {
    if(!inverse.inverseOperationID || !inverse.inverseVersionID || Buffer.byteLength(inverse.inverseOperationID,'utf8')>256 || Buffer.byteLength(inverse.inverseVersionID,'utf8')>256
      || !adminPath.startsWith('/') || await realpath(adminPath)!==adminPath || !destinationPath.startsWith('/')
      || !Number.isSafeInteger(timeoutMs) || timeoutMs<1 || timeoutMs>300000 || !worktreePath.startsWith('/') || await realpath(worktreePath)!==worktreePath
      || !registration.adminName || Buffer.byteLength(registration.adminName,'utf8')>255 || /[\x00-\x1f\x7f\\/]/.test(registration.adminName)
      || registration.adminName==='.' || registration.adminName==='..')throw Error('worktree unavailable')
    const rootedValue=await rooted(root);home=rootedValue.home;git=rootedValue.git
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    worktrees=await open(proc(git.fd,'worktrees'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    admin=await open(adminPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    target=await open(worktreePath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const expectedNS={root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}
    const {version:_version,...expectedExecutable}=expected.source.executable
    const commonGitIdentity=privateIdentityOf(await git.stat({bigint:true})),worktreesIdentity=privateIdentityOf(await worktrees.stat({bigint:true}))
    if(!isDeepStrictEqual(privateIdentityOf(await lstat(proc(git.fd,'worktrees'),{bigint:true})),worktreesIdentity))throw Error('worktree registry changed')
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await gitExecutable(),expectedExecutable)
      || !isDeepStrictEqual(await fileWitness(proc(git.fd,'config'),128*1024),expected.source.config)
      || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await target.stat({bigint:true})),registration.privateIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(worktreePath,{bigint:true})),registration.privateIdentity))throw Error('worktree namespace changed')
    for(const name of ['HEAD.lock','gitdir.lock','index.lock']) {
      const path=proc(admin.fd,name),file=await open(path,constants.O_RDWR|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600)
      const id=lockIdentity(await file.stat({bigint:true}));locks.push({name,file,id})
      if(!isDeepStrictEqual(lockIdentity(await lstat(path,{bigint:true})),id))throw Error('lock changed')
    }
    const marker=await privateFileWitness(proc(target.fd,'.git')),head=await privateFileWitness(proc(admin.fd,'HEAD'))
    const backlink=await privateFileWitness(proc(admin.fd,'gitdir')),index=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)
    const {bytesBase64,...markerIdentity}=registration.marker
    if(!isDeepStrictEqual(marker.witness,markerIdentity) || marker.bytes.toString('base64')!==bytesBase64
      || !head.bytes.equals(Buffer.from(expected.targetOID+'\n')) || !isDeepStrictEqual(backlink.witness,finalBacklink)
      || !backlink.bytes.equals(Buffer.from(destinationPath+'/.git\n')))throw Error('worktree metadata changed')
    const tree=expected.entries.map(entry=>({path:entry.path,mode:entry.mode,oid:entry.oid})),parsed=parse(index.bytes)
    const indexed=parsed.entries.map(entry=>({path:entry.name.toString('utf8'),mode:entry.mode,oid:entry.oid.toString('hex')}))
    const listed=records(await runGit(target.fd,admin.fd,admin.fd,objects.fd,['ls-files','--stage','-z'],Buffer.alloc(0),false,expectedExecutable.path,MAX_INDEX,undefined,false,git.fd,deadline,300000,true),'index',expected.targetOID.length)
    if(parsed.kind!==expected.source.physicalIndex.objectFormat || !isDeepStrictEqual(indexed,tree) || !isDeepStrictEqual(listed,tree)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)).witness,index.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'HEAD'))).witness,head.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'gitdir'))).witness,backlink.witness)
      || !isDeepStrictEqual((await privateFileWitness(proc(target.fd,'.git'))).witness,marker.witness)
      || !isDeepStrictEqual(await namespaces(root,home,git,objects),expectedNS) || !isDeepStrictEqual(await gitExecutable(),expectedExecutable)
      || !isDeepStrictEqual(await fileWitness(proc(git.fd,'config'),128*1024),expected.source.config)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(proc(git.fd,'worktrees'),{bigint:true})),worktreesIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity)
      || !isDeepStrictEqual(privateIdentityOf(await lstat(worktreePath,{bigint:true})),registration.privateIdentity) || performance.now()>=deadline)throw Error('worktree guard changed')
    for(const lock of locks)if(!isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed')
    const proof:GuardedPublishedWorktreeProof={adminName:registration.adminName,adminIdentity:registration.adminIdentity,worktreeIdentity:registration.privateIdentity,commonGitIdentity,worktreesIdentity,
      targetOID:expected.targetOID,treeOID:expected.treeOID,marker:registration.marker,backlink:backlink.witness,head:head.witness,index:index.witness,
      locks:{head:locks[0]!.id,backlink:locks[1]!.id,index:locks[2]!.id}}
    const absent=async (path:string)=>{try {await lstat(path);return false} catch (error) {return (error as NodeJS.ErrnoException).code==='ENOENT'}}
    const stillHeld=async ()=>{
      if(!admin || !target || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity)
        || !isDeepStrictEqual(privateIdentityOf(await target.stat({bigint:true})),registration.privateIdentity)
        || !isDeepStrictEqual((await privateFileWitness(proc(target.fd,'.git'))).witness,marker.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'HEAD'))).witness,head.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'gitdir'))).witness,backlink.witness)
        || !isDeepStrictEqual((await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)).witness,index.witness))throw Error('held worktree changed')
      for(const lock of locks)if(!isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed')
    }
    const phase=async (kind:'before_admin_restore'|'before_tree_restore'|'redo_restored', expectedState:'admin_restored'|'tree_restored'|'completed')=>{
      if(performance.now()>=deadline)throw Error('worktree guard deadline')
      issued=true
      const pending=transport.invoke({kind,...inverse,...proof})
      let timer:ReturnType<typeof setTimeout>|undefined
      try {
        const timed=new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(Error('worktree guard deadline')),Math.max(1,deadline-performance.now()))})
        const answer=await Promise.race([pending,timed]);if(answer.state!==expectedState)throw Error('worktree phase unavailable')
      } catch (error) {
        try {await transport.abortAndDrain()} catch { /* retain locks until the issued call settles */ }
        await Promise.allSettled([pending]);throw error
      } finally {if(timer)clearTimeout(timer)}
    }
    const publicAdmin=proc(worktrees.fd,registration.adminName)
    if(!await absent(destinationPath) || !await absent(publicAdmin))throw Error('public worktree occupied')
    await phase('before_admin_restore','admin_restored');await stillHeld()
    if(!await absent(destinationPath) || !await absent(adminPath) || !isDeepStrictEqual(privateIdentityOf(await lstat(publicAdmin,{bigint:true})),registration.adminIdentity))throw Error('worktree admin changed')
    await phase('before_tree_restore','tree_restored');await stillHeld()
    if(!await absent(adminPath) || !await absent(worktreePath) || !isDeepStrictEqual(privateIdentityOf(await lstat(publicAdmin,{bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(destinationPath,{bigint:true})),registration.privateIdentity))throw Error('worktree publication changed')
    await phase('redo_restored','completed');result={state:'completed',drained:true}
  } catch {result={state:issued?'unknown':'refused',...(issued?{drained:true}:{})}}
  finally {
    for(const lock of locks.reverse()) {
      try {if(!admin || !isDeepStrictEqual(lockIdentity(await lock.file.stat({bigint:true})),lock.id) || !isDeepStrictEqual(lockIdentity(await lstat(proc(admin.fd,lock.name),{bigint:true})),lock.id))throw Error('lock changed');await unlink(proc(admin.fd,lock.name))} catch {cleanup=false}
      await lock.file.close().catch(()=>{cleanup=false})
    }
    if(admin)await admin.close().catch(()=>{cleanup=false});if(worktrees)await worktrees.close().catch(()=>{cleanup=false});if(target)await target.close().catch(()=>{cleanup=false})
    if(objects)await objects.close().catch(()=>{cleanup=false});if(git)await git.close().catch(()=>{cleanup=false});if(home)await home.close().catch(()=>{cleanup=false})
  }
  return cleanup?result:{state:'unknown',drained:true}
}

type WorktreePublicationEvent =
  | {kind:'before_backlink_lock';adminName:string;adminIdentity:PrivateIdentity;backlink:RegisteredWorktreeWitness['backlink'];index:RegisteredWorktreeWitness['index']}
  | {kind:'publication_lock_ready';adminName:string;adminIdentity:PrivateIdentity;lockIdentity:PrivateIdentity;candidateSHA256:string;backlink:RegisteredWorktreeWitness['backlink']}
  | {kind:'before_backlink_commit';adminIdentity:PrivateIdentity;lockIdentity:PrivateIdentity;candidateSHA256:string;index:RegisteredWorktreeWitness['index']}
  | {kind:'backlink_committed';adminName:string;adminIdentity:PrivateIdentity;backlink:RegisteredWorktreeWitness['backlink'];head:RegisteredWorktreeWitness['head'];index:RegisteredWorktreeWitness['index']}
type WorktreePublicationReply = {state:'proceed'|'published'|'completed'|'refused'|'unknown'}

/** Source's exact Git metadata owner. Git never receives the public target as
 * a destructive command argument. An unacknowledged phase retains its lock. */
export async function publishPrivateWorktreeBacklink(root:string,privatePath:string,destinationPath:string,registration:RegisteredWorktreeWitness,timeoutMs:number,transport:{invoke:(event:WorktreePublicationEvent)=>Promise<WorktreePublicationReply>;abortAndDrain:()=>Promise<void>}) {
  if(!Number.isSafeInteger(timeoutMs) || timeoutMs<=0 || timeoutMs>300000)return {state:'refused' as const}
  const deadlineAt=performance.now()+timeoutMs;let issued=false,pending:Promise<WorktreePublicationReply>|undefined,expired=false,timer:ReturnType<typeof setTimeout>|undefined
  let rejectTimeout:(error:Error)=>void=()=>{};const timeout=new Promise<never>((_,reject)=>{rejectTimeout=reject});timer=setTimeout(()=>{expired=true;rejectTimeout(Error('worktree deadline'))},timeoutMs)
  let admin:Awaited<ReturnType<typeof open>>|undefined,privateDir:Awaited<ReturnType<typeof open>>|undefined,lock:Awaited<ReturnType<typeof open>>|undefined
  const call=async(event:WorktreePublicationEvent)=>{if(expired || performance.now()>=deadlineAt)throw Error('worktree deadline');pending=transport.invoke(event);try{return await Promise.race([pending,timeout])}finally{if(!expired)pending=undefined}}
  try {
    const adminName=registration.adminName
    if(!root.startsWith('/') || !privatePath.startsWith('/') || !destinationPath.startsWith('/') || Buffer.byteLength(destinationPath,'utf8')+6>4096 || /[\x00-\x1f\x7f]/.test(destinationPath) || !adminName || Buffer.byteLength(adminName,'utf8')>255 || /[\x00-\x1f\x7f\\/]/.test(adminName) || adminName==='.' || adminName==='..')return {state:'refused' as const}
    const adminPath=join(root,'.git','worktrees',adminName),destinationMarker=join(destinationPath,'.git')
    if(await realpath(adminPath)!==adminPath || await realpath(privatePath)!==privatePath)return {state:'refused' as const}
    admin=await open(adminPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);privateDir=await open(privatePath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    if(!isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await privateDir.stat({bigint:true})),registration.privateIdentity))return {state:'refused' as const}
    const backlinkPath=proc(admin.fd,'gitdir'),headPath=proc(admin.fd,'HEAD'),lockPath=proc(admin.fd,'gitdir.lock'),privateMarker=proc(privateDir.fd,'.git')
    const oldBacklink=await privateFileWitness(backlinkPath),oldHead=await privateFileWitness(headPath),marker=await privateFileWitness(privateMarker),oldIndex=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)
    if(!isDeepStrictEqual(oldIndex.witness,registration.index) || !isDeepStrictEqual(oldBacklink.witness,registration.backlink) || !isDeepStrictEqual(oldHead.witness,registration.head) || !isDeepStrictEqual(marker.witness,((({bytesBase64,...witness})=>witness)(registration.marker))) || marker.bytes.toString('base64')!==registration.marker.bytesBase64)return {state:'refused' as const}
    const candidate=Buffer.from(destinationMarker+'\n','utf8'),candidateSHA256=hash('sha256',candidate).toString('hex')
    if((await call({kind:'before_backlink_lock',adminName,adminIdentity:registration.adminIdentity,backlink:registration.backlink,index:oldIndex.witness})).state!=='proceed')return {state:'refused' as const}
    issued=true;lock=await open(lockPath,constants.O_RDWR|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600);await lock.writeFile(candidate);await lock.sync();await admin.sync()
    const lockRead=await privateFileWitness(lockPath),lockIdentity={device:lockRead.witness.device,inode:lockRead.witness.inode}
    if(!lockRead.bytes.equals(candidate) || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity))throw Error('worktree lock changed')
    const ready=await call({kind:'publication_lock_ready',adminName,adminIdentity:registration.adminIdentity,lockIdentity,candidateSHA256,backlink:registration.backlink})
    if(ready.state!=='published')throw Error('worktree publication unavailable')
    const installed=await open(destinationPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    try {
      if(!isDeepStrictEqual(privateIdentityOf(await installed.stat({bigint:true})),registration.privateIdentity) || !isDeepStrictEqual(privateIdentityOf(await privateDir.stat({bigint:true})),registration.privateIdentity))throw Error('published worktree changed')
      const currentMarker=await privateFileWitness(proc(installed.fd,'.git')),currentBacklink=await privateFileWitness(backlinkPath),currentHead=await privateFileWitness(headPath),currentLock=await privateFileWitness(lockPath),currentIndex=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)
      if(!isDeepStrictEqual(currentMarker.witness,marker.witness) || !currentMarker.bytes.equals(marker.bytes) || !isDeepStrictEqual(currentIndex.witness,registration.index) || !isDeepStrictEqual(currentBacklink.witness,registration.backlink) || !isDeepStrictEqual(currentHead.witness,registration.head) || !isDeepStrictEqual(currentLock.witness,lockRead.witness) || !currentLock.bytes.equals(candidate) || !isDeepStrictEqual(privateIdentityOf(await admin.stat({bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity))throw Error('worktree metadata changed')
      if((await call({kind:'before_backlink_commit',adminIdentity:registration.adminIdentity,lockIdentity,candidateSHA256,index:currentIndex.witness})).state!=='proceed')throw Error('worktree commit unavailable')
      // No awaited work is interposed between the final Host check and issue.
      await rename(lockPath,backlinkPath);await admin.sync()
      const committed=await privateFileWitness(backlinkPath),head=await privateFileWitness(headPath),finalIndex=await privateFileWitness(proc(admin.fd,'index'),8*1024*1024)
      if(committed.witness.device!==lockIdentity.device || committed.witness.inode!==lockIdentity.inode || committed.witness.sha256!==candidateSHA256 || !committed.bytes.equals(candidate) || !isDeepStrictEqual(head.witness,registration.head) || !isDeepStrictEqual(finalIndex.witness,registration.index) || !isDeepStrictEqual(privateIdentityOf(await lstat(adminPath,{bigint:true})),registration.adminIdentity) || !isDeepStrictEqual(privateIdentityOf(await installed.stat({bigint:true})),registration.privateIdentity))throw Error('worktree metadata outcome unavailable')
      const final=await call({kind:'backlink_committed',adminName,adminIdentity:registration.adminIdentity,backlink:committed.witness,head:head.witness,index:finalIndex.witness})
      return final.state==='completed'?{state:'committed' as const}:{state:'unknown' as const,drained:true}
    } finally {await installed.close()}
  } catch {
    if(issued){try{await transport.abortAndDrain();if(pending)await Promise.allSettled([pending]);return {state:'unknown' as const,drained:true}}catch{return {state:'unknown' as const,drained:false}}}
    return {state:'refused' as const}
  } finally {if(timer)clearTimeout(timer);if(lock)await lock.close().catch(()=>{});if(privateDir)await privateDir.close().catch(()=>{});if(admin)await admin.close().catch(()=>{})}
}

/** Read-only complete tracked metadata for a bounded reviewed branch switch.
 * Working-file authority/cleanliness and target collisions belong to Files. */
export async function previewPhysicalCheckout(root:string,targetRef:string,operation:'switch'|'pull'='switch') {
  if((operation!=='switch' && operation!=='pull') || !(operation==='switch'?/^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/:/^refs\/remotes\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/).test(targetRef) || targetRef.includes('..') || targetRef.includes('//') || targetRef.endsWith('/') || targetRef.endsWith('.') || targetRef.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))throw Error('target branch unavailable')
  const source=await previewPhysicalCommit(root)
  if((operation==='switch' && targetRef===source.headRef) || !isDeepStrictEqual(source.indexEntries,source.headEntries))throw Error('staged changes or same branch')
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),{root:source.physicalIndex.root,git:source.physicalIndex.git,objects:source.physicalIndex.objects}))throw Error('namespace changed')
    const call=(args:string[],limit=4096)=>runGit(home.fd,git.fd,git.fd,objects!.fd,args,Buffer.alloc(0),false,source.executable.path,limit)
    await call(['check-ref-format',targetRef]);const width=source.physicalIndex.objectFormat==='sha1'?40:64
    const refdata=(await call(['for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)',targetRef],65536)).toString('utf8')
    const refRows=refdata.split('\n').filter(Boolean).map(row=>row.split('\0')), exact=refRows.filter(row=>row[0]===targetRef)
    if(exact.length!==1 || exact[0]!.length!==3 || exact[0]![2]!=='' || !new RegExp(`^[0-9a-f]{${width}}$`).test(exact[0]![1]!))throw Error('target ref unavailable')
    const targetOID=exact[0]![1]!
    if((await call(['cat-file','-t',targetOID])).toString('utf8')!=='commit\n')throw Error('target unavailable')
    if(operation==='switch') {
      const occupied=await call(['worktree','list','--porcelain','-z'],MAX_INDEX)
      if(occupied.length && occupied[occupied.length-1]!==0)throw Error('worktree list unavailable')
      for(const field of occupied.toString('utf8').split('\0'))if(field===`branch ${targetRef}`)throw Error('branch already checked out')
    } else await call(['merge-base','--is-ancestor',source.headOID,targetOID])
    const targetEntries=records(await call(['ls-tree','-r','-z','--full-tree',targetOID],MAX_INDEX),'tree',width)
    const oldMap=new Map(source.indexEntries.map(e=>[e.path,e])),newMap=new Map(targetEntries.map(e=>[e.path,e]))
    const paths=[...new Set([...oldMap.keys(),...newMap.keys()])].sort((a,b)=>Buffer.compare(Buffer.from(a),Buffer.from(b)))
    const changed=paths.filter(path=>!isDeepStrictEqual(oldMap.get(path),newMap.get(path)))
    if(changed.length>64)throw Error('checkout exceeds participant bound')
    if(changed.some(path=>path.split('/').at(-1)==='.gitattributes'))throw Error('attribute changes unavailable')
    for(const path of changed){const before=oldMap.get(path),after=newMap.get(path);if(before && after && before.mode!==after.mode)throw Error('mode change unavailable')}
    if(changed.length)for(const oid of [source.headOID,targetOID]) {
      const attrs=await call(['check-attr','-z',`--source=${oid}`,'filter','text','eol','working-tree-encoding','ident','--',...changed],MAX_INDEX)
      if(!attrs.length || attrs[attrs.length-1]!==0)throw Error('attributes unavailable')
      const fields=new TextDecoder('utf-8',{fatal:true}).decode(attrs).split('\0');fields.pop()
      if(fields.length!==changed.length*15)throw Error('attributes incomplete')
      for(let i=0;i<fields.length;i+=3)if(!changed.includes(fields[i]!) || !['filter','text','eol','working-tree-encoding','ident'].includes(fields[i+1]!) || !['unspecified','unset'].includes(fields[i+2]!))throw Error('conversion attribute unavailable')
    }
    if(!isDeepStrictEqual(await previewPhysicalCommit(root),source))throw Error('source changed')
    const again=(await call(['for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)',targetRef],65536)).toString('utf8')
    if(again!==refdata)throw Error('target changed')
    return {version:1 as const,operation,source,targetRef,targetOID,targetEntries,changed}
  } finally {if(objects)await objects.close();await git.close();await home.close()}
}

/** Create a direct branch at the reviewed HEAD. The prepared ref transaction
 * holds the destination lock across the last Host authority check. */
export async function createPhysicalBranch(root:string, expected:PhysicalCommitPreview, ref:string,
    recheck:(phase:'before_lock'|'before_publish')=>Promise<boolean>) {
  let effect:'not_issued'|'applied'|'unknown'='not_issued', cleanup:'complete'|'unknown'='complete'
  const result=()=>({state:effect==='applied'?'applied' as const:effect==='unknown'?'unknown' as const:'refused' as const,ref:effect,cleanup})
  let lock:Awaited<ReturnType<typeof open>>|undefined, lockID:{device:number;inode:number}|undefined
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    if(typeof recheck!=='function' || !/^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(ref) || ref.includes('..') || ref.includes('//') || ref.includes('@{') || ref.endsWith('/') || ref.endsWith('.') || ref.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))return result()
    const same=async()=>isDeepStrictEqual(await previewPhysicalCommit(root),expected)
    if(!await recheck('before_lock') || !await same())return result()
    ;({home,git}=await rooted(root)); objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    lock=await open(proc(git.fd,'index.lock'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
    const ls=await lock.stat();lockID={device:ls.dev,inode:ls.ino}
    const anchored=async()=>{const held=await lock!.stat(),current=await lstat(proc(git!.fd,'index.lock'));if(!lockID || !held.isFile() || !current.isFile() || held.dev!==lockID.device || held.ino!==lockID.inode || current.dev!==lockID.device || current.ino!==lockID.inode)throw Error('lock changed');const value=await namespaces(root,home!,git!,objects!);if(!isDeepStrictEqual(value,{root:expected.physicalIndex.root,git:expected.physicalIndex.git,objects:expected.physicalIndex.objects}))throw Error('namespace changed')}
    await anchored();if(!await same())throw Error('preview changed')
    await runGit(home.fd,git.fd,git.fd,objects.fd,['check-ref-format',ref],Buffer.alloc(0),false,expected.executable.path)
    const env:Record<string,string>={PATH:process.env.PATH ?? '/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ATTR_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_LITERAL_PATHSPECS:'1',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
    const child=spawn(expected.executable.path,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.hooksPath=/dev/null','-c','credential.helper=','update-ref','--stdin','-z'],{cwd:proc(home.fd),env,stdio:['pipe','pipe','pipe',git.fd,objects.fd,git.fd,home.fd]})
    const closed=new Promise<number|null>(resolve=>child.once('close',resolve));child.on('error',()=>{})
    const stdin=child.stdin,stdout=child.stdout,stderr=child.stderr
    if(!stdin || !stdout || !stderr){child.kill('SIGKILL');throw Error('ref pipes unavailable')}
    stdin.on('error',()=>{});stderr.resume()
    const lines=createInterface({input:stdout,crlfDelay:Infinity})[Symbol.asyncIterator]()
    let prepared=false, timedOut=false
    const timer=setTimeout(()=>{timedOut=true;child.kill('SIGKILL')},10000)
    try {
      stdin.write(Buffer.from(`start\0create ${ref}\0${expected.headOID}\0prepare\0`))
      if((await lines.next()).value!=='start: ok' || (await lines.next()).value!=='prepare: ok')throw Error('ref prepare unavailable')
      prepared=true
      await anchored(); if(!await same())throw Error('preview changed')
      if(!await recheck('before_publish'))throw Error('authority changed')
      effect='unknown'; stdin.end(Buffer.from('commit\0'))
      if((await lines.next()).value!=='commit: ok' || await closed!==0)throw Error('ref commit outcome unavailable')
      effect='applied'; prepared=false
    } finally {
      if(effect==='not_issued' && child.exitCode===null) {
        stdin.end(Buffer.from('abort\0'))
        const acknowledgement=await lines.next().catch(()=>({value:undefined}))
        if(acknowledgement.value!=='abort: ok' || await closed!==0)cleanup='unknown'
        else prepared=false
      }
      if(child.exitCode===null){child.kill('SIGKILL');await closed}
      if(prepared || timedOut)cleanup='unknown'
      clearTimeout(timer)
    }
  } catch { /* report only a positively acknowledged result */ }
  finally {
    if(lock)await lock.close().catch(()=>{cleanup='unknown'})
    if(lockID && git)try {const current=await lstat(proc(git.fd,'index.lock'));if(!current.isFile() || current.dev!==lockID.device || current.ino!==lockID.inode)throw Error('lock changed');await unlink(proc(git.fd,'index.lock'))} catch {cleanup='unknown'}
    if(objects)await objects.close().catch(()=>{cleanup='unknown'});if(git)await git.close().catch(()=>{cleanup='unknown'});if(home)await home.close().catch(()=>{cleanup='unknown'})
  }
  return result()
}

/** Private configured remote metadata; URLs never belong in public DTOs. */
export async function inspectPhysicalRemote(root:string, expected:PhysicalCommitPreview, remoteName:string, branch:string, forPush=false) {
  if(!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(remoteName) || !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(branch) || branch.includes('..') || branch.includes('//') || branch.endsWith('/') || branch.endsWith('.') || branch.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))throw Error('unsupported remote or branch')
  if(!isDeepStrictEqual(await previewPhysicalCommit(root),expected))throw Error('preview changed')
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const namespace=await namespaces(root,home,git,objects);if(!isDeepStrictEqual(namespace,{root:expected.physicalIndex.root,git:expected.physicalIndex.git,objects:expected.physicalIndex.objects}))throw Error('namespace changed')
    const call=(args:string[])=>runGit(home.fd,git.fd,git.fd,objects!.fd,args,Buffer.alloc(0),false,expected.executable.path,65536)
    const configured=await call(['config','--local','--no-includes','--null','--get-all',`remote.${remoteName}.url`])
    const urls=new TextDecoder('utf-8',{fatal:true}).decode(configured).split('\0');if(urls.length!==2 || urls[1]!=='' || !urls[0])throw Error('remote unavailable')
    let remoteURL=urls[0]
    if(forPush){const all=new TextDecoder('utf-8',{fatal:true}).decode(await call(['config','--local','--no-includes','--null','--list'])).split('\0').filter(Boolean);const prefix=`remote.${remoteName}.pushurl\n`;const matches=all.filter(row=>row.startsWith(prefix)).map(row=>row.slice(prefix.length));if(matches.length>1 || (matches.length===1 && !matches[0]))throw Error('push remote unavailable');if(matches.length===1)remoteURL=matches[0]!}
    const trackingRef=`refs/remotes/${remoteName}/${branch}`,sourceRef=`refs/heads/${branch}`
    if(Buffer.byteLength(remoteURL)>4096 || /[\x00-\x20\x7f]/.test(remoteURL))throw Error('remote unavailable')
    await call(['check-ref-format',trackingRef]);await call(['check-ref-format',sourceRef])
    const raw=(await call(['for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)%00',trackingRef])).toString('utf8')
    const records=raw.split('\n').filter(Boolean).map(row=>row.split('\0'));let oldOID:string|null=null
    for(const row of records) {if(row.length!==4 || row[3]!=='' || row[2]!=='' || !/^[0-9a-f]{40}([0-9a-f]{24})?$/.test(row[1]!))throw Error('tracking ref unavailable');if(row[0]===trackingRef){if(oldOID!==null)throw Error('tracking ref unavailable');oldOID=row[1]!}}
    if(!isDeepStrictEqual(await previewPhysicalCommit(root),expected))throw Error('preview changed')
    return {remoteURL,trackingRef,sourceRef,oldOID}
  } finally {if(objects)await objects.close();await git.close();await home.close()}
}

/** Read one remote ref without credentials or redirects. Local paths are
 * reached through the already-authorized held directory descriptor. */
export async function probePhysicalRemote(root:string, expected:PhysicalCommitPreview, remoteURL:string,
    sourceRef:string, localRemote:{device:number;inode:number}|null, allowMissing=false) {
  if(!/^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(sourceRef) || sourceRef.includes('..') || sourceRef.includes('//') || sourceRef.endsWith('/') || sourceRef.endsWith('.') || sourceRef.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))throw Error('source ref unavailable')
  if(!remoteURL || Buffer.byteLength(remoteURL)>4096 || /[\x00-\x20\x7f]/.test(remoteURL))throw Error('remote unavailable')
  let localPath:string|null=remoteURL.startsWith('/')?remoteURL:null
  if(!localPath){const u=new URL(remoteURL);if(!['https:','file:'].includes(u.protocol) || u.username || u.password || u.search || u.hash || (u.protocol==='file:' && u.host))throw Error('remote unavailable');if(u.protocol==='file:')localPath=fileURLToPath(u)}
  if((localPath===null)!==(localRemote===null))throw Error('remote unavailable')
  let remote:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined
  const {home,git}=await rooted(root)
  try {
    if(localPath!==null){if(await realpath(localPath)!==localPath)throw Error('remote changed');remote=await open(localPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);if(!localRemote || !sameDirectory(directoryIdentity(await remote.stat()),localRemote) || !sameDirectory(directoryIdentity(await lstat(localPath)),localRemote))throw Error('remote changed')}
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),{root:expected.physicalIndex.root,git:expected.physicalIndex.git,objects:expected.physicalIndex.objects}) || !isDeepStrictEqual(await previewPhysicalCommit(root),expected))throw Error('preview changed')
    const data=await runGit(home.fd,git.fd,git.fd,objects.fd,['-c','http.followRedirects=false','-c','protocol.file.allow=always','ls-remote','--refs',...(allowMissing?[]:['--exit-code']),localPath===null?remoteURL:'/proc/self/fd/7',sourceRef],Buffer.alloc(0),false,expected.executable.path,65536,undefined,true,remote?.fd)
    const width=expected.physicalIndex.objectFormat==='sha1'?40:64,match=new RegExp(`^([0-9a-f]{${width}})\t([^\n]+)\n$`).exec(data.toString('utf8'))
    if((!match && !(allowMissing && data.length===0)) || (match && match[2]!==sourceRef) || !isDeepStrictEqual(await previewPhysicalCommit(root),expected))throw Error('remote ref unavailable')
    if(localPath!==null && (!remote || !localRemote || await realpath(localPath)!==localPath || !sameDirectory(directoryIdentity(await remote.stat()),localRemote) || !sameDirectory(directoryIdentity(await lstat(localPath)),localRemote)))throw Error('remote changed')
    return match ? match[1]! : null
  } finally {if(remote)await remote.close();if(objects)await objects.close();await git.close();await home.close()}
}

/** Transfer one reviewed commit and publish exactly one tracking ref.
 * Caller must authenticate the private URL and recheck authority at both phases. */
export async function fetchPhysicalCommit(root:string, expected:PhysicalCommitPreview, remoteURL:string,
    trackingRef:string, oldOID:string|null, targetOID:string, localRemote:{device:number;inode:number}|null,
    recheck:(phase:'before_transfer'|'before_publish')=>Promise<boolean>) {
  let transfer:'not_issued'|'completed'|'unknown'='not_issued', ref:'not_issued'|'applied'|'unknown'='not_issued', cleanup:'complete'|'unknown'='complete'
  const result=()=>({state:ref==='applied' && transfer==='completed'?'applied' as const:ref==='unknown' || transfer==='unknown'?'unknown' as const:'refused' as const,transfer,ref,cleanup})
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,remote:Awaited<ReturnType<typeof open>>|undefined
  try {
    const width=expected.physicalIndex.objectFormat==='sha1'?40:64, oid=new RegExp(`^[0-9a-f]{${width}}$`)
    if(typeof recheck!=='function' || !oid.test(targetOID) || (oldOID!==null && !oid.test(oldOID)) || !/^refs\/remotes\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(trackingRef) || trackingRef.includes('..') || trackingRef.includes('//') || trackingRef.endsWith('/') || trackingRef.endsWith('.') || trackingRef.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))return result()
    if(typeof remoteURL!=='string' || !remoteURL || Buffer.byteLength(remoteURL)>4096 || /[\x00-\x20\x7f]/.test(remoteURL))return result()
    let localPath:string|null=remoteURL.startsWith('/')?remoteURL:null
    if(!localPath) {const u=new URL(remoteURL);if(!['https:','file:'].includes(u.protocol) || u.username || u.password || u.search || u.hash || (u.protocol==='file:' && u.host))return result();if(u.protocol==='file:')localPath=fileURLToPath(u)}
    if((localPath===null)!==(localRemote===null))return result()
    if(localPath!==null) {if(await realpath(localPath)!==localPath)return result();remote=await open(localPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);if(!localRemote || !sameDirectory(directoryIdentity(await remote.stat()),localRemote))return result()}
    const remoteCurrent=async()=>{if(localPath!==null && (!remote || !localRemote || await realpath(localPath)!==localPath || !sameDirectory(directoryIdentity(await remote.stat()),localRemote) || !sameDirectory(directoryIdentity(await lstat(localPath)),localRemote)))throw Error('remote changed')}
    const same=async()=>isDeepStrictEqual(await previewPhysicalCommit(root),expected)
    if(!await same())return result()
    ;({home,git}=await rooted(root));objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const anchored=async()=>{const value=await namespaces(root,home!,git!,objects!);if(!isDeepStrictEqual(value,{root:expected.physicalIndex.root,git:expected.physicalIndex.git,objects:expected.physicalIndex.objects}))throw Error('namespace changed')}
    const call=(args:string[],network=false)=>runGit(home!.fd,git!.fd,git!.fd,objects!.fd,args,Buffer.alloc(0),false,expected.executable.path,65536,undefined,network,network?remote?.fd:undefined)
    await anchored(); await remoteCurrent(); await call(['check-ref-format',trackingRef]); if(!await same())throw Error('preview changed')
    if(!await recheck('before_transfer'))throw Error('authority changed')
    transfer='unknown'
    await call(['-c','http.followRedirects=false','-c','protocol.file.allow=always','fetch','--quiet','--no-tags','--no-write-fetch-head','--no-auto-maintenance','--no-write-commit-graph',localPath===null?remoteURL:'/proc/self/fd/7',targetOID],true)
    transfer='completed'
    if((await call(['cat-file','-t',targetOID])).toString('utf8')!=='commit\n')throw Error('target unavailable')
    if(oldOID!==null)await call(['merge-base','--is-ancestor',oldOID,targetOID])
    const env:Record<string,string>={PATH:process.env.PATH ?? '/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ATTR_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_LITERAL_PATHSPECS:'1',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
    const child=spawn(expected.executable.path,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.hooksPath=/dev/null','-c','credential.helper=','update-ref','--stdin','-z'],{cwd:proc(home.fd),env,stdio:['pipe','pipe','pipe',git.fd,objects.fd,git.fd,home.fd]})
    const closed=new Promise<number|null>(resolve=>child.once('close',resolve));child.on('error',()=>{})
    const stdin=child.stdin,stdout=child.stdout,stderr=child.stderr
    if(!stdin || !stdout || !stderr){child.kill('SIGKILL');throw Error('ref pipes unavailable')}
    stdin.on('error',()=>{});stderr.resume()
    const lines=createInterface({input:stdout,crlfDelay:Infinity})[Symbol.asyncIterator]()
    let prepared=false,timedOut=false;const timer=setTimeout(()=>{timedOut=true;child.kill('SIGKILL')},10000)
    try {
      const change=oldOID===null?`create ${trackingRef}\0${targetOID}\0`:`update ${trackingRef}\0${targetOID}\0${oldOID}\0`
      stdin.write(Buffer.from(`start\0${change}prepare\0`))
      if((await lines.next()).value!=='start: ok' || (await lines.next()).value!=='prepare: ok')throw Error('ref prepare unavailable')
      prepared=true;await anchored();await remoteCurrent();if(!await same())throw Error('preview changed')
      if(!await recheck('before_publish'))throw Error('authority changed')
      ref='unknown';stdin.end(Buffer.from('commit\0'))
      if((await lines.next()).value!=='commit: ok' || await closed!==0)throw Error('ref outcome unavailable')
      ref='applied';prepared=false
    } finally {
      if(ref==='not_issued' && child.exitCode===null){stdin.end(Buffer.from('abort\0'));const ack=await lines.next().catch(()=>({value:undefined}));if(ack.value!=='abort: ok' || await closed!==0)cleanup='unknown';else prepared=false}
      if(child.exitCode===null){child.kill('SIGKILL');await closed}
      if(prepared || timedOut)cleanup='unknown';clearTimeout(timer)
    }
  } catch {if(transfer==='unknown')cleanup='unknown'}
  finally {if(remote)await remote.close().catch(()=>{cleanup='unknown'});if(objects)await objects.close().catch(()=>{cleanup='unknown'});if(git)await git.close().catch(()=>{cleanup='unknown'});if(home)await home.close().catch(()=>{cleanup='unknown'})}
  return result()
}

/** Push the reviewed local commit to one explicit remote branch, without force. */
export async function pushPhysicalCommit(root:string, expected:PhysicalCommitPreview, remoteURL:string,
    destinationRef:string, localRemote:{device:number;inode:number}|null,
    recheck:(phase:'before_push')=>Promise<boolean>) {
  let remoteEffect:'not_issued'|'accepted'|'unknown'='not_issued',cleanup:'complete'|'unknown'='complete'
  const result=()=>({state:remoteEffect==='accepted'?'applied' as const:remoteEffect==='unknown'?'unknown' as const:'refused' as const,remote:remoteEffect,cleanup})
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,remote:Awaited<ReturnType<typeof open>>|undefined
  try {
    if(typeof recheck!=='function' || !/^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(destinationRef) || destinationRef.includes('..') || destinationRef.includes('//') || destinationRef.endsWith('/') || destinationRef.endsWith('.') || destinationRef.split('/').some(x=>x.startsWith('.') || x.endsWith('.lock')))return result()
    if(typeof remoteURL!=='string' || !remoteURL || Buffer.byteLength(remoteURL)>4096 || /[\x00-\x20\x7f]/.test(remoteURL))return result()
    let localPath:string|null=remoteURL.startsWith('/')?remoteURL:null
    if(!localPath){const u=new URL(remoteURL);if(!['https:','file:'].includes(u.protocol) || u.username || u.password || u.search || u.hash || (u.protocol==='file:' && u.host))return result();if(u.protocol==='file:')localPath=fileURLToPath(u)}
    if((localPath===null)!==(localRemote===null))return result()
    if(localPath!==null){if(await realpath(localPath)!==localPath)return result();remote=await open(localPath,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);if(!localRemote || !sameDirectory(directoryIdentity(await remote.stat()),localRemote))return result()}
    const same=async()=>isDeepStrictEqual(await previewPhysicalCommit(root),expected)
    if(!await same())return result()
    ;({home,git}=await rooted(root));objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const namespace=await namespaces(root,home,git,objects);if(!isDeepStrictEqual(namespace,{root:expected.physicalIndex.root,git:expected.physicalIndex.git,objects:expected.physicalIndex.objects}))throw Error('namespace changed')
    if(localPath!==null && (!remote || !localRemote || await realpath(localPath)!==localPath || !sameDirectory(directoryIdentity(await remote.stat()),localRemote) || !sameDirectory(directoryIdentity(await lstat(localPath)),localRemote)))throw Error('remote changed')
    await runGit(home.fd,git.fd,git.fd,objects.fd,['check-ref-format',destinationRef],Buffer.alloc(0),false,expected.executable.path)
    if(!await same())throw Error('preview changed')
    if(!await recheck('before_push'))throw Error('authority changed')
    remoteEffect='unknown'
    await runGit(home.fd,git.fd,git.fd,objects.fd,['-c','http.followRedirects=false','-c','protocol.file.allow=always','push','--porcelain','--no-verify','--no-follow-tags','--no-recurse-submodules','--no-signed',localPath===null?remoteURL:'/proc/self/fd/7',`${expected.headOID}:${destinationRef}`],Buffer.alloc(0),false,expected.executable.path,65536,undefined,true,remote?.fd)
    remoteEffect='accepted'
  } catch {if(remoteEffect==='unknown')cleanup='unknown'}
  finally {if(remote)await remote.close().catch(()=>{cleanup='unknown'});if(objects)await objects.close().catch(()=>{cleanup='unknown'});if(git)await git.close().catch(()=>{cleanup='unknown'});if(home)await home.close().catch(()=>{cleanup='unknown'})}
  return result()
}

/** Effect boundary: `recheck` is supplied only by the retained original caller;
 * it must revalidate Once, HEAD, file, account, map and worker immediately before
 * rename. It cannot make independent HEAD/filesystem changes atomic. */
export async function applyPhysicalIndex(root:string, expected:PhysicalIndexPreview, action:IndexAction, candidateToken:string, recheck:(phase:IndexPhase)=>Promise<boolean>):Promise<IndexResult> {
  let objectState:IndexResult['object']='none', renameIssued=false, candidateRetained=false, lock:Awaited<ReturnType<typeof open>>|undefined
  let candidateIdentity:IndexResult['candidateIdentity']
  let objects:Awaited<ReturnType<typeof open>>|undefined
  let lockIdentity:{dev:number;ino:number}|undefined
  if(!/^[0-9a-f]{32}$/.test(candidateToken) || (action.bytes!==undefined && (!(action.bytes instanceof Uint8Array) || action.bytes.length>MAX_FILE)))return {state:'refused',object:'none',candidateRetained:false}
  expected={...expected,root:{...expected.root},git:{...expected.git},objects:{...expected.objects}};action={...action,...(action.bytes===undefined ? {} : {bytes:Buffer.from(action.bytes)})}
  let opened:Awaited<ReturnType<typeof rooted>>
  try {opened=await rooted(root)} catch {return {state:'refused',object:'none',candidateRetained:false}}
  const {home,git}=opened
  try {
    if(typeof recheck!=='function' || !await recheck('before_lock'))throw Error('authority unavailable')
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const original=await namespaces(root,home,git,objects)
    if(!sameDirectory(original.root,expected.root) || !sameDirectory(original.git,expected.git) || !sameDirectory(original.objects,expected.objects))throw Error('repository namespace changed')
    lock=await open(proc(git.fd,'index.lock'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
    const lockStat=await lock.stat();lockIdentity={dev:lockStat.dev,ino:lockStat.ino}
    const actual=await readIndex(git.fd), parsed=parse(actual.bytes)
    if(expected.version!==1 || expected.device!==actual.device || expected.inode!==actual.inode || expected.size!==actual.size || expected.objectFormat!==parsed.kind || expected.sha256!==hash('sha256',actual.bytes).toString('hex'))throw Error('index changed')
    const info=await lstat(proc(objects.fd,'info')).catch((error:any)=>{if(error?.code==='ENOENT')return undefined;throw error})
    if(info) {
      if(!info.isDirectory() || info.isSymbolicLink())throw Error('unsupported objects')
      const directory=await open(proc(objects.fd,'info'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
      try {const alternates=await lstat(proc(directory.fd,'alternates')).catch((error:any)=>{if(error?.code==='ENOENT')return undefined;throw error});if(alternates)throw Error('unsupported alternates')} finally {await directory.close()}
    }
    if(!validPath(action.path) || ![0o100644,0o100755].includes(action.mode))throw Error('unsupported selected entry')
    const selected=parsed.entries.find(e=>e.name.equals(Buffer.from(action.path)))
    if(action.operation==='unstage' && !selected)throw Error('unsupported selected entry')
    if(!await recheck('before_object'))throw Error('authority changed')
    const after=await namespaces(root,home,git,objects)
    if(!sameDirectory(after.root,expected.root) || !sameDirectory(after.git,expected.git) || !sameDirectory(after.objects,expected.objects))throw Error('repository namespace changed')
    let object:Buffer|null
    if(action.operation==='stage') {
      if(!(action.bytes instanceof Uint8Array) || action.bytes.length>MAX_FILE || action.headOID!==undefined)throw Error('invalid source')
      const blob=Buffer.concat([Buffer.from(`blob ${action.bytes.length}\0`),Buffer.from(action.bytes)])
      const expectedOID=hash(parsed.kind,blob).toString('hex')
      objectState='unknown'
      const returned=(await runGit(home.fd,git.fd,git.fd,objects.fd,['hash-object','--stdin','--no-filters','-w'],Buffer.from(action.bytes),false)).toString('ascii').trim()
      if(returned!==expectedOID)throw Error('object unavailable')
      object=Buffer.from(returned,'hex');objectState='written_or_present'
    } else if(action.operation==='unstage') {
      if(action.bytes!==undefined || !(action.headOID===null || typeof action.headOID==='string'))throw Error('invalid source')
      const n=parsed.kind==='sha1'?40:64
      if(action.headOID!==null && !new RegExp(`^[0-9a-f]{${n}}$`).test(action.headOID))throw Error('invalid object')
      object=action.headOID===null ? null : Buffer.from(action.headOID,'hex')
    } else throw Error('invalid action')
    const dirname=`openclank-index-${candidateToken}`
    await mkdir(proc(git.fd,dirname),{mode:0o700})
    candidateRetained=true
    const directory=await open(proc(git.fd,dirname),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const ownedDirectory=directoryIdentity(await directory.stat())
    let next:Buffer, ownedCandidate:{device:number;inode:number}|undefined
    try {
      const temp=await open(proc(directory.fd,'index'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
      try {await temp.writeFile(actual.bytes);await temp.sync()} finally {await temp.close()}
      const oid=object?.toString('hex') ?? '0'.repeat(parsed.kind==='sha1'?40:64)
      const mode=object ? action.mode.toString(8) : '0'
      const input=Buffer.concat([Buffer.from(`${mode} ${oid}\t`),Buffer.from(action.path),Buffer.from([0])])
      await runGit(home.fd,git.fd,directory.fd,objects.fd,['update-index','-z','--index-info'],input,true)
      const candidateIndex=await readIndex(directory.fd),result=parse(candidateIndex.bytes)
      ownedCandidate={device:candidateIndex.device,inode:candidateIndex.inode}
      if(result.kind!==parsed.kind)throw Error('candidate changed format')
      const remaining=parsed.entries.filter(e=>!e.name.equals(Buffer.from(action.path)))
      const other=result.entries.filter(e=>!e.name.equals(Buffer.from(action.path)))
      if(remaining.length!==other.length || remaining.some((entry,i)=>!entry.raw.equals(other[i]!.raw)))throw Error('candidate changed other entries')
      const replacement=result.entries.find(e=>e.name.equals(Buffer.from(action.path)))
      if(object ? !replacement || replacement.mode!==action.mode || !replacement.oid.equals(object) : !!replacement)throw Error('candidate entry changed')
      next=candidateIndex.bytes
    } finally {
      // Only remove the exact positively observed post-Git candidate. An
      // uncertain child outcome or unknown entry remains for recovery.
      if(ownedCandidate) {const current=await lstat(proc(directory.fd,'index')).catch(()=>undefined)
        if(current?.isFile() && current.dev===ownedCandidate.device && current.ino===ownedCandidate.inode)await unlink(proc(directory.fd,'index')).catch(()=>{})}
      await directory.close().catch(()=>{})
      try {const current=directoryIdentity(await lstat(proc(git.fd,dirname)));if(sameDirectory(current,ownedDirectory)){candidateIdentity={token:candidateToken,...current};await rmdir(proc(git.fd,dirname));candidateRetained=false;candidateIdentity=undefined}} catch {}
    }
    await lock.writeFile(next);await lock.sync()
    if(!await recheck('before_publish'))throw Error('authority changed')
    const beforePublish=await namespaces(root,home,git,objects)
    if(!sameDirectory(beforePublish.root,expected.root) || !sameDirectory(beforePublish.git,expected.git) || !sameDirectory(beforePublish.objects,expected.objects))throw Error('repository namespace changed')
    const finalIndex=await readIndex(git.fd)
    if(finalIndex.device!==expected.device || finalIndex.inode!==expected.inode || finalIndex.size!==expected.size || hash('sha256',finalIndex.bytes).toString('hex')!==expected.sha256)throw Error('index changed')
    const current=await lstat(proc(git.fd,'index.lock'))
    if(!current.isFile() || current.dev!==lockIdentity.dev || current.ino!==lockIdentity.ino)throw Error('lock changed')
    renameIssued=true;await rename(proc(git.fd,'index.lock'),proc(git.fd,'index'))
    return {state:'applied',object:objectState,candidateRetained,...(candidateIdentity?{candidateIdentity}:{})}
  } catch {return {state:renameIssued?'unknown':'refused',object:objectState,candidateRetained,...(candidateIdentity?{candidateIdentity}:{})}}
  finally {
    if(lock)await lock.close().catch(()=>{})
    if(!renameIssued && lockIdentity) {const current=await lstat(proc(git.fd,'index.lock')).catch(()=>undefined);if(current && current.dev===lockIdentity.dev && current.ino===lockIdentity.ino)await unlink(proc(git.fd,'index.lock')).catch(()=>{})}
    if(objects)await objects.close().catch(()=>{})
    await git.close().catch(()=>{});await home.close().catch(()=>{})
  }
}

/** Generate and verify Git-owned index bytes before admitting a checkout. */
async function buildCheckoutCandidate(root:string, expected:Awaited<ReturnType<typeof previewPhysicalCheckout>>) {
  if (!isDeepStrictEqual(await previewPhysicalCheckout(root,expected.targetRef,expected.operation),expected)) throw Error('checkout preview changed')
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined,dir:Awaited<ReturnType<typeof open>>|undefined
  const temp=await mkdtemp(join(tmpdir(),'openclank-checkout-index-'));let indexIdentity:DirectoryIdentity|undefined
  try {
    dir=await open(temp,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),{root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}))throw Error('namespace changed')
    await runGit(home.fd,git.fd,dir.fd,objects.fd,['read-tree',expected.targetOID],Buffer.alloc(0),true,expected.source.executable.path,4096)
    const candidate=await readIndex(dir.fd);indexIdentity={device:candidate.device,inode:candidate.inode}
    const parsed=parse(candidate.bytes)
    if(parsed.kind!==expected.source.physicalIndex.objectFormat || !isDeepStrictEqual(parsed.entries.map(e=>({path:e.name.toString('utf8'),mode:e.mode,oid:e.oid.toString('hex')})),expected.targetEntries))throw Error('candidate index mismatch')
    if(!isDeepStrictEqual(await previewPhysicalCheckout(root,expected.targetRef,expected.operation),expected))throw Error('checkout preview changed')
    return {sha256:createHash('sha256').update(candidate.bytes).digest('hex'),size:candidate.bytes.length,bytes:candidate.bytes}
  } finally {
    if(dir && indexIdentity){const current=await lstat(proc(dir.fd,'index')).catch(()=>undefined);if(current?.isFile() && current.dev===indexIdentity.device && current.ino===indexIdentity.inode)await unlink(proc(dir.fd,'index'))}
    if(dir)await dir.close();if(objects)await objects.close();await git.close();await home.close();await rmdir(temp)
  }
}

/** Expose only the hash and size; publication rebuilds and compares bytes. */
export async function previewCheckoutCandidate(root:string, expected:Awaited<ReturnType<typeof previewPhysicalCheckout>>) {
  const value=await buildCheckoutCandidate(root,expected)
  return {sha256:value.sha256,size:value.size}
}


/** Run one reviewed checkout under Git index/ref locks. Unknown
 * callback, rename or ref outcomes remain status-only; no new operation. */
export async function applyPhysicalCheckout(root:string,expected:Awaited<ReturnType<typeof previewPhysicalCheckout>>,candidateSHA:string,count:number,phase:(event:any)=>Promise<{state:string}>) {
  let state:'refused'|'applied'|'rolled_back'|'unknown'='refused',index:'not_issued'|'applied'|'restored'|'unknown'='not_issued',ref:'not_issued'|'applied'|'unknown'='not_issued',steps=0,callbackKnown=true,prepared=false,filesPrepared=false
  const result=()=>({state,index,ref,steps})
  let home:Awaited<ReturnType<typeof open>>|undefined,git:Awaited<ReturnType<typeof open>>|undefined,objects:Awaited<ReturnType<typeof open>>|undefined,lock:Awaited<ReturnType<typeof open>>|undefined,lockID:DirectoryIdentity|undefined,guard:Awaited<ReturnType<typeof open>>|undefined,guardID:DirectoryIdentity|undefined,child:ReturnType<typeof spawn>|undefined
  let closed:Promise<number|null>|undefined,lines:AsyncIterator<string>|undefined,timer:ReturnType<typeof setTimeout>|undefined,timedOut=false,validateSource:(()=>Promise<boolean>)|undefined,oldBytes:Buffer|undefined,publishedID:DirectoryIdentity|undefined,knownNoRef=false
  try {
    if(!/^[0-9a-f]{64}$/.test(candidateSHA)||!Number.isSafeInteger(count)||count<1||count>64||typeof phase!=='function')return result()
    const candidate=await buildCheckoutCandidate(root,expected);if(candidate.sha256!==candidateSHA)throw Error('candidate changed')
    ;({home,git}=await rooted(root));objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    lock=await open(proc(git.fd,'index.lock'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
    const original=await readIndex(git.fd);if(original.device!==expected.source.physicalIndex.device || original.inode!==expected.source.physicalIndex.inode || createHash('sha256').update(original.bytes).digest('hex')!==expected.source.physicalIndex.sha256)throw Error('source index changed');oldBytes=original.bytes
    const st=await lock.stat();lockID={device:st.dev,inode:st.ino};await lock.writeFile(candidate.bytes);await lock.sync()
    const anchored=async()=>{const held=await lock!.stat(),current=await lstat(proc(git!.fd,'index.lock'));if(!lockID || !held.isFile()||!current.isFile()||held.dev!==lockID.device||held.ino!==lockID.inode||current.dev!==lockID.device||current.ino!==lockID.inode)throw Error('index lock changed');if(!isDeepStrictEqual(await namespaces(root,home!,git!,objects!),{root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}))throw Error('namespace changed')}
    const same=async()=>isDeepStrictEqual(await previewPhysicalCheckout(root,expected.targetRef,expected.operation),expected)
    validateSource=async()=>{await anchored();return await same()}
    await anchored();if(!await same())throw Error('preview changed')
    const env:Record<string,string>={PATH:process.env.PATH??'/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
    child=spawn(expected.source.executable.path,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','update-ref','--stdin','-z'],{cwd:proc(home.fd),env,stdio:['pipe','pipe','pipe',git.fd,objects.fd,git.fd,home.fd]})
    const refChild=child
    closed=new Promise<number|null>(resolve=>refChild.once('close',resolve));refChild.on('error',()=>{})
    const stdin=refChild.stdin,stdout=refChild.stdout,stderr=refChild.stderr
    if(!stdin || !stdout || !stderr)throw Error('ref pipes unavailable')
    stdin.on('error',()=>{});stderr.resume();lines=createInterface({input:stdout,crlfDelay:Infinity})[Symbol.asyncIterator]()
    timer=setTimeout(()=>{timedOut=true;child?.kill('SIGKILL')},30000)
    const command=expected.operation==='switch'?`option no-deref\0symref-update HEAD\0${expected.targetRef}\0ref\0${expected.source.headRef}\0verify ${expected.targetRef}\0${expected.targetOID}\0`:`option no-deref\0update ${expected.source.headRef}\0${expected.targetOID}\0${expected.source.headOID}\0verify ${expected.targetRef}\0${expected.targetOID}\0`
    stdin.write(Buffer.from(`start\0${command}prepare\0`));if((await lines.next()).value!=='start: ok'||(await lines.next()).value!=='prepare: ok')throw Error('ref prepare failed');prepared=true
    const send=async(event:any,expectedState:string)=>{callbackKnown=false;const reply=await phase(event);if(reply?.state!==expectedState)throw Error('phase unknown');callbackKnown=true}
    await anchored();if(!await same())throw Error('preview changed');await send({kind:'prepare'},'ready');filesPrepared=true
    for(let i=0;i<count;i++){if(i===count-1){await anchored();if(!await same())throw Error('preview changed')}await send({kind:'step',ordinal:i},'applied');steps++}
    // No intervening await after final Host approval before issuing rename.
    index='unknown';await rename(proc(git.fd,'index.lock'),proc(git.fd,'index'));index='applied';publishedID=lockID;lockID=undefined
    guard=await open(proc(git.fd,'index.lock'),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600)
    const guardStat=await guard.stat();guardID={device:guardStat.dev,inode:guardStat.ino}
    const installed=await readIndex(git.fd)
    if(!publishedID || installed.device!==publishedID.device || installed.inode!==publishedID.inode || createHash('sha256').update(installed.bytes).digest('hex')!==candidateSHA)throw Error('candidate changed')
    if(!isDeepStrictEqual(await namespaces(root,home,git,objects),{root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}))throw Error('namespace changed')
    await send({kind:'before_ref',indexSHA256:candidateSHA},'accepted')
    if(child.exitCode!==null || child.signalCode!==null){knownNoRef=true;prepared=false;throw Error('ref process exited before commit')}
    ref='unknown';stdin.end(Buffer.from('commit\0'));if((await lines.next()).value!=='commit: ok'||await closed!==0)throw Error('ref outcome unknown');ref='applied';prepared=false
    if(!guard || !guardID)throw Error('guard unavailable')
    const retiringGuard=guard,retiringID=guardID;guard=undefined;guardID=undefined
    await retiringGuard.close()
    const finalGuard=await lstat(proc(git.fd,'index.lock'))
    if(finalGuard.dev!==retiringID.device || finalGuard.ino!==retiringID.inode)throw Error('guard changed')
    await unlink(proc(git.fd,'index.lock'))
    await send({kind:'finalize',headOID:expected.targetOID,headRef:expected.operation==='switch'?expected.targetRef:expected.source.headRef,indexSHA256:candidateSHA},'complete');state='applied'
  } catch {
    if(knownNoRef && index==='applied' && ref==='not_issued' && git && home && objects && oldBytes && publishedID) {
      let restore:Awaited<ReturnType<typeof open>>|undefined,restoreID:DirectoryIdentity|undefined
      try {
        if(!guard || !guardID)throw Error('restore guard unavailable')
        restore=guard;restoreID=guardID;guard=undefined;guardID=undefined
        if(!isDeepStrictEqual(await namespaces(root,home,git,objects),{root:expected.source.physicalIndex.root,git:expected.source.physicalIndex.git,objects:expected.source.physicalIndex.objects}))throw Error('namespace changed')
        const installed=await readIndex(git.fd)
        if(installed.device!==publishedID.device || installed.inode!==publishedID.inode || createHash('sha256').update(installed.bytes).digest('hex')!==candidateSHA)throw Error('candidate changed')
        await restore.writeFile(oldBytes);await restore.sync()
        const again=await readIndex(git.fd)
        if(again.device!==installed.device || again.inode!==installed.inode || !again.bytes.equals(installed.bytes))throw Error('candidate changed')
        const held=await restore.stat(),lockPath=await lstat(proc(git.fd,'index.lock'))
        if(!restoreID || held.dev!==restoreID.device || held.ino!==restoreID.inode || lockPath.dev!==restoreID.device || lockPath.ino!==restoreID.inode)throw Error('restore lock changed')
        index='unknown';await rename(proc(git.fd,'index.lock'),proc(git.fd,'index'));index='restored';restoreID=undefined
        const current=await readIndex(git.fd);if(createHash('sha256').update(current.bytes).digest('hex')!==expected.source.physicalIndex.sha256)throw Error('restore changed')
        const call=(args:string[])=>runGit(home!.fd,git!.fd,git!.fd,objects!.fd,args,Buffer.alloc(0),false,expected.source.executable.path)
        if((await call(['symbolic-ref','HEAD'])).toString('utf8')!==expected.source.headRef+'\n' || (await call(['rev-parse','--verify','HEAD'])).toString('utf8')!==expected.source.headOID+'\n')throw Error('HEAD changed')
        validateSource=async()=>{const indexValue=await readIndex(git!.fd);return createHash('sha256').update(indexValue.bytes).digest('hex')===expected.source.physicalIndex.sha256 && (await call(['symbolic-ref','HEAD'])).toString('utf8')===expected.source.headRef+'\n' && (await call(['rev-parse','--verify','HEAD'])).toString('utf8')===expected.source.headOID+'\n'}
      } catch {index='unknown'} finally {if(restore)await restore.close().catch(()=>{index='unknown'});if(restoreID)try{const st=await lstat(proc(git.fd,'index.lock'));if(st.dev!==restoreID.device||st.ino!==restoreID.inode)throw Error('restore lock changed');await unlink(proc(git.fd,'index.lock'))}catch{index='unknown'}}
    }
    if(index==='unknown'||ref==='unknown'||!callbackKnown)state='unknown'
    else if((index==='not_issued'||index==='restored')&&ref==='not_issued'&&filesPrepared){try {if(!validateSource || !await validateSource())throw Error('source changed');const source=expected.source;const ack=await phase({kind:'failure',index:index==='restored'?'restored':'unchanged',ref:'unchanged',headOID:source.headOID,headRef:source.headRef,indexSHA256:source.physicalIndex.sha256});if(ack.state!=='accepted')throw Error('failure unknown');for(let i=steps-1;i>=0;i--){const out=await phase({kind:'rollback',ordinal:i});if(out.state!=='restored')throw Error('rollback unknown')}const out=await phase({kind:'finalize',headOID:source.headOID,headRef:source.headRef,indexSHA256:source.physicalIndex.sha256});if(out.state!=='complete')throw Error('restore unknown');state='rolled_back'}catch{state='unknown'}}
    else if(index==='applied'||ref==='applied'||filesPrepared)state='unknown'
  } finally {
    if(child && prepared && ref==='not_issued') {try {if(child.exitCode!==null || !lines || !closed || !child.stdin)throw Error('abort unavailable');child.stdin.end(Buffer.from('abort\0'));if((await lines.next()).value!=='abort: ok' || await closed!==0)throw Error('abort unavailable');prepared=false}catch{state='unknown'}}
    if(child && child.exitCode===null && child.signalCode===null){child.kill('SIGKILL');if(closed)await closed}
    if(timedOut || prepared)state='unknown';if(timer)clearTimeout(timer)
    if(guard)await guard.close().catch(()=>{state='unknown'});if(guardID&&git)try{const current=await lstat(proc(git.fd,'index.lock'));if(current.dev!==guardID.device||current.ino!==guardID.inode)throw Error('guard changed');await unlink(proc(git.fd,'index.lock'))}catch{state='unknown'}
    if(lock)await lock.close().catch(()=>{state='unknown'});if(lockID&&git)try{const current=await lstat(proc(git.fd,'index.lock'));if(current.dev!==lockID.device||current.ino!==lockID.inode)throw Error('lock changed');await unlink(proc(git.fd,'index.lock'))}catch{state='unknown'}
    if(objects)await objects.close().catch(()=>{state='unknown'});if(git)await git.close().catch(()=>{state='unknown'});if(home)await home.close().catch(()=>{state='unknown'})
  }
  return result()
}
