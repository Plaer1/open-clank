import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {deflateSync} from 'node:zlib';
import {fileURLToPath} from 'node:url';

const id = label=>'file:'+crypto.createHash('sha256').update(label).digest('hex');
// GOLDEN below is mocked renderer input ONLY, not native evidence or native IDs.
export const GOLDEN = Object.freeze({groups:['src','src/api','src/storage'],edges:[
  {from:id('src/api/service.py'),to:id('src/storage/store.py')},
  {from:id('src/main.py'),to:id('src/api/service.py')}],
  exact:['src/main.py → src/api/service.py','src/api/service.py → src/storage/store.py'],
  inferred:['src/plugin.py → src/api/service.py'],unresolved:['src/unresolved.py → missing_optional'],disconnected:['src/standalone.py']});

export function queryFixture(operation='overview', semantic=false) {
  const names=['src/main.py','src/api/service.py','src/storage/store.py','src/plugin.py','src/standalone.py'];
  return {schema:'open-clank.code-query/v1',domain:'code',operation,
    receipt:{workspace_id:'s00',registration_id:'s00-reg',source_snapshot_id:'s00-source',checkout_id:'s00-checkout',head_generation:'s00-head',admission_epoch:0,policy_generation:1,config_fingerprint:'s00-fixture'},
    hits:names.map(label=>({id:id(label),kind:'file',label,evidence_state:'metadata'})),
    edges:semantic?GOLDEN.edges.map((e,i)=>({...e,occurrence_id:'occ:'+String(i+1).padStart(64,'0'),resolution_state:'exact',witnesses:[]})):[],
    paths:[],facets:{kinds:[],languages:[],states:[],folders:[]},counts:[{name:'matched',value:160,unit:'resource',exactness:'exact'}],
    channels:[],capabilities:[],operation_capabilities:['overview','catalog','facets','search'].map(operation=>({operation,available:true})),
    omissions:[],reasons:[],partial:!semantic,degraded:false,next_cursor:null,error:null,cost:{}};
}

function crc32(bytes) {let crc=0xffffffff;for(const byte of bytes){crc^=byte;for(let n=0;n<8;n++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return(crc^0xffffffff)>>>0;}
const chunk=(type,body)=>{const t=Buffer.from(type),length=Buffer.alloc(4),crc=Buffer.alloc(4);length.writeUInt32BE(body.length);crc.writeUInt32BE(crc32(Buffer.concat([t,body])));return Buffer.concat([length,t,body,crc]);};
export function syntheticPng(width,height,seed=1) {
  assert(Number.isInteger(width)&&width>0&&width<=2048&&Number.isInteger(height)&&height>0&&height<=2048);
  const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(width);ihdr.writeUInt32BE(height,4);ihdr[8]=8;ihdr[9]=2;
  const scan=Buffer.alloc((width*3+1)*height);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){const n=y*(width*3+1)+1+x*3;scan[n]=(x+seed*17)%256;scan[n+1]=(y+seed*29)%256;scan[n+2]=((x>>4)^(y>>4)^seed)*19%256;}
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),chunk('IDAT',deflateSync(scan)),chunk('IEND',Buffer.alloc(0))]);
}

// No authorization is installed here. Root must authorize this fresh synthetic
// directory through normal policy/Files APIs in an isolated app fixture.
export function materializeFixture(parent='/private/tmp') {
  const canonical=fs.realpathSync(parent);assert(['/private/tmp','/tmp'].includes(canonical));
  const root=fs.mkdtempSync(path.join(canonical,'openclank-s00-media-'));fs.chmodSync(root,0o700);
  const files=[];
  const write=(relative,body,extra={})=>{const target=path.join(root,relative);fs.mkdirSync(path.dirname(target),{recursive:true,mode:0o700});fs.writeFileSync(target,body,{flag:'wx',mode:0o600});const bytes=Buffer.from(body);files.push({path:relative,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex'),...extra});};
  const thumb=syntheticPng(96,96,1);
  for(const [directory,count]of [['files-480',480],['files-640',640]])for(let n=0;n<count;n++)write(`${directory}/still-${String(n).padStart(4,'0')}.png`,thumb,{width:96,height:96,decodedBytes:96*96*4});
  write('editors/still.png',syntheticPng(256,192,2),{width:256,height:192,decodedBytes:256*192*4});
  write('editors/large-preview.png',syntheticPng(2048,2048,3),{width:2048,height:2048,decodedBytes:2048*2048*4});
  write('editors/image.md','# S00 synthetic image\n\n![S00 generated still](./still.png)\n\nEditable text after image.\n');
  write('editors/image.py','# S00 synthetic source image\n# ![S00 generated still](./still.png)\nfrom __future__ import annotations\nlabel = "synthetic only"\n');
  write('editors/text-only.py','# S00 text-only fixture\n# Unicode: café λ 🙂; direction controls are literal escapes below.\nlabel = "text \\u202e only"\n');
  const modules={'src/main.py':'from api.service import run\nrun()\n','src/api/service.py':'from storage.store import load\ndef run(): return load()\n','src/storage/store.py':'def load(): return 1\n','src/plugin.py':'import importlib\nplugin = importlib.import_module("api.service")\n','src/unresolved.py':'import missing_optional\n','src/standalone.py':'answer = 42\n','src/broken.py':'def intentional_parse_failure(\n'};
  for(const [name,body]of Object.entries(modules))write('corpus/'+name,body);
  for(let n=Object.keys(modules).length;n<160;n++)write(`corpus/src/partition/module_${n}.py`,`# synthetic partition ${n}\nVALUE = ${n}\n`);
  write('corpus/.gitignore','ignored/\n');write('corpus/ignored/private-sentinel.txt','S00 synthetic excluded sentinel\n');
  const manifest={schema:'openclank.s00-synthetic-fixture/v1',synthetic:true,root,
    files,filesScenarios:[{directory:'files-480',loadedEntries:480},{directory:'files-640',loadedEntries:640}],
    editors:{shared:'editors/image.md',legacyCode:'editors/image.py',textOnly:'editors/text-only.py',largePreview:'editors/large-preview.png'},
    corpus:{directory:'corpus',modules:160,ignored:'ignored/private-sentinel.txt',intentionalParseFailure:'src/broken.py',golden:GOLDEN},
    limits:{largestImagePixels:2048*2048,largestImageDecodedBytes:2048*2048*4,classification:'fixture ceilings, not measured GPU or adapter acceptance limits'}};
  fs.writeFileSync(path.join(root,'fixture-manifest.json'),JSON.stringify(manifest,null,2)+'\n',{flag:'wx',mode:0o600});return manifest;
}

export const NATIVE_GOLDEN_SHA256='c9c9277fcc345abb6e34615cea100156a041d48474bf7a601a0bc57b38aa9485';
const nativeGoldenPath=fileURLToPath(new URL('../../mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/s00_code_graph.json',import.meta.url));
export function readNativeGolden(){
  const bytes=fs.readFileSync(nativeGoldenPath);
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),NATIVE_GOLDEN_SHA256,'Native golden changed: review before fixture admission');
  const golden=JSON.parse(bytes);assert.equal(golden.partition_fillers,129);
  return {golden,source:{path:'mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/s00_code_graph.json',sha256:NATIVE_GOLDEN_SHA256,bytes:bytes.length}};
}

// Five fresh workspace roots, derived ONLY from the pinned native golden and
// deterministic unique declarations. The earlier corpus/media stay untouched.
// This creates no stores or index. Identical content/process/OS caches may be
// warm; classification is fresh workspace/index scope, never flushed caches.
export function deriveColdCorpora(fixture) {
  assert.equal(fixture.synthetic,true);assert.equal(fixture.schema,'openclank.s00-synthetic-fixture/v1');
  const root=fs.realpathSync(fixture.root);assert.equal(root,fixture.root);assert.match(root,/^\/private\/tmp\/openclank-s00-media-[A-Za-z0-9]+$/);
  const rootStat=fs.lstatSync(root);assert(rootStat.isDirectory()&&rootStat.uid===process.getuid()&&(rootStat.mode&0o077)===0);
  const manifestPath=path.join(root,'fixture-manifest.json');assert.equal(fs.realpathSync(manifestPath),manifestPath);const manifestStat=fs.lstatSync(manifestPath);assert(manifestStat.isFile()&&manifestStat.uid===process.getuid()&&(manifestStat.mode&0o777)===0o600);const original=fs.readFileSync(manifestPath);
  assert.deepEqual(JSON.parse(original),fixture);
  const rows=fixture.files.filter(row=>row.path.startsWith('corpus/'));
  assert(rows.length>0&&rows.filter(r=>r.path.endsWith('.py')).length===fixture.corpus.modules);
  const pinned=rows.map(row=>{const relative=row.path.slice('corpus/'.length);assert(relative&&!relative.split('/').some(s=>!s||s==='.'||s==='..'));const source=path.join(root,row.path);assert.equal(fs.realpathSync(source),source);const stat=fs.lstatSync(source);assert(stat.isFile()&&stat.uid===process.getuid()&&(stat.mode&0o777)===0o600);const bytes=fs.readFileSync(source);assert.equal(bytes.length,row.bytes);assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),row.sha256);return{relative,bytes,sha256:row.sha256};});
  const base=path.join(root,'cold-corpora');fs.mkdirSync(base,{mode:0o700}); // exclusive; retain interrupted attempts
  const {golden,source:goldenSource}=readNativeGolden();
  const bodies=Object.entries(golden.files).map(([relative,text])=>({relative,bytes:Buffer.from(text)}));
  for(let n=0;n<golden.partition_fillers;n++)bodies.push({relative:`partition/f${String(n).padStart(3,'0')}.py`,bytes:Buffer.from(`def filler_${n}():\n    return ${n}\n`)});
  for(const row of bodies){assert(row.relative&&!row.relative.split('/').some(s=>!s||s==='.'||s==='..'));row.sha256=crypto.createHash('sha256').update(row.bytes).digest('hex');}
  const trials=[];
  for(let number=1;number<=5;number++){
    const directory=path.join(base,'trial-'+String(number).padStart(2,'0'));fs.mkdirSync(directory,{mode:0o700});const files=[];
    for(const row of bodies){const target=path.join(directory,row.relative);fs.mkdirSync(path.dirname(target),{recursive:true,mode:0o700});fs.writeFileSync(target,row.bytes,{flag:'wx',mode:0o600});assert.equal(crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex'),row.sha256);files.push({path:row.relative,bytes:row.bytes.length,sha256:row.sha256,mode:0o600});}
    trials.push({number,directory,files});
  }
  assert.deepEqual(fs.readFileSync(manifestPath),original);
  for(const row of pinned)assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'corpus',row.relative))).digest('hex'),row.sha256);
  const result={schema:'openclank.s00-derived-cold-corpora/v2',synthetic:true,root,sourceManifestSha256:crypto.createHash('sha256').update(original).digest('hex'),goldenSource,
    realGolden:{groups:['src','partition'],relationships:golden.relationships,unresolvedSymbol:golden.unresolved_symbol,disconnected:{file:'island.py',name:golden.disconnected_declaration}},trials,
    classification:'fresh workspace/index scopes; canonical native golden plus 129 declaration fillers; identical content and process caches may be warm; OS cache not flushed'};
  fs.writeFileSync(path.join(base,'manifest.json'),JSON.stringify(result,null,2)+'\n',{flag:'wx',mode:0o600});return result;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  assert.equal(process.argv[2],'--materialize','Use --materialize; creates only a new synthetic /tmp directory');
  const manifest=materializeFixture();console.log(JSON.stringify({root:manifest.root,files:manifest.files.length,manifest:path.join(manifest.root,'fixture-manifest.json')}));
}
