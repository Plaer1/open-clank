/** Private Linux index primitive. Callers must independently hold and recheck
 * original Files/account/worker/Once/HEAD authority; no HTTP entry point. */
import { constants } from 'node:fs'
import { open, realpath, lstat, mkdir, unlink, rename, rmdir, access } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import { spawn } from 'node:child_process'

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
async function runGit(rootfd:number,gitfd:number,dirfd:number,objectsfd:number,args:string[],input:Buffer, index:boolean, executable="git", maxOutput=4096, identity?:{author:CommitIdentity;committer:CommitIdentity}) {
  const environment:Record<string,string>={PATH:process.env.PATH ?? '/usr/bin:/bin',GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_ATTR_NOSYSTEM:'1',GIT_TERMINAL_PROMPT:'0',GIT_OPTIONAL_LOCKS:'0',GIT_LITERAL_PATHSPECS:'1',GIT_NO_LAZY_FETCH:'1',GIT_NO_REPLACE_OBJECTS:'1',GIT_PAGER:'cat',PAGER:'cat',LC_ALL:'C',GIT_DIR:'/proc/self/fd/5',GIT_WORK_TREE:'/proc/self/fd/6',GIT_OBJECT_DIRECTORY:'/proc/self/fd/4',GIT_ALTERNATE_OBJECT_DIRECTORIES:''}
  if(index)environment.GIT_INDEX_FILE='/proc/self/fd/3/index'
  if(identity)for(const [prefix,value] of [['GIT_AUTHOR',identity.author],['GIT_COMMITTER',identity.committer]] as const) {environment[prefix+'_NAME']=value.name;environment[prefix+'_EMAIL']=value.email;environment[prefix+'_DATE']=`@${value.seconds} ${value.timezone}`} 
  const child=spawn(executable,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.hooksPath=/dev/null','-c','credential.helper=',...args],{cwd:proc(rootfd),env:environment,stdio:['pipe','pipe','pipe',dirfd,objectsfd,gitfd,rootfd]})
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
  const timer=setTimeout(()=>{timedOut=true;child.kill('SIGKILL')},3000)
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
export async function previewPhysicalCommit(root:string):Promise<PhysicalCommitPreview> {
  const {home,git}=await rooted(root);let objects:Awaited<ReturnType<typeof open>>|undefined
  try {
    objects=await open(proc(git.fd,'objects'),constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW)
    const namespace=await namespaces(root,home,git,objects), configPath=proc(git.fd,'config'),config=await fileWitness(configPath,128*1024), executable=await gitExecutable()
    const guards=['config.worktree','shallow','objects/info/alternates','info/grafts','MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-apply','rebase-merge','sequencer']
    for(const name of guards)await absent(proc(git.fd,name))
    const call=(args:string[],limit=4096)=>runGit(home.fd,git.fd,git.fd,objects!.fd,args,Buffer.alloc(0),false,executable.path,limit)
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
