// Clankshop uses the existing chat, scoped actor/companion, Stats and permission owners.
// One opt-in observer supplies every presentation; account preferences use the existing prefs owner.
// The mounted applet owns and tears down its refresh, animation, audio, picker and listeners.
import sessionModule from '../sessions.js';
import { renderPermissionCard } from '../chatRenderer.js';
import { MAX_CLANKS, STATE_TEXT, ROOM_LABELS, chooseChats, chatUnits, sceneUnits, placeUnits, liveLine, colourFor,
  stationFor, toolStation } from './clankshopModel.js';
import { VIEW_W, VIEW_H, badgeSvg } from './clankshopArt.js';
import { factoryBackdrop, factoryRobot, driftBackdrop, driftCityBackdrop, driftStorefronts, driftCar, driftPickup, driftHazard, idleHousing } from './clankshopScenes.js';
import { canonicalUsage, derivePlay, ACTIVE_MODES, activeMode, actorRunKey, createPresentation, pressPhase,
  sampleUsage, PREVIEW_DEFAULT, PREVIEW_CALLS_MAX, PREVIEW_PACE_MAX, PRESS_CAPACITY, TOOL_CATEGORIES,
  createPreviewWorkers, resetPreviewCadence, advancePreviewCadence, driftCourse } from './clankshopModes.js';
import { createClankshopAudio, CLANKSHOP_CUE_VOLUMES } from './clankshopAudio.js';
import { DRIFT_METERS, DRIFT_STYLES } from './driftTransport.js';
import { DRIFT_PALETTES } from './driftHarmony.js';
import { acknowledgeVisible } from '../achievementProducer.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { createResourcePicker } from './resourcePicker.js';
const CUE_LEVELS = Object.freeze({
  activity_volume:'Activity music layer', pickup_volume:'Pickup voices', drift_volume:'Drift screech',
  dodge_volume:'Hazard dodges', tool_volume:'Tool sounds', alert_volume:'Alerts and completion',
});
const DEFAULT_SETTINGS = Object.freeze({version:1,drift_meter:'adaptive',drift_pickup_scale:'major',drift_style:'rally-house',
  ...Object.fromEntries(Object.keys(CUE_LEVELS).map(key=>[key,1])),mode:'workshop',motion:'system',intensity:'normal',drawn_actor_limit:8,followed_chat_limit:8,effects_budget:12,audio_enabled:false,music_enabled:true,hits_enabled:true,master_volume:.15,music_volume:.55,effects_volume:.8,background_audio:'silent',pause_in_background:false,completion_cue:true});

const REFRESH_MS = 10000;
const BACKGROUND_FRAME_MS = 250;
const AUDIO_OPTIONS=new Set(['effects_budget','master_volume','music_volume','effects_volume',
  'background_audio','music_enabled','hits_enabled','completion_cue','drift_meter','drift_pickup_scale','drift_style',
  'activity_volume',...Object.values(CLANKSHOP_CUE_VOLUMES)]);
const FACTORY_START = Object.freeze({intake:24,assembly:256,calibration:488,bench:720});
const SERVICE_START = Object.freeze({dispatch:24,attention:256,rest:488,repair:720});
const EYE_ASSET = '/static/brand/open-clank/PNG-Transparent-1024/clanker-round-eye-toon.png';
const SVG_NS = 'http://www.w3.org/2000/svg';
const DRIFT_ROUTES=Object.freeze({mega:'Mega Highway',spaghetti:'Highway Spaghetti'});
const DRIFT_DEFAULT=Object.freeze({route:'mega',density:60,separation:65,pace:1});
const ROAD_HORIZON=1.23;
const clampTraffic=(value,min,max)=>Math.max(min,Math.min(max,value));

function stationPoint(room, slot, serviceExpanded) {
  const row = Math.floor(slot / 4), column = slot % 4;
  if (room in FACTORY_START) return {x:FACTORY_START[room]+32+column*50,y:row ? 302 : 397,scale:.78};
  return {x:SERVICE_START[room]+32+column*50,y:serviceExpanded ? (row ? 610 : 548) : 583,scale:serviceExpanded ? .64 : .78};
}

const visibleAction = unit => unit.currentAction || (unit.state === 'working'
  ? 'Running · No current tool reported' : STATE_TEXT[unit.state]);
const MILESTONES = Object.freeze({N31:'A Visit to Clankshop',N32:'A Different View'});
const INCOGNITO_KEY = 'ody-incognito-sessions'; // same sessionStorage list sessions.js keeps
const el = (tag, props = {}, ...kids) => { const n = document.createElement(tag); for (const [k, v] of Object.entries(props)) { if (k === 'text') n.textContent = v; else if (k in n) n[k] = v; else n.setAttribute(k, v); } n.append(...kids); return n; };
const incognitoIds = () => { try { return JSON.parse(sessionStorage.getItem(INCOGNITO_KEY) || '[]'); } catch (_) { return []; } };

async function getJson(url, signal) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', signal });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

export function createClankshopFeature() {
  let root = null, win = null, stage = null, unitsLayer = null, listEl = null, lineEl = null, cardsEl = null, sendChat = null, sendText = null, sendNote = null, liveBox = null;
  let timer = null, abort = null, epoch = 0, observer = null, bound = [], toy = null, modeSelect = null, audioButton = null, audio = null, audioArmed = false, selectedDetail = null;
  let audioStatus=null, audioIssue='', audioAttempt=null, audioView=null, mixerSettings=null, mixerArmed=null, mixerScene=null;
  let preferenceWrites=Promise.resolve();
  const trafficByPanel=new WeakMap();
  let driftLayout={...DRIFT_DEFAULT};
  let animation=null, frameTime=0, arrival=0, lastObservation=null, lastReadoutAt=0, wasActive=null, wasVisible=null;
  let preview=null, livePresentation=createPresentation(), fields=new Map(), placed=[];
  let previewPanel=null, updatePreviewControls=null, debugButton=null, previewTag=null, selectedOpen=null, stationStatus=null, playStatus=null;
  let selectedUnitId=null, liveOnly=null, unitButtons=new Map(), refreshBtn=null;
  let profileBadges=null, badgeSelection=new Set(), galleryButton=null, scopeRevision=0;
  const permissionCards = new Map();
  let settings = {...DEFAULT_SETTINGS}, controls = new Map(), profilePicker = null, earnedLine = null;
  let profileActor = null, profileTheme = null, profilePreview = null, saveProfile = null, mascotData = null;
  const model = { live: false, account: '', loaded: false, failures: 0, units: [], more: 0, permissions: new Map(), activity: new Map(), usage: new Map(), achievements: new Map(), chats: [] };

  const minimized = () => !!win?.root?.classList.contains('modal-minimized');
  const viewVisible = () => !!root?.isConnected && !!win?.visible && !minimized() && !document.hidden;
  const active = () => !!root?.isConnected && (!!win?.visible || minimized())
    && (!settings.pause_in_background || (viewVisible() && document.hasFocus()));

  function stopAudio() {
    audioAttempt=null;
    audio?.close();
    audio=createClankshopAudio({onTiming:renderAudioTiming});
    audioArmed=false;
    audioIssue='';
    audioView=mixerSettings=mixerArmed=mixerScene=null;
  }

  function syncAudio() {
    if (!audio || !audioButton || !audioStatus) return;
    const scene=viewMode(), focused=document.hasFocus(), prefs=audioPreferences();
    const hidden=document.hidden || minimized() || !win?.visible || !root?.isConnected;
    const paused=!!preview?.paused, suspended=hidden || paused;
    const enabled=audioArmed || !!audioAttempt;
    const visibility=`${focused}:${suspended}`;
    if (audioView!==visibility) {
      audioView=visibility;
      if (!focused || suspended) audio.setActivity?.(0);
      const mixer=audio, selected=preview;
      void mixer.visibility(focused,suspended).catch(()=>{
        if (audio!==mixer || preview!==selected || audioView!==visibility || !focused || suspended || !audioArmed) return;
        audioArmed=false;
        if (selected) selected.audioIssue='Could not resume audio. Select Enable audio to retry.';
        else audioIssue='Could not resume audio. Select Enable audio to retry.';
        syncAudio();
      });
    }
    if (mixerScene!==scene) { audio.setScene?.(scene); mixerScene=scene; }
    if (mixerSettings!==prefs || mixerArmed!==enabled) {
      audio.configure({...prefs,audio_enabled:enabled});
      mixerSettings=prefs;
      mixerArmed=enabled;
    }
    const issue=preview ? preview.audioIssue : audioIssue;
    const playing=paused ? 'Audio paused in Debug.' : hidden ? 'Audio suspended while Clankshop is hidden.'
      : !focused && prefs.background_audio==='silent' ? 'Audio quiet in the background.'
      : prefs.master_volume===0 ? 'Audio is on with master volume at zero.'
      : !focused && prefs.background_audio==='quarter' ? 'Audio at reduced volume in the background.' : 'Audio on.';
    const status=audioAttempt ? 'Starting audio…' : [audioArmed ? playing : 'Audio muted.',issue].filter(Boolean).join(' ');
    if (audioStatus.textContent!==status) audioStatus.textContent=status;
    audioButton.textContent=audioArmed?'Mute audio':'Enable audio';
    audioButton.setAttribute('aria-busy',String(!!audioAttempt));
  }

  function resetAudioActivity() {
    audioAttempt=null;
    audio?.resetWork();
    audio?.setActivity(0);
    audio?.configure({...audioPreferences(),audio_enabled:false});
    mixerSettings=null;
  }

  async function toggleAudio() {
    if (audioAttempt || !audio) return;
    const mixer=audio, mounted=root, selected=preview, scope=scopeRevision;
    const current=()=>audio===mixer && root===mounted && preview===selected && scopeRevision===scope;
    const setIssue=value=>{ if (selected) selected.audioIssue=value; else audioIssue=value; };
    setIssue('');
    if (audioArmed) {
      audioArmed=false;
      syncAudio();
    } else {
      const attempt={};
      audioAttempt=attempt;
      syncAudio();
      try {
        const enabled=await mixer.enable({...audioPreferences(),audio_enabled:true});
        if (!current() || audioAttempt!==attempt) return;
        audioArmed=enabled;
        if (!enabled) setIssue('Audio is unavailable in this browser.');
      } catch {
        if (!current() || audioAttempt!==attempt) return;
        setIssue('Could not enable audio. Select Enable audio to retry.');
      }
      if (!current() || audioAttempt!==attempt) return;
      audioAttempt=null;
      syncAudio();
      if (!audioArmed) return;
    }
    if (!selected) {
      const preference=audioArmed;
      try { await saveSettings({audio_enabled:preference}); }
      catch {
        if (!current() || audioArmed!==preference || audioAttempt) return;
        setIssue('Could not save the audio preference.');
        syncAudio();
      }
    }
  }
  function invalidateActivity() {
    model.activity.clear(); model.usage.clear();
    lastObservation=null;
    livePresentation.suspend();
    for (const car of trafficByPanel.get(livePresentation)?.values() || []) car.rate=0;
    if (!preview) audio?.setActivity(0,livePresentation.road.actions);
  }

  function listen(target, name, fn) { target.addEventListener(name, fn); bound.push(() => target.removeEventListener(name, fn)); }

  const viewMode = () => preview?.mode || activeMode(settings.mode);
  const displayUnits = () => preview?.units || model.units;
  const presentation = () => preview?.display || livePresentation;
  const audioPreferences = () => preview?.audioSettings || settings;
  const driftConfig = () => preview?.drift || driftLayout;

  function syncTraffic(panel,units) {
    let cars=trafficByPanel.get(panel);
    if (!cars) { cars=new Map(); trafficByPanel.set(panel,cars); }
    const eligible=units.filter(unit=>unit.live);
    const ids=new Set(eligible.map(unit=>unit.id));
    for (const id of cars.keys()) if (!ids.has(id)) cars.delete(id);
    const taken=new Set([...cars.values()].map(car=>car.slot));
    for (const unit of eligible) {
      const existing=cars.get(unit.id);
      if (existing) { existing.unit=unit; continue; }
      let slot=0;
      while (taken.has(slot)) slot++;
      taken.add(slot);
      const run=actorRunKey(unit), calls=panel.tracks.get(run)?.calls || 0;
      cars.set(unit.id,{unit,slot,run,calls,total:calls,rate:0,age:Infinity});
    }
    return cars;
  }

  function sampleTraffic(panel,cars,elapsed) {
    for (const car of cars.values()) {
      const run=actorRunKey(car.unit), reported=panel.tracks.get(run)?.calls || 0;
      const delta=run===car.run ? Math.max(0,reported-car.calls) : reported;
      car.total+=delta;
      car.calls=run===car.run ? Math.max(car.calls,reported) : reported;
      car.run=run;
      car.rate=elapsed>0 && car.unit.state==='working' ? Math.min(14,delta/elapsed) : 0;
      car.age=0;
    }
  }

  function trafficRate(car) {
    return car.unit.state==='working' ? car.rate*Math.max(0,1-Math.max(0,car.age-2)/11) : 0;
  }

  function visibleTraffic(panel) {
    const limit=settings.drawn_actor_limit;
    return [...(trafficByPanel.get(panel)?.values() || [])].sort((a,b)=>a.slot-b.slot).slice(0,limit);
  }

  function advanceTraffic(panel,dt) {
    for (const car of trafficByPanel.get(panel)?.values() || []) {
      car.age+=dt;
    }
  }
  const swatch = colour => {
    const dot = el('span', {class:'clankshop-swatch','aria-hidden':'true'});
    dot.style.background = colour; dot.style.color = colour;
    return dot;
  };

  function releasePreview() {
    if (!preview) return;
    const armed=preview.savedAudioArmed;
    preview = null;
    audioArmed=armed;
    resetAudioActivity();
    invalidateActivity();
    draw();
    syncTimer();
    if (active()) void refresh().catch(() => {});
  }

  function previewSetup() {
    const config = {...PREVIEW_DEFAULT};
    const display = createPresentation({paceCeiling:PREVIEW_PACE_MAX});
    display.apply([],1,{input:config.input,output:config.output});
    return { config, display, drift:{...DRIFT_DEFAULT}, workers:createPreviewWorkers(), units:[], sequence:0, target:0,
      pending:[], sampleAge:0, burstPending:false, paused:false };
  }

  function syncPreviewUnits() {
    preview.units = preview.workers.slice(0,preview.config.actors).map(worker => {
      const {actorId,sessionId,executionRevision,name,rate,category} = worker;
      const state = worker.state || (rate > 0 ? 'working' : 'idle');
      const currentAction = state === 'working' ? 'Tool: ' + category : null;
      return {id:actorId,sessionId,actorId,executionRevision,name,state,colour:colourFor(actorId),
        chatTitle:'Preview',preview:true,live:true,currentAction,
        currentStation:currentAction && toolStation(currentAction,'reported_tool_name')};
    });
  }

  function enterPreview() {
    if (preview) return;
    epoch += 1; abort?.abort();
    profilePicker?.destroy(); profilePicker=null;
    invalidateActivity();
    clearSelection();
    resetAudioActivity();
    preview = {...previewSetup(),drift:{...driftLayout},mode:activeMode(settings.mode),savedAudioArmed:audioArmed,audioIssue:'',audioSettings:{...settings}};
    syncPreviewUnits();
    draw();
    syncTimer();
    previewPanel.querySelector('input')?.focus();
  }

  function makePreviewEvent(worker, cue = null) {
    const {sessionId,actorId,executionRevision,category}=worker;
    return { sessionId,actorId,executionRevision,callId:`preview-${++preview.sequence}`,
      label:'Tool: '+category, ...(cue ? {cue} : {}) };
  }

  function addPreviewCall(worker) {
    preview.pending.push(makePreviewEvent(worker));
  }

  function sendPreviewCue(kind) {
    if (!preview || preview.paused) return;
    const worker = preview.workers[preview.target];
    if (kind === 'repeat') {
      if (worker.lastEvent && preview.display.hasReceipt(worker.lastEvent)) preview.display.record(worker.lastEvent);
    } else if (kind === 'burst') {
      if (preview.burstPending || worker.state || worker.rate === 0) return;
      preview.burstPending=true;
      for (let n=0; n<PRESS_CAPACITY+1; n++) addPreviewCall(worker);
    } else {
      worker.state = {completion:'done',failure:'failed',question:'question',permission:'permission'}[kind] || null;
      resetPreviewCadence(worker);
      if (worker.state) preview.display.record(makePreviewEvent(worker,kind));
      syncPreviewUnits();
    }
    draw();
  }

  function previewReading() {
    const cfg=preview.config;
    return cfg.usageGap ? {input:null,output:null} : {input:cfg.input,output:cfg.output};
  }

  function previewClock(dt, paint) {
    for (let i=0; i<preview.config.actors; i++) {
      const worker = preview.workers[i];
      const arrivals = advancePreviewCadence(worker,dt);
      for (let n=0; n<arrivals; n++) addPreviewCall(worker);
    }
    preview.sampleAge += dt;
    if (preview.sampleAge < .35) return;
    const hasArrivals = preview.pending.length > 0;
    const cars=syncTraffic(preview.display,preview.units);
    preview.display.apply(preview.pending,preview.sampleAge,previewReading());
    sampleTraffic(preview.display,cars,preview.sampleAge);
    audio?.setActivity((preview.display.reading().pace || 0)*(preview.mode==='drift' ? preview.drift.pace : 1),
      preview.display.road.actions);
    for (const event of preview.pending) {
      const worker=preview.workers.find(unit=>unit.actorId===event.actorId);
      worker.lastEvent=event;
    }
    preview.pending.length=0;
    preview.sampleAge=0;
    preview.burstPending=false;
    if (hasArrivals && paint) updatePreviewControls?.(true);
  }

  function setupPreview() {
    const sliders = new Map();
    previewPanel = el('section', {class:'cs-preview','aria-label':'Simulated Clankshop controls', hidden:true});
    for (const [key,label,unit,min,max,step] of [
      ['actors','Workers','workers',1,MAX_CLANKS,1],
      ['input','Input rate','tokens/s',0,1300,10], ['output','Output rate','tokens/s',0,1300,10],
    ]) {
      const detail = el('output');
      const input = el('input',{type:'range', min,max,step});
      sliders.set(key, {input,detail,unit});
      input.addEventListener('input', () => {
        if (!preview) return;
        const value = Number(input.value);
        if (key === 'actors') {
          for (let n=preview.config.actors; n<value; n++) resetPreviewCadence(preview.workers[n]);
          preview.target = Math.min(preview.target,value-1);
        }
        preview.config[key] = value;
        if (key === 'actors') syncPreviewUnits();
        else preview.display.setUsage(previewReading());
        draw();
      });
      previewPanel.append(el('label',{class:'cs-preview-slider'},el('span',{text:label}),input,detail));
    }
    const driftControls=el('fieldset',{class:'cs-drift-debug',hidden:true},el('legend',{text:'Drift course'}));
    const driftSliders=[
      ['density','Roadside buildings',10,100,5],
      ['separation','Lane separation',10,100,5], ['pace','Playback speed',.25,2,.25],
    ].map(([key,label,min,max,step])=>{
      const input=el('input',{type:'range',min,max,step}), result=el('output');
      input.addEventListener('input',()=>{
        if (!preview) return;
        preview.drift[key]=Number(input.value);
        if (key==='pace' && !preview.paused) {
          const reading=preview.display.reading();
          if (!reading.coasting && reading.pace>0) audio?.setActivity?.(reading.pace*preview.drift.pace);
        }
        draw();
      });
      const row=el('label',{class:'cs-preview-slider'},el('span',{text:label}),input,result);
      driftControls.append(row);
      return {key,input,result,row};
    });
    const defaults = createPreviewWorkers();
    const actorRows = defaults.map(({name},index) => {
      const category = el('select', {'aria-label':`${name} tool category`},
        ...TOOL_CATEGORIES.map(value=>el('option',{value,text:value})));
      const rate = el('input',{type:'range',min:0,max:PREVIEW_CALLS_MAX,step:.25,'aria-label':`${name} calls per second`});
      const value = el('span');
      const row = el('div',{class:'cs-preview-worker'},
        el('strong',{text:name}),category,el('label',{class:'cs-preview-worker-rate'},rate,value));
      const update = (key,next) => {
        if (!preview || index>=preview.config.actors) return;
        const worker = preview.workers[index];
        worker[key] = next;
        worker.state = null;
        resetPreviewCadence(worker);
        syncPreviewUnits();
        draw();
      };
      category.addEventListener('change',()=>update('category',category.value));
      rate.addEventListener('input',()=>update('rate',Number(rate.value)));
      return {row,category,rate,value};
    });
    const combined = el('p',{class:'cs-preview-total'});
    const workers = el('div',{class:'cs-preview-workers',role:'group','aria-label':'Per-worker tool activity'},...actorRows.map(actor=>actor.row));
    const gap = el('input',{type:'checkbox'});
    gap.addEventListener('change',()=>{ if (preview) { preview.config.usageGap=gap.checked; preview.display.setUsage(previewReading()); draw(); } });
    const target = el('select', {'aria-label':'Cue worker'}, ...defaults.map(({name},index)=>el('option',{value:String(index),text:name})));
    target.addEventListener('change',()=>{ if (preview) { preview.target=Number(target.value); updatePreviewControls(); } });
    const cue = el('select', {'aria-label':'Preview cue'}, ...[['burst','Release burst'],['repeat','Repeat last receipt'],
      ['completion','Completion'],['failure','Failure'],['question','Question'],['permission','Permission'],
      ['resume','Resume worker']].map(([value,text])=>el('option',{value,text})));
    cue.addEventListener('change',()=>updatePreviewControls());
    const dispatch = el('button',{type:'button',class:'copal-btn',text:'Show cue'});
    dispatch.addEventListener('click',()=>sendPreviewCue(cue.value));
    const pause = el('button',{type:'button',class:'copal-btn',text:'Pause'});
    pause.addEventListener('click',()=>{
      if (!preview) return;
      preview.paused=!preview.paused;
      if (preview.paused) preview.display.cues();
      frameTime=0;
      draw();
    });
    const reset = el('button',{type:'button',class:'copal-btn',text:'Reset preview'});
    reset.addEventListener('click',()=>{
      if (!preview) return;
      resetAudioActivity();
      Object.assign(preview,previewSetup());
      clearSelection(); syncPreviewUnits(); draw();
    });
    previewPanel.prepend(el('h3',{text:'Debug preview'}),el('p',{text:'Simulated activity stays in this window.'}));
    previewPanel.append(combined,driftControls,workers,el('label',{},gap,' Usage unavailable'),
      el('div',{class:'cs-preview-actions'},target,cue,dispatch),el('div',{class:'cs-preview-actions'},pause,reset));
    updatePreviewControls=(onlyCue=false)=>{
      previewPanel.hidden=!preview;
      if (!preview) return;
      const selectedWorker=preview.workers[preview.target];
      const unavailable=preview.paused
        || cue.value==='burst' && (preview.burstPending || !!selectedWorker.state || selectedWorker.rate===0)
        || cue.value==='repeat' && (!selectedWorker.lastEvent || !preview.display.hasReceipt(selectedWorker.lastEvent));
      dispatch.setAttribute('aria-disabled',String(unavailable));
      if (onlyCue) return;
      driftControls.hidden=preview.mode!=='drift';
      for (const {key,input,result,row} of driftSliders) {
        row.hidden=(key==='separation' && preview.drift.route!=='spaghetti') || (key==='density' && preview.drift.route==='spaghetti');
        const value=preview.drift[key];
        input.value=String(value);
        result.textContent=key==='pace' ? `${value}×` : `${value}%`;
      }
      for (const [key,{input,detail,unit}] of sliders) {
        input.value=String(preview.config[key]);
        detail.textContent=`${input.value} ${unit}`;
      }
      let total=0, configured=0;
      actorRows.forEach(({row,category,rate,value},i)=>{
        const worker=preview.workers[i], visible=i<preview.config.actors;
        row.hidden=!visible;
        target.options[i].hidden=!visible;
        target.options[i].disabled=!visible;
        if (!visible) return;
        category.value=worker.category;
        rate.value=String(worker.rate);
        value.textContent=`${worker.rate} calls/s`;
        configured += worker.rate;
        if (!worker.state) total += worker.rate;
      });
      combined.textContent=total===configured ? `${total} calls/s combined` : `${total} of ${configured} calls/s active`;
      target.value=String(preview.target);
      gap.checked=preview.config.usageGap;
      pause.textContent=preview.paused?'Resume':'Pause';
    };
  }

  function clearSelection() {
    selectedUnitId = null;
    listEl?.replaceChildren();
    unitButtons.clear();
  }

  function selectUnit(id) {
    selectedUnitId = id;
    drawWorkers();
    updateMode(performance.now(),true);
  }

  function drawWorkers() {
    const {drawn, extra, all} = sceneUnits(displayUnits(), settings.drawn_actor_limit);
    placed = placeUnits(drawn, placed);
    if (!all.some(unit => unit.id === selectedUnitId)) selectedUnitId = null;
    const keep = new Set();
    const priority=new Set(drawn.map(unit=>unit.id));
    const listed=[...drawn,...all.filter(unit=>!priority.has(unit.id))].slice(0,128);
    for (const unit of listed) {
      const {id,name,chatTitle,colour} = unit;
      let button = unitButtons.get(id);
      if (!button) {
        button = el('button',{type:'button',class:'cs-worker-button'});
        button.addEventListener('click',()=>selectUnit(id));
        unitButtons.set(id,button);
      }
      const caption = `${name} · ${ROOM_LABELS[stationFor(unit)]} · ${visibleAction(unit)}`;
      if (button.title !== caption) {
        button.title=caption;
        button.setAttribute('aria-label', [name, chatTitle===name ? null : chatTitle, ROOM_LABELS[stationFor(unit)], visibleAction(unit)].filter(Boolean).join(' · '));
        button.replaceChildren(swatch(colour),el('span',{text:name}),el('small',{text:chatTitle===name?visibleAction(unit):chatTitle}));
      }
      button.setAttribute('aria-pressed',String(id===selectedUnitId));
      const row = button.parentElement || el('li',{},button);
      if (listEl.children[keep.size] !== row) listEl.insertBefore(row,listEl.children[keep.size] || null);
      keep.add(id);
    }
    for (const [id,button] of unitButtons) if (!keep.has(id)) { button.parentElement?.remove(); unitButtons.delete(id); }
    const selected = all.find(unit => unit.id === selectedUnitId);
    selectedOpen.hidden = !selected || !!preview;
    selectedOpen.textContent = selected?.requestRef ? 'Open question' : selected?.state === 'permission' ? 'Open chat permission' : 'Open chat';
    if (selected) {
      const chosenRoom = stationFor(selected);
      const parent = selected.parentRef && all.find(unit => unit.sessionId===selected.parentRef.sessionId && unit.actorId===selected.parentRef.nativeId);
      const parts = [selected.name, ROOM_LABELS[chosenRoom], visibleAction(selected),
        selected.lastAction ? `Last recorded action ${selected.lastAction}` : null,
        parent ? `Parent: ${parent.name} (${parent.chatTitle})` : null];
      selectedDetail.textContent=parts.filter(Boolean).join(' · ');
    } else if (all.length>listed.length) selectedDetail.textContent=`Showing ${listed.length} workers from the loaded pages.`;
    else selectedDetail.textContent = all.length ? 'Select a worker to see its station and latest action.' : 'Workers appear here as observations are loaded.';
    const badge = stage.querySelector('#cs-badge');
    const markup = badgeSvg(extra);
    if (badge.dataset.rendered !== markup) { badge.dataset.rendered=markup; badge.innerHTML=markup; }
  }

  function paintWorkshop(readout) {
    const panel = presentation();
    const existing = new Map([...unitsLayer.children].map(node=>[node.dataset.unitId,node]));
    const expanded = new Set(placed.filter(unit=>unit.slot>=4).map(unit=>unit.room));
    for (const unit of placed) {
      let node = existing.get(unit.id);
      if (!node) {
        node=document.createElementNS(SVG_NS,'g');
        node.dataset.unitId=unit.id;
        node.classList.add('cs-worker-sprite');
        node.addEventListener('click',()=>selectUnit(unit.id));
        const title = document.createElementNS(SVG_NS,'title');
        const body = document.createElementNS(SVG_NS,'g');
        const signal = document.createElementNS(SVG_NS,'text');
        signal.classList.add('cs-factory-worker-signal');
        signal.setAttribute('x','15');
        signal.setAttribute('y','-43');
        signal.setAttribute('text-anchor','middle');
        const label = document.createElementNS(SVG_NS,'text');
        label.classList.add('cs-factory-worker-label');
        label.setAttribute('y','13');
        label.setAttribute('text-anchor','middle');
        node.append(title, body, signal, label);
        unitsLayer.append(node);
      }
      const {x,y,scale}=stationPoint(unit.room,unit.slot,expanded.has(unit.room));
      const track=panel.tracks.get(actorRunKey(unit));
      const phase = unit.state === 'working' ? (unit.currentAction ? pressPhase(track) : 'waiting') : unit.state;
      const [title, body, signal, label] = node.children;
      const artwork = JSON.stringify([unit.room,unit.state,phase,unit.colour,track?.charge,scale]);
      if (node.dataset.rendered !== artwork) {
        node.dataset.rendered=artwork;
        body.setAttribute('transform',`scale(${scale})`);
        body.innerHTML=factoryRobot({colour:unit.colour,phase,room:unit.room,state:unit.state,charge:track?.charge || 0});
        node.dataset.workerState=phase;
        signal.textContent=({permission:'!',question:'?',failed:'!',waiting:'···',unavailable:'–',starting:'›',done:'✓'})[phase] || '';
      }
      const detail=`${unit.name} · ${ROOM_LABELS[unit.room]} · ${visibleAction(unit)}`;
      if (title.textContent!==detail) title.textContent=detail;
      const letters=Array.from(unit.name);
      const caption=letters.length>8 ? letters.slice(0,7).join('')+'…' : unit.name;
      if (label.textContent!==caption) label.textContent=caption;
      const position=`translate(${x}px,${y}px)`;
      if (node.style.transform !== position) node.style.transform=position;
      node.classList.toggle('cs-selected-sprite',unit.id===selectedUnitId);
      existing.delete(unit.id);
    }
    for (const node of existing.values()) node.remove();
    if (readout) {
      const tracked = placed.filter(unit => unit.state === 'working' && unit.currentAction && panel.tracks.has(actorRunKey(unit)));
      const rows = tracked.map(unit => {
        const track = panel.tracks.get(actorRunKey(unit)), phase=pressPhase(track);
        const detail = phase === 'settled' && track.releases ? `Last stamp: ${track.batch} calls · ${track.releaseCause}`
          : phase === 'settled' ? 'Settled' : phase === 'charge' ? `Charging ${track.charge} / ${PRESS_CAPACITY} · ${track.cause}`
          : `${{stamp:'Stamping',landing:'Part landing',settling:'Settling'}[phase]} · ${track.batch} calls · ${track.releaseCause}`;
        return {id:unit.id, colour:unit.colour, name:unit.name, detail, room:ROOM_LABELS[unit.room]};
      });
      const markup=JSON.stringify(rows);
      if (stationStatus.dataset.rendered!==markup) {
        stationStatus.dataset.rendered=markup;
        stationStatus.replaceChildren(...rows.map(row=>el('li',{},swatch(row.colour),
          el('strong',{text:row.name}),el('span',{text:`${row.room} · ${row.detail}`}))));
      }
    }
  }

  function draw() {
    if (!root) return;
    drawWorkers();
    syncTraffic(presentation(),displayUnits());
    updateMode(performance.now(),true);
    updatePreviewControls?.();
    debugButton.textContent=preview?'Return to live view':'Debug';
    previewTag.hidden=!preview; liveOnly.hidden=!!preview;
    modeSelect.value=viewMode();
    liveBox.disabled=!!preview;
    refreshBtn.disabled=!!preview;
    liveBox.checked=model.live;
    const ids=earnedIds();
    const milestones=ids.filter(id=>MILESTONES[id]).map(id=>MILESTONES[id]);
    earnedLine.textContent=preview || !model.achievements.size ? '' :
      (milestones.length ? `Clankshop milestones · ${milestones.join(' · ')}` : `${ids.length} earned achievements available`);
    galleryButton.hidden=!!preview || !model.achievements.size;
    lineEl.textContent=preview ? (preview.paused?'Simulated · Paused':'Simulated · Playing')
      : liveLine({enabled:model.live,loaded:model.loaded,failures:model.failures}) + (model.live?' Activity and usage cover loaded root sessions only.':'');
    if (!preview && [...model.activity.values()].some(receipt=>receipt.novelty==='older_revision_bound_reached'))
      lineEl.textContent+=' Some older revisions exceed the animation coverage window.';
    for (const [key,control] of controls) {
      const value=(AUDIO_OPTIONS.has(key) ? audioPreferences() : settings)[key];
      if (control.type==='checkbox') control.checked=value; else control.value=value;
    }
    syncAudio();
    if (!preview) {
      const chosen=sendChat.value;
      sendChat.replaceChildren(...model.chats.map(chat=>el('option',{value:chat.id,text:chat.name || 'Untitled chat'})));
      if (model.chats.some(chat=>chat.id===chosen)) sendChat.value=chosen;
      drawCards(); drawProfile();
    } else cardsEl.replaceChildren();
    syncAnimation();
  }

  function earnedIds() {
    return [...new Set([...model.achievements.values()].flat().map(ref=>ref.achievementId)
      .filter(id=>typeof id==='string' && /^[a-z0-9.-]{1,80}$/i.test(id)))].sort();
  }

  const profileKey = (unit) => JSON.stringify([unit.id,unit.executionRevision]);
  const xml = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'})[c]);
  function profileCard() {
    if (preview || !model.account || !model.live || !profileActor?.value || !mascotData) return null;
    const matches = model.units.filter((unit) => unit.live && profileKey(unit) === profileActor.value && Number.isSafeInteger(unit.executionRevision));
    if (matches.length !== 1) return null;
    const unit=matches[0], dark=profileTheme?.value === 'dark', bg=dark?'#191A1E':'#F4EAD2', ink=dark?'#F4EAD2':'#191A1E';
    const badges=earnedIds().filter(id=>badgeSelection.has(id)).slice(0,3);
    const badgeLabels=badges.map((id,i)=>`<text x="200" y="${183+i*14}" fill="${ink}" font-family="sans-serif" font-size="10">Earned · ${xml(MILESTONES[id] || id)}</text>`).join('');
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 240" role="img" aria-label="Observed Clank profile"><rect width="480" height="240" rx="18" fill="${bg}"/><image x="40" y="45" width="130" height="130" href="${mascotData}"/><text x="200" y="115" fill="${ink}" font-family="sans-serif" font-size="24">${xml(unit.name)}</text><text x="200" y="145" fill="${ink}" font-family="sans-serif" font-size="12" textLength="240" lengthAdjust="spacingAndGlyphs">Actor: ${xml(unit.actorId)} · revision ${unit.executionRevision}</text><text x="200" y="165" fill="${ink}" font-family="sans-serif" font-size="12" textLength="240" lengthAdjust="spacingAndGlyphs">Session: ${xml(unit.sessionId)}</text>${badgeLabels}</svg>`;
  }
  function drawProfile() {
    if (!profileActor || !profilePreview || !saveProfile) return;
    const selected=profileActor.value;
    const actors=model.account && model.live ? model.units.filter((unit)=>unit.live && Number.isSafeInteger(unit.executionRevision)) : [];
    profileActor.replaceChildren(el('option',{value:'',text:'Choose an observed actor'}),...actors.map((unit)=>el('option',{value:profileKey(unit),text:`${unit.name} — ${unit.chatTitle}`})));
    profileActor.value=actors.filter((unit)=>profileKey(unit)===selected).length===1 ? selected : '';
    const ids=earnedIds(), chipMarkup=JSON.stringify(ids);
    if (profileBadges.dataset.rendered!==chipMarkup) {
      profileBadges.dataset.rendered=chipMarkup;
      for (const id of badgeSelection) if (!ids.includes(id)) badgeSelection.delete(id);
      profileBadges.replaceChildren(...ids.map(id=>{
        const check=el('input',{type:'checkbox',checked:badgeSelection.has(id)});
        check.addEventListener('change',()=>{
          if (check.checked && badgeSelection.size>=3) { check.checked=false; return; }
          if (check.checked) badgeSelection.add(id); else badgeSelection.delete(id);
          drawProfile();
        });
        return el('label',{},check,' ',MILESTONES[id] || id);
      }));
    }
    const svg=profileCard(); saveProfile.disabled=!svg;
    if (svg) profilePreview.innerHTML=svg;
    else profilePreview.textContent=mascotData ? 'Select a current observed actor to preview.' : 'Profile mascot unavailable.';
  }
  async function loadProfileMascot() {
    const target=profilePreview;
    try {
      const response=await fetch('/static/icons/treehouse-achievements/kit/mascot.svg',{credentials:'same-origin',cache:'no-store'});
      if (!response.ok) return;
      const bytes=new Uint8Array(await response.arrayBuffer());
      if (!bytes.length || bytes.length>16384) return;
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map((v)=>v.toString(16).padStart(2,'0')).join('');
      if (hash!=='6fb81fd1227982627a87ed46aa9c66753f2af6df3fc78356c736e6729b2755bb' || target!==profilePreview || !root?.isConnected) return;
      mascotData='data:image/svg+xml;base64,'+btoa(String.fromCharCode(...bytes)); drawProfile();
    } catch { /* The optional preview stays unavailable. */ }
  }

  async function openUnit(unit) {
    if (preview || !model.account || !model.chats.some(chat=>chat.id===unit.chatId)) return;
    const account=model.account, request=unit.requestRef, permission=unit.permissionRef;
    await sessionModule.selectSession(unit.chatId,{preserveComposer:false});
    if (!request && !permission) return;
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    if (!root || preview || account!==model.account || sessionModule.getCurrentSessionId()!==unit.chatId
        || !model.units.some(item=>actorRunKey(item)===actorRunKey(unit))) return;
    let card;
    if (request) card=[...document.querySelectorAll('.mimo-question-card')].find(node=>node.dataset.sessionId===unit.chatId
      && node.dataset.requestId===request.requestID && node.dataset.actorSessionId===request.sessionId
      && node.dataset.actorId===request.actorId && node.dataset.questionRef===request.questionRef
      && node.dataset.questionRevision===String(request.revision));
    else card=[...document.querySelectorAll('#chat-history .permission-card')].find(node=>node.dataset.sessionId===unit.chatId
      && node.dataset.requestId===permission && model.permissions.get(unit.chatId)?.request_id===permission);
    if (!card) { sendNote.textContent='The recorded request is no longer visible in that chat.'; return; }
    card.scrollIntoView({block:'nearest'}); (card.querySelector('input,button,textarea,select') || card).focus();
  }

  function reduced() {
    return settings.motion==='reduced' || settings.motion==='system' && globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  }

  function mountMode(mode) {
    if (toy.dataset.mode === mode) return;
    toy.dataset.mode = mode;
    fields = new Map();
    stationStatus.hidden=mode!=='workshop';
    if (mode === 'workshop') {
      toy.replaceChildren();
      playStatus.replaceChildren();
      return;
    }
    const value = (key, initial = '—') => {
      const node = el('span', {text: initial});
      fields.set(key, node);
      return node;
    };
    const stat = (label, key, initial) => el('div', {class:'cs-stat'}, el('small', {text:label}), value(key, initial));
    const sources = el('ul', {class:'cs-contributors', 'aria-label':'Participating workers'});
    fields.set('sources', sources);
    if (mode === 'drift') {
      fields.set('gate-nodes',new Map());
      const course = el('div', {class:'cs-course','aria-hidden':'true'});
      course.innerHTML = `<svg viewBox="0 0 360 640" focusable="false">
        <defs><linearGradient id="cs-drift-road-fill" x2="0" y2="1">
          <stop stop-color="var(--clanker-raised,var(--panel,#34373e))"/>
          <stop offset="1" stop-color="var(--clanker-well,var(--bg,#191a1e))"/>
        </linearGradient></defs>
        <g class="cs-drift-city">${driftCityBackdrop()}</g>
        <g class="cs-drift-industrial" hidden>${driftBackdrop()}</g>
        <g class="cs-traffic-strips"></g>
        <path class="cs-track" fill="url(#cs-drift-road-fill)"/>
        <path class="cs-rails" fill="none" stroke-width="4"/>
        <path class="cs-lanes" fill="none" stroke-width="3.2" stroke-dasharray="11 9"/>
        <g class="cs-car-trails" fill="none" stroke-linecap="round" stroke-width="3">
          <path d="M-31 30q-3 24-16 40m78-40q-3 24-16 40"/>
        </g>
        <g class="cs-storefronts">${driftStorefronts()}</g>
        <g class="cs-encounters"/>
        <g class="cs-drift-car-svg"><g transform="scale(.94)">${driftCar()}</g></g>
      </svg>`;
      for (const key of ['track','rails','lanes','encounters','car-trails','drift-car-svg','traffic-strips','storefronts','drift-city','drift-industrial'])
        fields.set(key,course.querySelector('.cs-'+key));
      const route=el('select',{'aria-label':'Drift route'},
        ...Object.entries(DRIFT_ROUTES).map(([value,text])=>el('option',{value,text})));
      route.value=driftConfig().route;
      route.addEventListener('change',()=>{
        if (!Object.hasOwn(DRIFT_ROUTES,route.value)) return;
        driftConfig().route=route.value;
        draw();
      });
      fields.set('drift-route',route);
      const note=el('p',{class:'cs-mode-summary',text:'Recent bursts drive speed and trigger drifts on sharp bends. Total calls make the road curvier.'});
      const stats = el('div',{class:'cs-drift-stats'},el('h3',{text:'Drift'}),el('label',{class:'cs-drift-route'},'Route',route),note,
        stat('Course speed','speed','0 / 100'),stat('Pickups','pickups','0'),stat('Hazards avoided','dodges','0'),stat('Drifts','drifts','0'),stat('Curviness','curviness','0%'),stat('Total tool calls','worker-calls','0'),stat('Time signature','meter','—'),stat('Tempo','music-bpm','—'),
        el('p',{class:'cs-maneuver'},value('maneuver','Parked')),sources);
      toy.replaceChildren(course);
      playStatus.replaceChildren(stats);
      return;
    }
    const meter = (key, label, ceiling) => {
      const element = el('meter',{min:0,max:ceiling,value:0,'aria-label':label});
      fields.set(key+'-gauge',element);
      return el('div',{class:'cs-meter','data-metric':key},
        el('span',{text:label}), value(key,'Unavailable'), element);
    };
    const housing = el('div',{class:'cs-idle-housing','aria-hidden':'true'});
    housing.innerHTML = idleHousing();
    const eye = el('img',{class:'cs-idle-eye',src:EYE_ASSET,alt:'Open Clank robo-eye',width:1024,height:1024,decoding:'async',draggable:false});
    const machine = el('div',{class:'cs-machine'},housing,eye);
    fields.set('machine',machine);
    toy.replaceChildren(machine);
    playStatus.replaceChildren(el('h3',{text:'Idle'}),el('div',{class:'cs-meter-panel'},
      meter('tools','Tool calls / s',14),meter('input','Input tokens / s',1300),meter('output','Output tokens / s',1300)),
      el('p',{class:'cs-mode-summary'},value('totals','Usage unavailable')), sources);
  }

  function setText(key, value) {
    const node = fields.get(key);
    if (node && node.textContent !== value) node.textContent = value;
  }

  function renderAudioTiming(timing) {
    setText('meter',timing?.meter ?? '—');
    setText('music-bpm',timing?.bpm ? `${Math.round(timing.bpm)} BPM` : '—');
  }

  function updateMode(now, force = false) {
    if (!stage || !toy) return;
    const mode = viewMode(), simulated = !!preview;
    root.dataset.clankshopMode = mode;
    root.toggleAttribute('data-clankshop-preview', simulated);
    toy.hidden = mode === 'workshop';
    stage.hidden = mode !== 'workshop';
    const still = reduced();
    root.classList.toggle('cs-reduced', still);
    root.classList.toggle('cs-full', settings.motion === 'full');
    mountMode(mode);
    const panel = presentation();
    const reading = panel.reading();
    const pulse = panel.active() && !still ? (audio?.beatPhase() || 0) : 1;
    root.style.setProperty('--cs-pulse', String(1 - pulse));
    const lightFrame = force || now - lastReadoutAt >= 240;
    if (lightFrame) lastReadoutAt = now;
    if (mode === 'workshop') { paintWorkshop(lightFrame); return; }
    if (mode === 'drift') {
      const road = panel.road;
      if (lightFrame) {
        setText('speed', `${Math.round((road.speed / .75) * 100)} / 100`);
        setText('pickups', String(road.pickups));
        setText('dodges', String(road.dodges));
        setText('maneuver', road.maneuver);
        setText('worker-calls',road.actions.toLocaleString());
        setText('drifts',String(road.driftCount));
        renderAudioTiming({meter:audio?.driftMeter(),bpm:audio?.driftBpm()});
        setText('curviness',Math.round(road.curviness/1.25*100)+'%');
        fields.get('drift-route').value=driftConfig().route;
      }
      if (!still || lightFrame) paintRoad(panel,still);
    } else {
      const burst = reading.pace > 0 && !reading.coasting
        ? (still ? .34 : Math.max(0, 1 - reading.age / .8)) : 0;
      fields.get('machine').style.setProperty('--cs-tool-burst',String(burst));
    }
    if (mode === 'idle' && lightFrame) {
      const readingValues = {tools:reading.pace,input:reading.usage.input,output:reading.usage.output};
      fields.get('tools-gauge').max=simulated ? PREVIEW_PACE_MAX : 14;
      for (const [key, number] of Object.entries(readingValues)) {
        setText(key, number === null ? 'Unavailable' : number.toLocaleString(undefined,{maximumFractionDigits:1}));
        const gauge = fields.get(key+'-gauge');
        gauge.hidden = number === null;
        gauge.value = number ?? 0;
      }
      const machine = fields.get('machine');
      for (const key of ['input','output']) {
        const rate = readingValues[key];
        machine.toggleAttribute(`data-${key}-missing`,rate === null);
        machine.style.setProperty(`--cs-${key}-level`,String(Math.max(0,Math.min(1,(rate ?? 0) / 1300))));
      }
      if (preview) setText('totals', 'Local preview · Usage rates follow the Debug inputs');
      else {
        const usage = [...model.usage.values()].flatMap(snapshot => snapshot.items || []);
        const events=[...model.activity.values()].flatMap(snapshot=>[...snapshot.events.values()]);
        const play=derivePlay(events,usage);
        const tokens=play.tokens===null ? 'Reported token totals unavailable' :
          `${play.tokens.toLocaleString()} reported tokens · ${play.unassignedTokens.toLocaleString()} unassigned`;
        setText('totals',`${tokens} · ${play.points} play points in loaded snapshots`);
      }
    }
    if (lightFrame) {
      const sources = fields.get('sources');
      const entries = displayUnits().filter(unit => unit.live).slice(0, settings.drawn_actor_limit);
      const caption = mode==='drift'
        ? visibleTraffic(panel).map(car=>{
            const {id,colour,name,chatTitle,actorId}=car.unit;
            return [id,colour,name,chatTitle,actorId,car.total,null,trafficRate(car)>0?'driving':'quiet',car.slot+1];
          })
        : entries.map(unit => {
            const observed = panel.tracks.get(actorRunKey(unit));
            return [unit.id,unit.colour,unit.name,unit.chatTitle,unit.actorId,observed?.calls || 0,unit.reportedTokens];
          });
      const markup = JSON.stringify(caption);
      if (sources.dataset.rendered !== markup) {
        sources.dataset.rendered = markup;
        sources.replaceChildren(...caption.map(([,colour,name,chat,actor,calls,tokens,pace,number]) => el('li', {},
          swatch(colour), el('strong',{text:number ? `${number} · ${name}` : name,title:`${chat} · ${actor}`}),
          el('span',{text:mode==='drift' ? `${calls} calls · ${pace}`
            : `${calls} new observed calls · ${Number.isSafeInteger(tokens) && tokens>=0 ? tokens.toLocaleString()+' reported tokens' : 'usage unavailable'}`}))));
      }
    }
  }

  function paintRoad(panel,still) {
    const {road}=panel, {distance}=road;
    const x = (ahead, lane) => {
      const depth = 1 - ahead / 1.23;
      const bend = 16*depth + 32*Math.sin(Math.PI*depth);
      return 180 + driftCourse(distance + ahead)*road.curviness*bend + (46 + 111*depth)*lane;
    };
    const y = ahead => 541 - (ahead / 1.23) * 387;
    const laneX = (ahead, lane) => x(ahead,(lane - 1) * .69);
    const vertices = Array.from({length:18}, (_, i) => 1.23 - i * 1.23 / 17);
    const path = lane => vertices.map((ahead, i) => `${i ? 'L' : 'M'}${x(ahead,lane).toFixed(1)} ${y(ahead).toFixed(1)}`).join('');
    const left = path(-1) + 'L0 640', right = path(1) + 'L360 640';
    fields.get('track').setAttribute('d', left + 'H360' + [...vertices].reverse().map(ahead => `L${x(ahead,1).toFixed(1)} ${y(ahead).toFixed(1)}`).join('') + 'Z');
    fields.get('rails').setAttribute('d', left + right);
    const spaghetti = driftConfig().route === 'spaghetti';
    fields.get('drift-city').toggleAttribute('hidden',spaghetti);
    fields.get('drift-industrial').toggleAttribute('hidden',!spaghetti);
    const lanes = fields.get('lanes');
    lanes.setAttribute('d', path(-.34) + `L${x(0,-.34)-15} 640` + path(.34) + `L${x(0,.34)+15} 640`);
    lanes.setAttribute('stroke-dashoffset', String(-Math.round(distance * 95) % 17));
    paintRoadside({x,y,vertices,distance});
    const car = fields.get('drift-car-svg');
    const posture = `translate(${laneX(0,road.lane)} ${y(0)}) rotate(${still ? 0 : road.yaw})`;
    car.setAttribute('transform',posture);
    const crew=Array.from(trafficByPanel.get(panel)?.values() || []);
    const energy=crew.reduce((sum,member)=>sum+trafficRate(member),0);
    car.style.setProperty('--cs-exhaust',String(still ? 0 : Math.min(.9,energy/12)));
    const livery=crew.length ? crew[road.pickups % crew.length].unit.colour : '#75d8dc';
    car.style.setProperty('--cs-livery',livery);
    car.classList.toggle('cs-car-glint', road.glint > 0);
    const trails = fields.get('car-trails');
    trails.setAttribute('transform',posture);
    trails.setAttribute('opacity',still ? '0' : String(road.driftTime>0 ? .85 : Math.min(.28,Math.abs(road.yaw)/65)));
    const visible = road.gates.filter(gate => !gate.passed && gate.at - distance <= 1.23);
    const spots = fields.get('encounters'), nodes=fields.get('gate-nodes');
    const keep=new Set();
    for (const gate of visible) {
      const ahead=gate.at-distance;
      const scale=.38+.62*Math.max(0,1-ahead/1.23);
      for (const [lane,art,kind] of [[gate.pickup,driftPickup,'pickup'],[gate.hazard,driftHazard,'hazard']]) {
        if (lane===null) continue;
        const key=`${gate.id}:${kind}`;
        let group=nodes.get(key);
        if (!group) {
          group=document.createElementNS(SVG_NS,'g');
          group.innerHTML=kind==='pickup' ? art(gate.kind) : art();
          spots.append(group);
          nodes.set(key,group);
        }
        group.setAttribute('transform',`translate(${laneX(ahead,lane).toFixed(1)} ${y(ahead).toFixed(1)}) scale(${scale.toFixed(3)})`);
        keep.add(key);
      }
    }
    for (const [key,group] of nodes) if (!keep.has(key)) { group.remove(); nodes.delete(key); }
  }

  function paintRoadside({x,y,vertices,distance}) {
    const {route,density,separation}=driftConfig();
    const spaghetti=route==='spaghetti', spread=separation/100;
    const branchCount=2;
    const stripLayer=fields.get('traffic-strips');
    const roadX=(ahead,side,branch)=>{
      const centre=x(ahead,0);
      if (!spaghetti) return centre;
      const rail=x(ahead,side);
      const margin=Math.max(0,side<0 ? rail-9 : 351-rail);
      const share=branch===0 ? .40-.25*spread : .61+.30*spread;
      const d=clampTraffic((ROAD_HORIZON-ahead)/.43,0,1), join=d*d*(3-2*d);
      const curve=Math.sin(ahead*4.5+branch*1.6)*margin*.06;
      return centre+(rail-centre+side*(margin*share+curve))*join;
    };
    const paths=[];
    for (const side of spaghetti ? [-1,1] : []) for (let branch=0; branch<branchCount; branch++) {
      const samples=vertices.filter(ahead=>ahead>=.27);
      paths.push(samples.map((ahead,i)=>`${i?'L':'M'}${roadX(ahead,side,branch).toFixed(1)} ${y(ahead).toFixed(1)}`).join(''));
    }
    if (stripLayer.dataset.route!==route) {
      stripLayer.dataset.route=route;
      stripLayer.replaceChildren(...paths.map(()=>{
        const branch=document.createElementNS(SVG_NS,'g');
        for (const name of ['edge','surface','line']) {
          const path=document.createElementNS(SVG_NS,'path');
          path.classList.add(`cs-traffic-${name}`);
          branch.append(path);
        }
        return branch;
      }));
    }
    for (const [i,strip] of [...stripLayer.children].entries()) {
      for (const path of strip.children) path.setAttribute('d',paths[i]);
      strip.lastElementChild.setAttribute('stroke-dashoffset',String(-Math.round(distance*73)%17));
    }
    const storefronts=fields.get('storefronts');
    storefronts.toggleAttribute('hidden',spaghetti);
    if (spaghetti) return;
    const count=4+Math.round(density*.05);
    // Project repeated roadside billboards in depth order. Mirror the building
    // geometry on the left so each lit street face points toward the roadway.
    for (let i=0;i<storefronts.children.length-1;i++) {
      const node=storefronts.children[i+1], pair=Math.floor(i/2), side=i%2 ? 1 : -1;
      node.toggleAttribute('hidden',pair>=count);
      if (pair>=count) continue;
      const travel=distance*3.2+(side>0 ? .33 : 0);
      const depth=(pair+travel%1)/count;
      const ahead=ROAD_HORIZON*(1-depth*depth);
      const scale=.11+depth*depth*.99;
      const base=x(ahead,side)+side*(3+5*depth);
      node.setAttribute('transform',`translate(${base.toFixed(1)} ${y(ahead).toFixed(1)}) scale(${(side*scale).toFixed(3)} ${scale.toFixed(3)})`);
      const variant=((Math.floor(travel)-pair)%4+4)%4;
      const href='#cs-facade-'+variant;
      if (node.getAttribute('href')!==href) node.setAttribute('href',href);
    }
  }

  function warmFrame(dt, frameNow, paint) {
    const panel = presentation();
    const driftShown=viewMode()==='drift', {pickups,dodges,driftCount}=panel.road;
    if (preview) previewClock(dt, paint);
    const multiplier=driftConfig().pace;
    panel.advance(dt,settings.intensity,!!preview || driftShown,multiplier);
    if (preview || driftShown) advanceTraffic(panel,dt);
    const cues=panel.cues(), ambient=panel.ambientCues();
    if (!paint) return;
    if (!driftShown) cues.push(...ambient);
    if (driftShown) {
      if (panel.road.dodges>dodges) cues.unshift({kind:'dodge',label:'Hazard avoided'});
      if (panel.road.pickups>pickups) cues.unshift({kind:'pickup',label:'Pickup collected',variation:panel.road.pickupCue});
      if (panel.road.driftCount>driftCount) cues.unshift({kind:'drift',duration:panel.road.driftDuration});
    }
    if (audioArmed) for (const cue of cues.slice(0,audioPreferences().effects_budget)) audio?.hit(cue);
    updateMode(frameNow);
  }

  function needsFrame() {
    const simulatedWork = preview && (preview.pending.length > 0
      || preview.workers.slice(0,preview.config.actors).some(worker=>!worker.state && worker.rate>0));
    return active() && !preview?.paused && (simulatedWork || presentation().active());
  }

  function stopAnimationFrame() {
    if (animation?.background) clearTimeout(animation.id);
    else if (animation) cancelAnimationFrame(animation.id);
    animation=null;
    frameTime=0;
  }

  function enqueueFrame(background) {
    const id=background ? setTimeout(()=>tick(performance.now()),BACKGROUND_FRAME_MS) : requestAnimationFrame(tick);
    animation={id,background};
  }

  function syncAnimation() {
    const needs=needsFrame(), background=!viewVisible();
    if (animation && (!needs || animation.background!==background)) stopAnimationFrame();
    if (needs && !animation) {
      frameTime=performance.now();
      enqueueFrame(background);
    }
  }

  function tick(now) {
    animation=null;
    if (!active() || preview?.paused) { frameTime=0; return; }
    const paint=viewVisible(), staticMode=reduced(), elapsed=now-frameTime;
    if (!paint || !staticMode || elapsed>=150) {
      frameTime=now;
      if (paint) warmFrame(Math.min(staticMode ? .22 : .07,elapsed/1000),now,true);
      else {
        // Hidden tabs stop delivering animation frames. Advance the same
        // simulation on a slower timer, with bounded steps after throttling.
        const dt=Math.min(1,elapsed/1000);
        const steps=Math.max(1,Math.ceil(dt/.07));
        for (let step=0;step<steps;step++) warmFrame(dt/steps,now,false);
      }
    }
    if (needsFrame()) enqueueFrame(!paint);
    else { frameTime=0; if (paint) updateMode(now,true); }
  }

  async function saveSettings(patch) {
    const account=model.account, mounted=root, scope=scopeRevision;
    if (preview || !account || !mounted) throw new Error('settings');
    const current=()=>root===mounted && scopeRevision===scope && model.account===account;
    const write=preferenceWrites.then(async()=>{
      if (!current()) throw new Error('settings');
      const next={...settings,...patch};
      const response=await fetch('/api/prefs/clankshop',{method:'PUT',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({value:next,expected_account_id:account})});
      if (!response.ok || !current()) throw new Error('settings');
      settings=next;
      draw();
      if ('pause_in_background' in patch) syncTimer();
    });
    preferenceWrites=write.catch(()=>{});
    return write;
  }

  // The permission card is the chat's own card (same wording, same route), limited to Allow once and Reject.
  function drawCards() {
    const keep = new Set();
    for (const [chatId, payload] of model.permissions) {
      const chat=model.chats.find(row=>row.id===chatId);
      if (!chat) continue;
      const key=JSON.stringify([chatId,payload.request_id,payload.revision]);
      let holder=permissionCards.get(key);
      if (!holder) {
        holder=el('div',{class:'clankshop-card'},el('strong',{text:`Waiting in ${chat.name || 'a chat'}`}));
        renderPermissionCard(payload,chatId,{container:holder,onceOrReject:true});
        permissionCards.set(key,holder);
      }
      if (cardsEl.children[keep.size]!==holder) cardsEl.insertBefore(holder,cardsEl.children[keep.size] || null);
      keep.add(key);
    }
    for (const [key,holder] of permissionCards) if (!keep.has(key)) { holder.remove(); permissionCards.delete(key); }
  }

  async function loadUnits(chat, signal) {
    const base = `/api/orchestration/${encodeURIComponent(chat.id)}`;
    const cursor = model.activity.get(chat.id)?.cursor;
    const query = cursor ? `?activity_cursor=${encodeURIComponent(cursor)}` : '';
    const [actors, companion] = await Promise.all([getJson(`${base}/observations/native_actor?limit=50`, signal), getJson(`${base}/companion-observation${query}`, signal)]);
    return { actors, companion };
  }

  function activityFor(chat, activity, units, now) {
    const prior = model.activity.get(chat.id);
    if (!activity || !['baseline','delta','partial'].includes(activity.state) || typeof activity.cursor !== 'string') {
      model.activity.delete(chat.id);
      return [];
    }
    const events = new Map(activity.state === 'baseline' ? [] : prior?.events || []);
    const qualified = new Map(units.filter(unit=>unit.live).map(unit=>[actorRunKey(unit),unit]));
    const fresh = prior && ['baseline','delta'].includes(prior.state) && activity.state === 'delta' && now - prior.at <= 25;
    const arrivals = [];
    for (const raw of activity.items || []) {
      const event = {...raw,chatId:chat.id,accountId:model.account};
      if (!qualified.has(actorRunKey(event))) continue;
      const ref = event.sourceRef;
      const terminal=event.coverage==='qualified_terminal' && event.state==='terminal' && ['success','failure'].includes(event.outcome);
      const running=event.coverage==='qualified_activity' && ['started','progress'].includes(event.state) && event.outcome===null
        && Number.isSafeInteger(event.activitySequence) && event.activitySequence>0;
      const label = event.labelBasis==='generic' && event.label==='Tool action' || event.labelBasis==='reported_tool_name'
        && typeof event.label==='string' && event.label.startsWith('Tool: ') && TOOL_CATEGORIES.includes(event.label.slice(6));
      if (!ref?.session_handle || !ref.message_id || !ref.part_id || ref.actor_id!==event.actorId || (!terminal && !running) || !label
        || !Number.isSafeInteger(event.sourceRevision) || event.sourceRevision<1 || typeof event.callId!=='string' || !event.callId) continue;
      const key=JSON.stringify([actorRunKey(event),ref.session_handle,ref.message_id,ref.part_id]);
      const old=events.get(key);
      if (old && (old.callId!==event.callId || event.sourceRevision<=old.sourceRevision
          || old.state==='terminal' && running || running && event.activitySequence<=old.activitySequence)) continue;
      events.set(key,{...event,station:toolStation(event.label,event.labelBasis),_arrival:old?._arrival ?? ++arrival});
      if (!fresh) continue;
      // The Archive decides novelty, including revisions older than
      // our local window. Older producers can prove only revision one.
      const firstCall=event.firstActivity ?? (event.sourceRevision===1 && (event.state==='started' || terminal));
      const firstTerminal=event.firstTerminal ?? (event.sourceRevision===1 && terminal);
      const effectIdentity=JSON.stringify([ref.session_handle,ref.message_id,ref.part_id,event.callId]);
      if (!old && firstCall===true) arrivals.push({...event,effectIdentity});
      if (terminal && old?.state!=='terminal' && firstTerminal===true)
        arrivals.push({...event,effectIdentity,cue:event.outcome==='failure'?'failure':'completion'});
    }
    while (events.size>128) events.delete(events.keys().next().value);
    model.activity.set(chat.id,{cursor:activity.cursor,events,state:activity.state,at:now,sampled:!!fresh,novelty:activity.coverage?.novelty});
    for (const unit of units) {
      const own=[...events.values()].filter(event=>actorRunKey(event)===actorRunKey(unit)).sort((a,b)=>b._arrival-a._arrival);
      const current=own.find(event=>event.coverage==='qualified_activity'), last=own.find(event=>event.coverage==='qualified_terminal');
      if (current && unit.state==='working') { unit.currentAction=current.label; unit.currentStation=current.station; }
      if (last) unit.lastAction=last.label+(last.outcome==='failure'?' failed':' completed');
    }
    return arrivals;
  }

  function scopedUsage(chat, companion, elapsed, units) {
    const old=model.usage.get(chat.id), snapshot=companion?.usage;
    if (snapshot?.state !== 'snapshot') { model.usage.delete(chat.id); return null; }
    const source=companion.source;
    const binding=JSON.stringify([model.account,chat.id,source?.mapRevision,source?.bindingRevision]);
    model.usage.set(chat.id,{...snapshot,binding});
    for (const item of canonicalUsage(snapshot.items).items) {
      if (item.attribution!=='actor') continue;
      const ref = item.actorRef;
      const match=units.find(unit=>ref?.sessionId===unit.sessionId && ref.actorId===unit.actorId
        && ref.executionRevision===unit.executionRevision);
      if (!match) continue;
      const metrics=['input_tokens','output_tokens'].map(key=>item.metrics?.[key]);
      const values=metrics.filter(metric=>metric?.state==='reported' && Number.isSafeInteger(metric.value) && metric.value>=0);
      if (values.length) match.reportedTokens = (match.reportedTokens ?? 0) + values.reduce((n,metric)=>n+metric.value,0);
    }
    return old?.binding === binding && elapsed <= 25 ? sampleUsage(old.items,snapshot.items,elapsed) : null;
  }

  async function refresh() {
    if (!root || preview || !active()) return;
    abort?.abort(); abort = new AbortController();
    const mine=++epoch, signal=abort.signal, account=model.account;
    const visibleChats=()=>chooseChats(sessionModule.getSessions?.() || [],{incognitoIds:incognitoIds(),limit:settings.followed_chat_limit});
    const {shown,more}=visibleChats();
    let failures=0, units=[];
    if (model.live && account) {
      const results=await Promise.allSettled(shown.map(chat=>loadUnits(chat,signal)));
      if (mine!==epoch || !active() || preview || model.account!==account) return;
      const preference=await getJson('/api/prefs/companion_enabled',signal).catch(()=>null);
      if (mine!==epoch || !active() || preview || model.account!==account) return;
      if (preference?.value !== true || String(preference.account_id || '')!==account) {
        if (preference) model.live = false;
        if (!preference || String(preference.account_id || '')!==account) model.loaded=false;
        model.units=[]; model.permissions.clear(); invalidateActivity(); model.achievements.clear();
        draw();
        return;
      }
      const latestIds=new Set(visibleChats().shown.map(chat=>chat.id));
      if (shown.some(chat=>!latestIds.has(chat.id))) {
        model.units=[]; model.permissions.clear(); invalidateActivity(); model.achievements.clear(); draw(); return;
      }
      const now=performance.now()/1000, elapsed=lastObservation===null ? 0 : Math.max(.5,now-lastObservation);
      const liveEvents=[], usageSamples=[];
      const priorRuns=new Map(model.units.filter(unit=>unit.live).map(unit=>[actorRunKey(unit),unit]));
      shown.forEach((chat,i)=>{
        const value=results[i].status==='fulfilled'?results[i].value:null;
        if (!value) failures++;
        const actors=chatUnits(chat,{...(value||{}),permission:model.permissions.get(chat.id)||null,account});
        const companion=value?.companion;
        const earned=companion?.extensions?.achievements;
        if (earned?.status==='available') model.achievements.set(chat.id,earned.refs || []); else model.achievements.delete(chat.id);
        const canCue=model.activity.get(chat.id)?.state==='delta' && companion?.activity?.state==='delta' && elapsed>0 && elapsed<=25;
        liveEvents.push(...activityFor(chat,companion?.activity,actors,now));
        const usage=scopedUsage(chat,companion,elapsed,actors);
        if (usage) usageSamples.push(usage);
        if (canCue) for (const unit of actors) {
          const prior=priorRuns.get(actorRunKey(unit));
          const request=unit.requestRef;
          if (prior && unit.state==='question' && request?.questionRef && prior.requestRef?.questionRef!==request.questionRef) {
            liveEvents.push({...unit,callId:request.questionRef,cue:'question',label:'Question'});
          }
          if (prior && unit.state==='permission' && unit.permissionRef && prior.permissionRef!==unit.permissionRef) {
            liveEvents.push({...unit,callId:unit.permissionRef,cue:'permission',label:'Permission'});
          }
        }
        units.push(...actors);
      });
      const measure = key => {
        const rates=usageSamples.map(sample=>sample[key]).filter(value=>value!==null);
        return rates.length ? rates.reduce((sum,value)=>sum+value,0) : null;
      };
      const sampled=shown.some(chat=>model.activity.get(chat.id)?.sampled);
      const traffic=syncTraffic(livePresentation,units), span=sampled && elapsed<=25 ? elapsed : 0;
      livePresentation.apply(liveEvents,span,{input:measure('input'),output:measure('output')});
      sampleTraffic(livePresentation,traffic,span);
      audio?.setActivity(livePresentation.reading().pace || 0,livePresentation.road.actions);
      livePresentation.forget(new Set(units.filter(unit=>unit.live).map(actorRunKey)));
      lastObservation=now;
    } else {
      units=shown.flatMap(chat=>chatUnits(chat,{permission:model.permissions.get(chat.id)||null,account}));
    }
    const keep=new Set(shown.map(chat=>chat.id));
    for (const source of [model.activity,model.usage,model.achievements,model.permissions]) for (const id of source.keys()) if (!keep.has(id)) source.delete(id);
    Object.assign(model,{units,more,failures,loaded:true,chats:shown});
    draw();
  }

  function syncTimer() {
    const on=active();
    if (wasActive!==on) {
      if (!on && wasActive===true) {
        epoch++; abort?.abort();
        invalidateActivity();
        preview?.display.cues();
        preview?.display.ambientCues();
      }
      if (on && wasActive===false && !preview) void refresh().catch(()=>{});
      wasActive=on;
    }
    const shown=viewVisible();
    if (shown && wasVisible===false) {
      updateMode(performance.now(),true);
      updatePreviewControls?.();
    }
    wasVisible=shown;
    syncAudio();
    root?.classList.toggle('cs-still',!on);
    syncAnimation();
    const observing=on && !preview && model.live;
    if (observing && !timer) timer=setInterval(()=>{ if (active() && !preview) void refresh().catch(()=>{}); else syncTimer(); },REFRESH_MS);
    else if (!observing && timer) { clearInterval(timer); timer=null; }
  }

  async function loadPreference() {
    const mounted=root, scope=scopeRevision;
    const current=()=>scope===scopeRevision && root===mounted;
    try {
      const body = await getJson('/api/prefs/companion_enabled');
      if (!current()) return;
      model.live = body.value === true; model.account = String(body.account_id || '');
      const config = await getJson('/api/prefs/clankshop');
      if (current() && String(config.account_id || '')===model.account) {
        settings={...DEFAULT_SETTINGS,...config.value};
        draw();
        syncTimer();
      }
    } catch (_) { if (current()) model.live=false; }
  }
  async function setLive(value) {
    const account=model.account, mounted=root;
    if (preview || !account) { draw(); return; }
    try {
      const response=await fetch('/api/prefs/companion_enabled',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({value,expected_account_id:account})});
      if (!response.ok) throw new Error();
      if (root!==mounted || model.account!==account) return;
      model.live=value;
      if (!value) { resetAudioActivity(); invalidateActivity(); model.achievements.clear(); model.permissions.clear(); syncAudio(); }
    } catch (_) {
      if (root===mounted) { lineEl.textContent='Could not change live observation.'; liveBox.checked=model.live; }
      return;
    }
    await refresh();
    syncTimer();
  }

  // Send box, variant B: open the chosen chat with the text in its own message box. The existing send path stays the only sender.
  async function openChatWithText() {
    if (preview) return;
    const id = sendChat.value, text = sendText.value;
    if (!id || !text.trim()) { sendNote.textContent = 'Pick a chat and type a message first.'; return; }
    await sessionModule.selectSession(id, { preserveComposer: false });
    const box = document.getElementById('message');
    if (!box) { sendNote.textContent = 'The chat message box is not available.'; return; }
    box.value = text; box.dispatchEvent(new Event('input', { bubbles: true })); box.focus();
    sendNote.textContent = 'The chat is open with your text in its message box. Nothing is sent until you press Send there.';
    sendText.value = '';
  }

  function render(body, windowApi) {
    if (root?.isConnected && body.contains(root)) { syncTimer(); return; }
    destroy();
    win = windowApi || null;
    root = el('div', { class: 'clankshop cs-still' });
    stage = el('div', { class: 'clankshop-stage' });
    stage.innerHTML=`<svg class="cs-scene" viewBox="0 0 ${VIEW_W} ${VIEW_H}" role="img" focusable="false"
      aria-label="Robot factory with intake, assembly, calibration, workbench and service bays. Select workers in the controls panel." font-family="system-ui,sans-serif">
      ${factoryBackdrop()}<g id="cs-units"></g><g id="cs-badge"></g></svg>`;
    stationStatus=el('ul',{class:'cs-stations','aria-label':'Factory station activity'});
    playStatus=el('div',{class:'cs-play-status'});
    toy = el('section', {class:'clankshop-toy',hidden:true,'aria-label':'Activity presentation'});
    audio = createClankshopAudio({onTiming:renderAudioTiming}); audioArmed = false;
    unitsLayer = stage.querySelector('#cs-units');
    modeSelect = el('select', {'aria-label':'View mode'});
    for (const mode of ACTIVE_MODES) modeSelect.append(el('option',{value:mode,text:mode[0].toUpperCase()+mode.slice(1)}));
    modeSelect.addEventListener('change',()=>{
      const mode=activeMode(modeSelect.value);
      if (preview) { preview.mode=mode; draw(); return; }
      saveSettings({mode}).then(()=>{
        if (!preview && mode!=='workshop' && settings.mode===mode && active())
          acknowledgeVisible(toy,'surface.visited',{surface:'clankshop-game',mode,visited:true,deliberate:true},{accountId:model.account});
      }).catch(()=>{ if (modeSelect) modeSelect.value=viewMode(); });
    });
    debugButton=el('button',{type:'button',class:'copal-btn',text:'Debug'});
    debugButton.addEventListener('click',()=>{ if (preview) { releasePreview(); debugButton.focus(); } else enterPreview(); });
    previewTag=el('strong',{class:'cs-demo-tag',text:'Simulated',hidden:true});
    setupPreview();
    audioButton=el('button',{type:'button',class:'copal-btn',text:'Enable audio'});
    audioButton.addEventListener('click',()=>{ void toggleAudio(); });
    audioStatus=el('p',{class:'clankshop-line',role:'status'});
    refreshBtn = el('button', { type: 'button', class: 'copal-btn', text: 'Refresh', title: 'Look at the chats again' });
    refreshBtn.addEventListener('click', () => { refresh().catch(() => {}); });
    liveBox = el('input', { type: 'checkbox', id: 'clankshop-live' });
    liveBox.addEventListener('change', () => setLive(liveBox.checked));
    lineEl = el('p', { class: 'clankshop-line', role: 'status' });
    selectedDetail=el('p',{class:'clankshop-selected','aria-live':'polite'});
    selectedOpen=el('button',{type:'button',class:'copal-btn',text:'Open chat',hidden:true});
    selectedOpen.addEventListener('click',()=>{
      const unit=model.units.find(item=>item.id===selectedUnitId);
      if (unit) void openUnit(unit).catch(()=>{ if (sendNote) sendNote.textContent='Could not open that chat.'; });
    });
    listEl = el('ul', { class: 'clankshop-list', 'aria-label': 'Clanks and what they are doing' });
    cardsEl = el('div', { 'aria-live': 'polite' });
    sendChat = el('select', { 'aria-label': 'Chat to open' });
    sendText = el('textarea', { placeholder: 'Message for the chat…', 'aria-label': 'Message text' });
    sendNote = el('p', { class: 'clankshop-line', role: 'status' });
    const sendBtn = el('button', { type: 'button', class: 'copal-btn', text: 'Open chat with this text' });
    sendBtn.addEventListener('click', () => { openChatWithText().catch(() => { sendNote.textContent = 'Could not open that chat.'; }); });
    controls = new Map();
    const options = el('div', {class:'clankshop-options'}), audioOptions=el('div',{class:'clankshop-options'});
    function setting(key, label, choices, kind='select', names={}) {
      let control;
      if (kind === 'select') { control=el('select',{'aria-label':label}); for (const value of choices) control.append(el('option',{value,text:names[value] || String(value)})); }
      else if (kind === 'checkbox') control=el('input',{type:'checkbox','aria-label':label});
      else control=el('input',{type:'range',min:choices[0],max:choices[1],step:choices[2],'aria-label':label});
      const localAudio=AUDIO_OPTIONS.has(key);
      controls.set(key,control);
      (localAudio ? audioOptions : options).append(el('label',{},label,' ',control));
      if (kind==='range' && key.endsWith('_volume')) control.addEventListener('input',()=>{
        if (!audioArmed || preview?.paused) return;
        audio?.configure({...audioPreferences(),[key]:Number(control.value),audio_enabled:true});
        mixerSettings=null;
      });
      control.addEventListener('change',()=>{
        const value=kind==='checkbox'?control.checked:kind==='range'?Number(control.value):control.value;
        if (preview && localAudio) {
          preview.audioSettings={...preview.audioSettings,[key]:value};
          draw();
        } else saveSettings({[key]:value}).catch(()=>draw());
      });
    }
    setting('pause_in_background','Pause in background',null,'checkbox');
    setting('motion','Motion',['system','reduced','full']); setting('intensity','Intensity',['quiet','normal','busy']);
    setting('drawn_actor_limit','Drawn actors',[1,16,1],'range'); setting('followed_chat_limit','Followed chats',[1,8,1],'range');
    setting('effects_budget','Effects budget',[0,32,1],'range');
    setting('master_volume','Master volume',[0,1,.05],'range');
    setting('music_volume','Music volume',[0,1,.05],'range'); setting('effects_volume','Effects volume',[0,1,.05],'range');
    setting('drift_style','Drift soundtrack',Object.keys(DRIFT_STYLES),'select',DRIFT_STYLES);
    setting('drift_meter','Drift time signature',['adaptive',...DRIFT_METERS],'select',{adaptive:'Adaptive to tool calls'});
    setting('drift_pickup_scale','Drift scale',Object.keys(DRIFT_PALETTES),'select',
      Object.fromEntries(Object.entries(DRIFT_PALETTES).map(([key,scale])=>[key,scale.name])));
    for (const [key,label] of Object.entries(CUE_LEVELS)) setting(key,label+' volume',[0,1,.05],'range');
    setting('background_audio','Background audio',['full','quarter','silent']); setting('music_enabled','Music',null,'checkbox');
    setting('hits_enabled','Action sounds',null,'checkbox'); setting('completion_cue','Completion cue',null,'checkbox');
    const advanced=el('details',{},el('summary',{text:'View settings'}),options);
    const soundSettings=el('details',{},el('summary',{text:'Audio settings'}),audioOptions);
    profileActor=el('select',{'aria-label':'Observed actor for profile'});
    profileTheme=el('select',{'aria-label':'Profile theme'},el('option',{value:'dark',text:'Dark'}),el('option',{value:'light',text:'Light'}));
    profileBadges=el('div',{class:'cs-badge-checks','aria-label':'Earned badges to include, up to three'});
    profilePreview=el('div',{class:'clankshop-profile-preview','aria-label':'Profile export preview'});
    profileActor.addEventListener('change',drawProfile); profileTheme.addEventListener('change',drawProfile);
    void loadProfileMascot();
    saveProfile=el('button',{type:'button',class:'copal-btn',text:'Save profile card',disabled:true});
    saveProfile.addEventListener('click',()=>{
      const account=model.account, stamp=scopeRevision, mounted=root, text=profileCard();
      const selected=model.units.find((unit)=>unit.live && profileActor && profileKey(unit)===profileActor.value);
      const chat=model.chats.find((item)=>item.id===selected?.chatId);
      const current=()=>!preview && root===mounted && mounted?.isConnected && model.account===account && scopeRevision===stamp && !!text && !!selected && !!chat && profileCard()===text && !!account;
      if (!current()) return;
      profilePicker?.destroy();
      const picker=createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:account,originWindow:win,
        getAccountScope:()=>model.account,isOriginCurrent:current,rootLabel:'Choose a folder for the profile card',
        isCompatible:(row)=>row.provider==='host' && (row.capabilities.create || row.capabilities.write) && row.revision?.kind==='hostDirectoryRevision' ? '' : 'Choose a writable Host folder with a current revision.',
        onSelect:async(row)=>{ if (!current()) return false;
          try {
            const fresh=await loadUnits(chat,abort?.signal);
            const matches=chatUnits(chat,{actors:fresh.actors,companion:fresh.companion,account}).filter((unit)=>unit.live && profileKey(unit)===profileKey(selected));
            if (!current() || matches.length!==1 || matches[0].name!==selected.name) { sendNote.textContent='The observed actor changed; refresh and preview again.'; return false; }
          } catch { if (current()) sendNote.textContent='The observed actor is unavailable; refresh and preview again.'; return false; }
          const operationId=`clank-profile-${crypto.randomUUID()}`;
          await filesFacadeClient.createResource(row.ref,{name:'clank-profile.svg',text,operationId,expectedParentRevision:row.revision});
          if (current()) sendNote.textContent='Profile card saved to the selected folder.'; return true; },
        onClose:()=>{ if (profilePicker===picker) profilePicker=null; }});
      profilePicker=picker; void picker.open();
    });
    earnedLine=el('p',{class:'clankshop-line'});
    galleryButton=el('button',{type:'button',class:'copal-btn',text:'Open achievements',hidden:true});
    galleryButton.addEventListener('click',async()=>{
      const current=root, account=model.account;
      const {openAppDestination}=await import('./markdownRenderer.js');
      if (root===current && model.account===account && !preview)
        await openAppDestination('clank://achievements',null,{sourceKind:'clankshop'});
    });
    const profile=el('details',{},el('summary',{text:'Profile card'}),profileActor,profileTheme,profileBadges,profilePreview,saveProfile);
    liveOnly=el('div',{class:'cs-live-controls'},earnedLine,galleryButton,cardsEl,advanced,profile,
      el('details',{},el('summary',{text:'Draft a chat message'}),el('div',{class:'clankshop-send'},sendChat,sendText,sendBtn)));
    const side=el('section',{class:'clankshop-side',tabIndex:0,'aria-label':'Clankshop controls and activity'},
      lineEl,audioStatus,soundSettings,playStatus,stationStatus,previewPanel,el('h3',{text:'Workers'}),
      listEl,selectedDetail,selectedOpen,sendNote,liveOnly);
    const visual=el('div',{class:'clankshop-visual'},stage,toy);
    const bar=el('div',{class:'clankshop-bar'},el('label',{},'View ',modeSelect),debugButton,previewTag,
      refreshBtn,el('label',{for:'clankshop-live'},liveBox,'Live'),audioButton);
    root.append(bar,el('div',{class:'clankshop-body'},visual,side));
    body.replaceChildren(root);
    draw();

    listen(document, 'openclank:auth-context-changed', () => {
      epoch++; scopeRevision++; abort?.abort(); profilePicker?.destroy(); profilePicker=null;
      permissionCards.clear(); preview=null; livePresentation=createPresentation(); clearSelection(); invalidateActivity(); badgeSelection.clear();
      stopAudio(); settings={...DEFAULT_SETTINGS}; model.permissions.clear(); model.achievements.clear();
      Object.assign(model, { account: '', live: false, loaded: false, units: [], chats: [], more: 0, failures: 0 });
      draw();
      loadPreference().then(()=>refresh()).then(syncTimer).catch(()=>{});
    });
    listen(window,'workspace-change',()=>{
      if (!root) return;
      epoch++; scopeRevision++; abort?.abort(); profilePicker?.destroy(); profilePicker=null;
      permissionCards.clear();
      stopAudio();
      preview=null; livePresentation=createPresentation(); invalidateActivity(); clearSelection();
      model.units=[]; model.permissions.clear(); model.achievements.clear(); badgeSelection.clear(); draw(); syncTimer();
      if (active()) void refresh().catch(()=>{});
    });
    listen(document,'odysseus:session-selected',(event)=>{
      if (preview) releasePreview();
      const id=event.detail?.sessionId;
      if (id && (incognitoIds().includes(id) || sessionModule.getSessions?.()?.some(chat=>chat.id===id && chat.incognito))) {
        stopAudio();
        syncAudio();
      }
    });
    const motionPref=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (motionPref?.addEventListener) listen(motionPref,'change',()=>draw());
    listen(window, 'focus', syncTimer); listen(window, 'blur', syncTimer); listen(document, 'visibilitychange', syncTimer);
    if (win?.root) {
      observer = new MutationObserver(syncTimer);
      observer.observe(win.root, { attributes:true, attributeFilter:['class','style'] });
    }
    listen(document, 'odysseus:permission-pending', (event) => {
      const { sessionId, payload } = event.detail || {};
      if (!preview && model.account && typeof sessionId === 'string' && payload && payload.request_id && !payload.sudo_password) { model.permissions.set(sessionId, payload); if (root) refresh().catch(() => {}); }
    });
    listen(document, 'odysseus:interactive-resolved', (event) => {
      const d = event.detail || {};
      if (d.kind === 'permission' && model.permissions.get(d.sessionId)?.request_id === d.requestId) { model.permissions.delete(d.sessionId); if (root) refresh().catch(() => {}); }
    });
    loadPreference().then(() => { if (root && model.account) acknowledgeVisible(root,'surface.visited',{surface:'clankshop',visited:true},{accountId:model.account}); return refresh(); }).then(syncTimer).catch(() => {});
    syncTimer();
  }

  function destroy() {
    epoch++; scopeRevision++; permissionCards.clear();
    abort?.abort(); abort = null;
    if (timer) { clearInterval(timer); timer = null; }
    stopAnimationFrame();
    arrival=0; lastObservation=null; wasActive=wasVisible=null;
    preview=null; livePresentation=createPresentation(); driftLayout={...DRIFT_DEFAULT}; fields.clear(); placed=[]; clearSelection();
    previewPanel=updatePreviewControls=debugButton=previewTag=selectedOpen=stationStatus=playStatus=liveOnly=refreshBtn=null;
    profileBadges=galleryButton=null; badgeSelection.clear();
    observer?.disconnect(); observer = null;
    profilePicker?.destroy(); profilePicker=null;
    profileActor=profileTheme=profilePreview=saveProfile=null; mascotData=null;
    audioAttempt=null; audioIssue=''; audioStatus=null; preferenceWrites=Promise.resolve();
    audioView=mixerSettings=mixerArmed=mixerScene=null;
    audio?.close(); audio = null; audioArmed = false;
    for (const off of bound) off();
    bound = [];
    root?.remove();
    root = win = stage = unitsLayer = listEl = lineEl = cardsEl = sendChat = sendText = sendNote = liveBox = toy = modeSelect = audioButton = earnedLine = selectedDetail = null;
    Object.assign(model, { account:'', live:false, loaded:false, units:[], more:0, failures:0, chats:[] });
    model.permissions.clear(); model.activity.clear(); model.usage.clear(); model.achievements.clear(); controls.clear();
  }

  return { render, destroy, refresh };
}
