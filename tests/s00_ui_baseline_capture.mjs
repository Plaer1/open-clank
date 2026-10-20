// Attach-only measurement of a root-approved isolated normal-auth page. Never
// starts an app/browser, logs in, grants policy, or requests a code index.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {browserGraphObservation,browserMediaObservation,distribution,usefulView,validateCaptureConfig,installBrowserRecorder,stageDurations} from './helpers/s00_ui_baseline_contract.mjs';

const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const args=process.argv.slice(2);
assert.equal(args.length,6,'--config PATH --approval PATH --approval-sha SHA256');
assert.equal(args[0],'--config');assert.equal(args[2],'--approval');assert.equal(args[4],'--approval-sha');
const privateFile=file=>{const s=fs.lstatSync(file);assert(s.isFile()&&!s.isSymbolicLink()&&s.uid===process.getuid()&&(s.mode&0o777)===0o600,'Private owned regular file required');return fs.readFileSync(file);};
const configBytes=privateFile(args[1]),approvalBytes=privateFile(args[3]);assert.equal(sha(approvalBytes),args[5]);
const config=JSON.parse(configBytes),approval=JSON.parse(approvalBytes);validateCaptureConfig(config,approval,sha(configBytes));
const root=fs.realpathSync(config.root);
assert.equal(root,config.root);assert.match(root,/^\/private\/tmp\/openclank-(?:graph-usability-s00|graph-qol-s06)\.[A-Za-z0-9]+$/);
const rootStat=fs.lstatSync(root);assert(rootStat.isDirectory()&&rootStat.uid===process.getuid()&&(rootStat.mode&0o077)===0);
const within=file=>{assert(path.isAbsolute(file));const rel=path.relative(root,file);assert(rel&&!rel.startsWith('..')&&!path.isAbsolute(rel));return file;};
for(const file of [args[1],args[3]])assert.equal(fs.realpathSync(file),within(file));
assert(['baseline-recording','acceptance'].includes(config.purpose));
assert.equal(config.purpose,approval.purpose);
assert.match(config.runName,/^ui-baseline-[a-z0-9-]{1,64}$/);
const manifestPath=within(config.sourceManifestPath);assert.equal(fs.realpathSync(manifestPath),manifestPath);
assert.equal(sha(fs.readFileSync(manifestPath)),config.sourceManifestSha256);
assert.equal(typeof config.server.commandSha256,'string');assert.equal(typeof config.browser.commandSha256,'string');
const shell=(program,argv)=>execFileSync(program,argv,{encoding:'utf8',maxBuffer:1024*1024}).trim();
const processIdentity=(item,isBrowser=false)=>{
  assert.equal(Number(shell('/bin/ps',['-p',String(item.pid),'-o','uid='])),process.getuid());
  assert.equal(shell('/bin/ps',['-p',String(item.pid),'-o','lstart=']),item.birth);
  const command=shell('/bin/ps',['-ww','-p',String(item.pid),'-o','command=']);assert.equal(sha(command),item.commandSha256);
  const cwd=shell('/usr/sbin/lsof',['-a','-p',String(item.pid),'-d','cwd','-Fn']).split('\n').find(l=>l.startsWith('n'))?.slice(1);
  assert.equal(cwd,item.cwd);assert.equal(fs.realpathSync(cwd),within(cwd));
  if(isBrowser){const profile=within(config.browser.profile);assert.equal(fs.realpathSync(profile),profile);assert(command.includes(`--user-data-dir=${profile}`));}
  const port=new URL(isBrowser?config.webSocketDebuggerUrl:config.origin).port;
  const listeners=shell('/usr/sbin/lsof',['-nP','-a','-p',String(item.pid),`-iTCP:${port}`,'-sTCP:LISTEN','-Fp']).split('\n').filter(l=>l.startsWith('p'));
  assert.deepEqual([...new Set(listeners)],[`p${item.pid}`]);
  return {pid:item.pid,birth:item.birth,commandSha256:item.commandSha256,listenerPort:Number(port),owned:true,
    ...(isBrowser?{headless:/--headless(?:=|\s|$)/.test(command),gpuDisabled:/--disable-gpu(?:\s|$)/.test(command)}:{})};
};
const serverBefore=processIdentity(config.server),browserBefore=processIdentity(config.browser,true);
const output=within(path.join(root,config.runName));fs.mkdirSync(output,{mode:0o700}); // exclusive, never overwrites an old run
let ws,seq=0,cleanupRecorder=async()=>{},screenshotGuard=async()=>false;const pending=new Map();
const result={schema:'openclank.s00-ui-baseline/v1',status:'failed',purpose:config.purpose,mode:config.mode,
  configSha256:sha(configBytes),approvalSha256:sha(approvalBytes),sourceManifestSha256:config.sourceManifestSha256,
  cacheClass:config.cacheClass,osCache:config.osCache,serverBefore,browserBefore,measurements:{},failures:[],
  browserOwnedByRoot:true,browserClosed:false,backendSpeedQualified:false,gpuHardwareQualified:false};
result.hardware={platform:os.platform(),arch:os.arch(),cpuModel:os.cpus()[0]?.model,cpuLogicalCount:os.cpus().length,
  totalMemoryBytes:os.totalmem(),freeMemoryBytes:os.freemem(),loadAverage:os.loadavg(),gpu:'unverified'};
const write=(name,bytes)=>fs.writeFileSync(path.join(output,name),bytes,{flag:'wx',mode:0o600});
try {
  ws=new WebSocket(config.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Owned CDP attach observation expired')),5000);ws.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});ws.addEventListener('error',()=>{clearTimeout(timer);reject(Error('Owned CDP attach failed'));},{once:true});});
  ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id&&pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);}});
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error(`CDP stage expired: ${method}`));},15000);pending.set(id,m=>{clearTimeout(timer);m.error?reject(Error(`CDP rejected ${method}`)):resolve(m.result);});ws.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});assert(!r.exceptionDetails,'Measurement expression failed; private details omitted');return r.result?.value;};
  assert(await evaluate(`location.origin===${JSON.stringify(config.origin)}`),'Approved origin unavailable');
  const auth=await evaluate(`fetch('/api/auth/status',{redirect:'error'}).then(async r=>{if(!r.ok)throw Error('auth');const a=await r.json();return{configured:a.configured,authenticated:a.authenticated,username:a.username};})`);
  assert(auth.configured===true&&auth.authenticated===true&&auth.username===config.syntheticUsername,'Approved synthetic principal unavailable');
  screenshotGuard=()=>evaluate(`(async()=>{if(location.origin!==${JSON.stringify(config.origin)})return false;try{const r=await fetch('/api/auth/status',{redirect:'error'});if(!r.ok)return false;const a=await r.json();return a.configured===true&&a.authenticated===true&&a.username===${JSON.stringify(config.syntheticUsername)};}catch{return false;}})()`);
  result.normalSyntheticAuthConfirmed=true; // no username/auth body emitted
  await cdp('Performance.enable');const before=await cdp('Performance.getMetrics');
  // Inline the pure judge's default limits because serialized functions have no
  // module closure in the page. No product hook or fake data is installed.
  await evaluate(`(${installBrowserRecorder})(${browserGraphObservation},${browserMediaObservation},(o,e)=>(${usefulView})(o,e,${JSON.stringify({graphHeightFraction:0.6,minimumLabelPx:11,maximumOverlapFraction:0.1})}),${JSON.stringify(config.expected)},${JSON.stringify(config.scenario)})`);
  cleanupRecorder=()=>evaluate('window.__s00Baseline?.stop();delete window.__s00Baseline');
  console.log(JSON.stringify({status:'armed',scenario:config.scenario,output,observationMs:config.observationMs}));
  const deadline=Date.now()+config.observationMs;
  while(Date.now()<deadline){if(await evaluate('window.__s00Baseline.snapshot().stopped'))break;await new Promise(r=>setTimeout(r,250));}
  const recorded=await evaluate('window.__s00Baseline.stop()');
  result.recorded=recorded;result.measurements.stages=stageDurations(recorded.times);
  result.measurements.activeFrameIntervalsMs=distribution(recorded.activeFrameIntervalsMs);
  result.measurements.inputToMutationAtFrameMs=distribution(recorded.inputToMutationAtFrameMs);
  result.measurements.observationExpired=recorded.times.stopped-recorded.times.armed>=config.observationMs;
  result.measurements.firstUsefulDefinition='Trusted normal #workspace-use confirmation → scope commit/progress/drawable/structural/published/useful separately. Choose code folder click is pickerOpened, so human browsing dwell is separate. Arm timestamp is never substituted for confirmed choice. Observation expiry does not cancel native work.';
  const observation=await evaluate(`(${browserGraphObservation})()`);
  result.observation=observation;result.usefulness=usefulView(observation,config.expected);
  if(config.scenario.startsWith('graph-')){
    result.failures.push(...result.usefulness.failures);
    if(config.scenario==='graph-choice'&&recorded.times.choice===undefined)result.failures.push('No trusted normal folder-choice action observed');
  } else {
    // Media stages are measurement-only until the normal navigation/functional
    // journey has verified the loaded synthetic fixture identity separately.
    assert.equal(config.purpose,'baseline-recording','Media snapshots are not independent functional acceptance');
    result.mediaFunctionalAcceptance=false;
  }
  result.media=await evaluate(`(${browserMediaObservation})()`);
  const frames=await evaluate(`new Promise(resolve=>{const a=[];let last=performance.now();const step=t=>{a.push(t-last);last=t;if(a.length===120)resolve(a);else requestAnimationFrame(step);};requestAnimationFrame(step);})`);
  result.measurements.frameIntervalMs=distribution(frames);
  result.measurements.frameDefinition='120 idle/requestAnimationFrame intervals, NOT active rendering FPS or GPU time';
  const transfer=await evaluate(`(()=>{const a=performance.getEntriesByType('resource');return{entries:a.length,transferBytes:a.reduce((s,e)=>s+e.transferSize,0),encodedBytes:a.reduce((s,e)=>s+e.encodedBodySize,0),decodedBytes:a.reduce((s,e)=>s+e.decodedBodySize,0),durationMs:a.reduce((s,e)=>s+e.duration,0),zeroTransferEntries:a.filter(e=>e.transferSize===0).length};})()`);
  result.measurements.transfer={...transfer,definition:'ResourceTiming totals since current page navigation; zero transfer may be cache or unavailable, not proved backend zero work'};
  const after=await cdp('Performance.getMetrics'),a=Object.fromEntries(after.metrics.map(m=>[m.name,m.value])),b=Object.fromEntries(before.metrics.map(m=>[m.name,m.value]));
  result.measurements.browser={};for(const key of ['LayoutDuration','RecalcStyleDuration','ScriptDuration','TaskDuration'])result.measurements.browser[key+'DeltaSeconds']=a[key]-b[key];
  result.measurements.browser.jsHeapUsedBytes=a.JSHeapUsedSize;
  // Physical wheel only on the observed graph. Read the actual SVG transform;
  // no DOM event dispatch or fabricated successful paint when nothing changes.
  const v=observation.visibleGraph;
  if(observation.present&&v.right>v.left&&v.bottom>v.top){
    const x=(v.left+v.right)/2,y=(v.top+v.bottom)/2;
    const owned=await evaluate(`(()=>{const e=document.elementFromPoint(${x},${y});return Boolean(e?.closest('[data-code-viewport]'));})()`);
    if(owned){
      await evaluate(`window.__s00Input={start:performance.now(),before:document.querySelector('[data-code-viewport] g')?.getAttribute('transform'),changed:null};window.__s00Input.frame=requestAnimationFrame(function step(){const s=window.__s00Input;if(document.querySelector('[data-code-viewport] g')?.getAttribute('transform')!==s.before){s.changed=performance.now()-s.start;return;}if(performance.now()-s.start<1000)s.frame=requestAnimationFrame(step);});`);
      await cdp('Input.dispatchMouseEvent',{type:'mouseWheel',x,y,deltaX:0,deltaY:40});
      await new Promise(r=>setTimeout(r,1050));
      result.measurements.inputToObservedTransformMs=await evaluate(`(()=>{cancelAnimationFrame(window.__s00Input.frame);const v=window.__s00Input.changed;delete window.__s00Input;return v;})()`);
    }else result.measurements.inputNotMeasured='Graph center not pointer-owned';
  }
  result.measurements.inputDefinition='One real CDP wheel to observed SVG transform mutation at rAF; not compositor presentation proof; five repetitions and real headed active-frame test remain required';
  result.serverAfter=processIdentity(config.server);result.browserAfter=processIdentity(config.browser,true);
  assert.deepEqual(result.serverAfter,serverBefore);assert.deepEqual(result.browserAfter,browserBefore);
  assert(await screenshotGuard(),'Screenshot principal/origin guard unavailable');
  const image=await cdp('Page.captureScreenshot',{format:'png'});write('graph.png',Buffer.from(image.data,'base64'));
  result.status=result.failures.length?'failed':'passed-stage-only';
  result.remaining=['five cold/warm repetitions','headed hardware/GPU classification','actual compositor presentation timing','Files and both Editors normal navigation and fixture identity assertions','normal source/package combined qualification'];
}catch(error){result.failures.push(String(error.message||error));
  if(ws?.readyState===WebSocket.OPEN){try{assert(await screenshotGuard(),'Screenshot principal/origin guard unavailable');const id=++seq;const image=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('screenshot expired')),3000);pending.set(id,m=>{clearTimeout(timer);m.error?reject(Error('screenshot rejected')):resolve(m.result);});ws.send(JSON.stringify({id,method:'Page.captureScreenshot',params:{format:'png'}}));});write('failure.png',Buffer.from(image.data,'base64'));}catch{result.failureScreenshotUnavailable=true;}}
}finally{try{await cleanupRecorder();}catch{result.recorderCleanupUnconfirmed=true;}ws?.close();write('result.json',JSON.stringify(result,null,2)+'\n');}
console.log(JSON.stringify({output,status:result.status,failures:result.failures.length,purpose:config.purpose}));
if(result.status==='failed'&&config.purpose!=='baseline-recording')process.exitCode=1;
