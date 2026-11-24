// This mixer plays the packaged loops and cues through one gesture-opened context.
import { DRIFT_TRACK, DRIFT_VOICE_NOTES, driftPickupNote, driftVoiceNote } from './driftHarmony.js';
import { createDriftTransport, driftMeter, driftStyle } from './driftTransport.js';

const SCENES = {
  workshop:{bpm:84,tempoRange:12,musicLevel:.54,cues:['quiet-clang','recharge-buzz'],legacyCue:'workshop-light'},
  drift:{bpm:DRIFT_TRACK.bpm, musicLevel:1, cues:['drift-action','drift-screech','drift-voice-sustain-stack','drift-voice-chip','drift-voice-coil','drift-voice-cell','drift-voice-star','dodge']},
  idle:{bpm:66,tempoRange:8,musicLevel:.46,cues:['quiet-clang','recharge-buzz'],legacyCue:'idle-action'},
};
const ACTIONS = new Set(['stamp','tool','recharge','pickup','dodge','drift']);
const SIGNALS = new Set(['completion','failure','question','permission']);
export const CLANKSHOP_CUE_VOLUMES = {
  pickup:'pickup_volume', drift:'drift_volume', dodge:'dodge_volume', stamp:'tool_volume', tool:'tool_volume', recharge:'tool_volume',
  completion:'alert_volume', failure:'alert_volume', question:'alert_volume', permission:'alert_volume',
};
const quietAfter = 45_000, stopAfter = 240_000;
const soundPath = '/static/audio/clankshop/';
const maxVoices = 10;

export function createClankshopAudio({
  Context = globalThis.AudioContext || globalThis.webkitAudioContext,
  now = () => performance.now(),
  fetchAsset = (...args) => globalThis.fetch(...args),
  onTiming = () => {},
} = {}) {
  let ctx, master, music, effects, limiter, sourceScope, timer;
  let prefs={}, scene='workshop', visible=true, hidden=false, admitted=false;
  let last=null, activity=0, activityRate=0, beatOrigin=0, offset=0, playing=null;
  let actionTotal=0, nextClang=0, musicClang=null;
  let driftRetryAt=-Infinity;
  let driftBar=0, pickupSequence=0, previousPickup=null, lastPickupAt=-Infinity;
  const cueBuses = new Map(), driftKits=new Map(), kitRequests=new Map();
  let requested=null, failedRequest=null, ready=null, enableVersion=0, effectVersion=0;
  const clips=new Map(), failedAt=new Map(), primed=new Set(), voices=new Set(), lastHits=new Map();

  const beat = () => 60 / SCENES[scene].bpm;
  const tempoProgress = () => 1-Math.exp(-actionTotal/64);
  const driftBpm = () => 104+44*tempoProgress();
  const loopPosition = (track,time) => track.fileOffset+Math.max(0,time-track.positionAt)*track.rate;
  const loopBeat = () => beat()/(playing?.rate ?? 1);
  const setting = (key,fallback) => {
    const value=Number(prefs[key] ?? fallback);
    return Number.isFinite(value) ? Math.max(0,Math.min(1,value)) : fallback;
  };
  const gain = () => admitted && prefs.audio_enabled && !hidden
    ? setting('master_volume',.15)*(visible || prefs.background_audio==='full' ? 1 : prefs.background_audio==='quarter' ? .25 : 0) : 0;
  const age = () => last===null ? Infinity : now()-last;
  const musicAllowed = () => gain()>0 && prefs.music_enabled && ctx?.state==='running' && age()<stopAfter;

  function apply() {
    if (!ctx) return;
    const t=ctx.currentTime, quiet=age();
    const fade=quiet<=quietAfter ? 1 : Math.max(0,(stopAfter-quiet)/(stopAfter-quietAfter));
    master.gain.setTargetAtTime(gain(),t,.025);
    music.gain.setTargetAtTime(prefs.music_enabled ? setting('music_volume',.55)*SCENES[scene].musicLevel*fade : 0,t,.16);
    effects.gain.setTargetAtTime(setting('effects_volume',.8),t,.025);
    playing?.activityGain.gain.setTargetAtTime(activity*setting('activity_volume',1),t,.27);
    for (const [key,bus] of cueBuses) bus.gain.setTargetAtTime(setting(key,1),t,.025);
  }

  async function decode(opened, scope, key, loop) {
    const maxBytes=loop ? 2*1024*1024 : 256*1024;
    const signal=typeof AbortSignal.any==='function' && typeof AbortSignal.timeout==='function'
      ? AbortSignal.any([scope.signal,AbortSignal.timeout(8_000)]) : scope.signal;
    const response=await fetchAsset(`${soundPath}${key}.wav`,{credentials:'same-origin',signal});
    if (!response.ok || Number(response.headers?.get('content-length'))>maxBytes) throw new Error('Clankshop track unavailable');
    const body=await response.arrayBuffer();
    if (body.byteLength>maxBytes || opened!==ctx || scope.signal.aborted) throw new Error('Clankshop track unavailable');
    const buffer=await opened.decodeAudioData(body);
    if (opened!==ctx || scope.signal.aborted || buffer.duration<=0 || buffer.duration>(loop ? 30 : 2)
        || buffer.numberOfChannels!==1) throw new Error('Clankshop track unavailable');
    return buffer;
  }

  function load(key, loop=false) {
    const hit=clips.get(key);
    if (hit) return hit;
    if (!ctx || !admitted || now()-(failedAt.get(key) ?? -Infinity)<5_000) return Promise.reject(new Error('Clankshop audio unavailable'));
    const opened=ctx, scope=sourceScope;
    const pending=decode(opened,scope,key,loop).catch(error=>{
      if (clips.get(key)===pending) {
        clips.delete(key);
        failedAt.set(key,now());
      }
      throw error;
    });
    clips.set(key,pending);
    return pending;
  }

  function loop(name) {
    return Promise.all([load(`${name}-bed`,true),load(`${name}-activity`,true)]).then(pair=>{
      const [bed,layer]=pair;
      if (bed.length!==layer.length || bed.sampleRate!==layer.sampleRate) throw new Error('Clankshop music layers are not aligned');
      return pair;
    });
  }

  function prepareKit(style) {
    if (driftKits.has(style)) return Promise.resolve();
    if (kitRequests.has(style)) return kitRequests.get(style);
    const opened=ctx;
    const pending=Promise.all([loop(style),load(`${style}-bass`),load('drift-doo').catch(()=>null)])
      .then(([[bed,layer],bass,doo])=>{
        if (opened!==ctx) return;
        const eighth=30/DRIFT_TRACK.bpm;
        if (bed.duration<8*eighth-.002 || Math.abs(bed.duration/eighth-Math.round(bed.duration/eighth))>.008) {
          throw new Error('Drift percussion must line up with the eighth-note grid');
        }
        driftKits.set(style,{bed,layer,bass,doo});
      }).finally(()=>{
        if (kitRequests.get(style)===pending) kitRequests.delete(style);
      });
    kitRequests.set(style,pending);
    return pending;
  }

  function resetWork() {
    actionTotal=0;
  }

  function stopMusic(reset=false) {
    if (timer!=null) clearInterval(timer);
    timer=null;
    if (musicClang) {
      try { musicClang.stop(); } catch {}
      musicClang=null;
    }
    if (playing) {
      const current=playing, {bed,layer,activityGain,duration,transport}=current;
      playing=null;
      if (transport) driftBar=transport.stop();
      else {
        offset=loopPosition(current,ctx.currentTime)%duration;
        for (const node of [bed,layer]) {
          try { node.stop(); } catch {}
          node.disconnect();
        }
      }
      activityGain.disconnect();
      if (scene==='drift') onTiming();
    }
    if (reset) { offset=0; driftBar=0; pickupSequence=0; previousPickup=null; lastPickupAt=-Infinity; }
  }

  function stopEffects() {
    effectVersion++;
    lastHits.clear();
    for (const source of voices) {
      try { source.stop(); } catch {}
      source.disconnect();
    }
    voices.clear();
  }

  function watchMusic() {
    timer=setInterval(()=>{
      if (!musicAllowed()) stopMusic(age()>=stopAfter);
      else {
        apply();
        if (!playing.transport) updateLoopTempo();
        if (scene==='drift') {
          onTiming(driftTiming());
          if (now()>=driftRetryAt) prepare();
        } else if (ctx.currentTime>=nextClang) scheduleQuietClang();
      }
    },1000);
  }

  function targetLoopRate() {
    if (scene==='drift') return driftBpm()/DRIFT_TRACK.bpm;
    const {bpm,tempoRange}=SCENES[scene];
    return 1+(tempoRange/bpm)*tempoProgress();
  }

  function updateLoopTempo() {
    const track=playing, time=ctx.currentTime;
    const delta=targetLoopRate()-track.rate;
    const rate=track.rate+Math.max(-.023,Math.min(.023,delta));
    if (Math.abs(rate-track.rate)<.001) return;
    track.fileOffset=loopPosition(track,time);
    track.positionAt=time;
    track.rate=rate;
    track.bed.playbackRate.setValueAtTime(rate,time);
    track.layer.playbackRate.setValueAtTime(rate,time);
  }

  function loopOrigin(time) {
    return playing ? time-loopPosition(playing,time)/playing.rate : beatOrigin;
  }

  function scheduleQuietClang() {
    const opened=ctx, current=playing, spacing=loopBeat();
    const origin=loopOrigin(opened.currentTime);
    const start=origin+Math.ceil((opened.currentTime+.025-origin)/spacing)*spacing;
    nextClang=start+spacing*(scene==='idle' ? 19 : 13);
    void load('quiet-clang').then(buffer=>{
      if (ctx!==opened || playing!==current || !musicAllowed()) return;
      const cue=opened.createBufferSource(), level=opened.createGain();
      cue.buffer=buffer;
      level.gain.value=.16;
      cue.connect(level);
      level.connect(music);
      musicClang=cue;
      cue.onended=()=>{
        if (musicClang===cue) musicClang=null;
        cue.disconnect();
        level.disconnect();
      };
      cue.start(Math.max(start,opened.currentTime+.008));
    }).catch(()=>{});
  }

  function driftTiming() {
    if (!playing || scene!=='drift') return null;
    return playing.transport?.position() ?? {meter:'4/4',bpm:DRIFT_TRACK.bpm*playing.rate};
  }

  function startDriftMusic() {
    if (playing || !musicAllowed()) return;
    const activityGain=ctx.createGain();
    activityGain.gain.value=0;
    activityGain.connect(music);
    const transport=createDriftTransport(ctx,driftKits,[music,activityGain],(meter,currentStyle)=>{
      const style=driftStyle(prefs.drift_style);
      return {meter:driftMeter(prefs.drift_meter,activityRate,meter), bpm:driftBpm(),
        palette:prefs.drift_pickup_scale, style:driftKits.has(style) ? style : currentStyle ?? driftKits.keys().next().value};
    },driftBar);
    playing={activityGain,transport};
    onTiming(transport.position());
    apply();
    watchMusic();
  }

  function startMusic(bedClip, layerClip) {
    if (playing || !musicAllowed()) return;
    const bed=ctx.createBufferSource(), layer=ctx.createBufferSource(), activityGain=ctx.createGain();
    bed.buffer=bedClip; layer.buffer=layerClip;
    bed.loop=layer.loop=true;
    activityGain.gain.value=0;
    bed.connect(music);
    layer.connect(activityGain);
    activityGain.connect(music);
    const start=ctx.currentTime+.012;
    const duration=bedClip.duration, rate=targetLoopRate();
    offset%=duration;
    bed.playbackRate.value=layer.playbackRate.value=rate;
    beatOrigin=start-offset/rate;
    nextClang=start+beat()*9/rate;
    playing={bed,layer,activityGain,duration,fileOffset:offset,positionAt:start,rate};
    apply();
    bed.start(start,offset);
    layer.start(start,offset);
    if (scene==='drift') onTiming(driftTiming());
    watchMusic();
  }

  function settleEnable(ok) {
    const resolve=ready;
    ready=null;
    resolve?.(ok);
  }

  function prime() {
    if (!admitted || !prefs.hits_enabled || primed.has(scene)) return;
    primed.add(scene);
    for (const asset of SCENES[scene].cues) void load(asset).catch(()=>{});
  }

  function prepare() {
    if (!admitted || !prefs.audio_enabled) return;
    const wantsMusic=prefs.music_enabled && (ready || musicAllowed());
    const style=driftStyle(prefs.drift_style);
    if (wantsMusic && playing) {
      if (scene==='drift' && (!playing.transport || !driftKits.has(style)) && now()>=driftRetryAt) {
        driftRetryAt=now()+5_000;
        const current=playing;
        void prepareKit(style).then(()=>{
          if (playing!==current || current.transport || driftStyle(prefs.drift_style)!==style || !musicAllowed()) return;
          stopMusic();
          startDriftMusic();
        }).catch(()=>{});
      }
      settleEnable(true);
      prime();
      return;
    }
    if (!wantsMusic && !ready) return;
    const asset=prefs.hits_enabled ? SCENES[scene].cues[0] : prefs.completion_cue ? 'completion' : null;
    if (!wantsMusic && !asset) { settleEnable(true); return; }
    const drift=wantsMusic && scene==='drift';
    const key=drift ? `music:drift:${style}` : wantsMusic ? `music:${scene}` : `effect:${asset}`;
    if (requested?.key===key) return;
    if (failedRequest===key) { settleEnable(false); return; }
    const request={key,scene,ctx};
    requested=request;
    const fallbackCue=SCENES[scene].legacyCue;
    const buffers=drift ? prepareKit(style).then(()=>null).catch(()=>{
      driftRetryAt=now()+5_000;
      return loop(DRIFT_TRACK.asset);
    }) : wantsMusic ? loop(scene+'-quiet').catch(()=>loop(scene))
      : load(asset).catch(error=>fallbackCue && asset===SCENES[scene].cues[0] ? load(fallbackCue) : Promise.reject(error));
    const current=()=>requested===request && scene===request.scene && ctx===request.ctx && prefs.audio_enabled;
    void buffers.then(result=>{
      if (!current()) return;
      if (drift && !result) startDriftMusic();
      else if (wantsMusic) startMusic(...result);
      requested=null;
      settleEnable(true);
      prime();
    }).catch(()=>{
      if (!current()) return;
      requested=null;
      failedRequest=key;
      settleEnable(false);
    });
  }

  function wake() {
    last=now();
    apply();
    prepare();
  }

  function playDriftLayer(buffer,output,start,length,volume,rate=1) {
    const source=ctx.createBufferSource(), edge=ctx.createGain();
    const end=start+Math.min(length,buffer.duration/rate);
    source.playbackRate.value=rate;
    source.buffer=buffer;
    source.connect(edge);
    edge.connect(output);
    edge.gain.setValueAtTime(volume,start);
    edge.gain.setValueAtTime(volume,Math.max(start,end-.12));
    edge.gain.linearRampToValueAtTime(0,end);
    voices.add(source);
    source.onended=()=>{ if (voices.delete(source)) source.disconnect(); edge.disconnect(); };
    source.start(start);
    source.stop(end);
  }

  function hit({kind='stamp',variation,duration,replay=false,attention=false}={}) {
    const action=ACTIONS.has(kind), channel=CLANKSHOP_CUE_VOLUMES[kind];
    if (setting(channel,1)===0 || replay || (!action && !SIGNALS.has(kind)) || !gain() || ctx?.state!=='running'
        || (action && !attention ? !prefs.hits_enabled : !prefs.completion_cue)) return;
    if (scene==='drift' && (kind==='tool' || kind==='recharge')) return;
    const quietScene=scene!=='drift';
    const clang=quietScene && (kind==='tool' || kind==='stamp');
    const gate=clang ? 'quiet-clang' : kind;
    const gap=clang ? 1450 : kind==='recharge' ? 950 : action ? 95 : 180;
    const received=now();
    if (received-(lastHits.get(gate) ?? -Infinity)<gap) return;
    lastHits.set(gate,received);
    wake();
    const opened=ctx, version=effectVersion;
    const pickup=scene==='drift' && kind==='pickup'
      ? (Object.hasOwn(DRIFT_VOICE_NOTES,variation) && variation!=='sustain' ? variation : 'chip') : null;
    const key=pickup ? 'drift-voice-'+pickup : kind==='drift' ? 'drift-screech'
      : clang ? 'quiet-clang' : kind==='recharge' ? 'recharge-buzz' : kind==='stamp' ? 'drift-action' : kind;
    const fallback=clang ? SCENES[scene].legacyCue : null;
    const clip=fallback ? load(key).catch(()=>load(fallback)) : load(key);
    const sustained=kind==='drift' && Number.isFinite(duration);
    const vocal=sustained ? load('drift-voice-sustain-stack').catch(()=>null) : null;
    void Promise.all([clip.catch(()=>null),vocal]).then(([buffer,voice])=>{
      if (!buffer && !voice) return;
      if (opened!==ctx || version!==effectVersion || now()-received>220 || voices.size>=maxVoices || !gain()
          || opened.state!=='running' || (action && !attention ? !prefs.hits_enabled : !prefs.completion_cue)) return;
      const source=opened.createBufferSource();
      source.buffer=buffer ?? voice;
      const edge=kind==='drift' || clang || kind==='recharge' ? opened.createGain() : null;
      if (edge) edge.gain.value=clang ? (kind==='stamp' ? .34 : .19) : kind==='recharge' ? .24 : .42;
      const output=cueBuses.get(channel) || effects;
      source.connect(edge || output);
      edge?.connect(output);
      voices.add(source);
      source.onended=()=>{ if (voices.delete(source)) source.disconnect(); edge?.disconnect(); };
      const time=opened.currentTime;
      const position=playing?.transport?.position(time);
      const tick=(position?.beatLength ?? loopBeat())/8;
      const origin=position?.origin ?? loopOrigin(time);
      const delay=playing ? (tick-((time-origin)%tick+tick)%tick)%tick : 0;
      const start=time+.008+Math.min(.06,delay);
      if (pickup) {
        const position=playing?.transport?.position(start);
        const previous=received-lastPickupAt<2000 ? previousPickup : null;
        const sequence=pickupSequence++, palette=prefs.drift_pickup_scale;
        const {midi,original}=position || !playing
          ? driftVoiceNote(pickup,position ?? {bar:driftBar,beat:0},sequence,previous,position?.palette ?? palette,true)
          : driftPickupNote(pickup,loopPosition(playing,start),playing.duration,sequence,previous,palette);
        previousPickup=midi; lastPickupAt=received;
        source.playbackRate.value=2**((midi-original)/12);
      }
      source.start(start);
      if (kind==='drift') {
        const bass=driftKits.get(position?.style ?? driftStyle(prefs.drift_style))?.bass;
        if (voice) {
          const palette=position?.palette ?? prefs.drift_pickup_scale;
          const note=playing && !position
            ? driftPickupNote('sustain',loopPosition(playing,start),playing.duration,pickupSequence,previousPickup,palette)
            : driftVoiceNote('sustain',position ?? {bar:driftBar,beat:0},pickupSequence,previousPickup,palette,true);
          const rate=2**((note.midi-note.original)/12);
          if (buffer && voices.size<maxVoices) playDriftLayer(voice,output,start,duration,.23,rate);
          else if (!buffer) source.playbackRate.value=rate;
        }
        if (bass && voices.size<maxVoices) playDriftLayer(bass,output,start,.22,.26);
      }
      if (sustained) {
        const end=start+Math.max(.01,Math.min(duration+.16,source.buffer.duration/source.playbackRate.value,1.5));
        edge.gain.setValueAtTime(.42,Math.max(start,end-.13));
        edge.gain.linearRampToValueAtTime(0,end);
        source.stop(end);
      }
    }).catch(()=>{});
  }

  return {
    async enable(value) {
      prefs=value;
      if (!prefs.audio_enabled || !Context) return false;
      const version=++enableVersion;
      settleEnable(false);
      if (!ctx) {
        ctx=new Context();
        sourceScope=new AbortController();
        master=ctx.createGain(); music=ctx.createGain(); effects=ctx.createGain();
        master.gain.value=0;
        limiter=ctx.createDynamicsCompressor();
        limiter.threshold.value=-12; limiter.knee.value=3; limiter.ratio.value=20;
        limiter.attack.value=.003; limiter.release.value=.14;
        music.connect(master); effects.connect(master); master.connect(limiter); limiter.connect(ctx.destination);
        for (const key of new Set(Object.values(CLANKSHOP_CUE_VOLUMES))) {
          const bus=ctx.createGain();
          bus.gain.value=setting(key,1);
          bus.connect(effects);
          cueBuses.set(key,bus);
        }
      }
      const opened=ctx;
      await opened.resume();
      if (ctx!==opened || version!==enableVersion || opened.state!=='running' || !prefs.audio_enabled) return false;
      admitted=true;
      failedRequest=null;
      failedAt.clear();
      return new Promise(resolve=>{
        ready=resolve;
        wake();
        if (hidden) void opened.suspend().catch(()=>{});
      });
    },
    configure(value) {
      const musicEnabled=!prefs.music_enabled && value.music_enabled;
      if (prefs.drift_style!==value.drift_style) driftRetryAt=-Infinity;
      prefs=value;
      if (!gain()) { stopMusic(); stopEffects(); }
      else if (!prefs.music_enabled) stopMusic();
      if (!prefs.audio_enabled) {
        requested=null; failedRequest=null;
        enableVersion++;
        settleEnable(false);
      } else if (musicEnabled && admitted) {
        failedRequest=null;
        wake();
      } else prepare();
      apply();
    },
    setScene(mode) {
      if (!Object.hasOwn(SCENES,mode) || mode===scene) return;
      stopMusic(true); stopEffects();
      scene=mode; activityRate=activity=0;
      driftRetryAt=-Infinity;
      requested=null; failedRequest=null;
      if (admitted) wake();
    },
    resetWork,
    setActivity(rate,total=actionTotal) {
      const newActions=Number.isSafeInteger(total) && total>actionTotal;
      if (Number.isSafeInteger(total) && total>=0) actionTotal=total;
      activityRate=Math.max(0,Number(rate)||0);
      activity=Math.min(1,activityRate/12);
      if ((activity>0 || newActions) && admitted) wake();
      else apply();
    },
    beatPhase() {
      const position=playing?.transport?.position();
      const phase=position?.beat ?? (playing ? loopPosition(playing,ctx.currentTime) : now()/1000)/beat();
      return (phase%1+1)%1;
    },
    driftMeter:() => driftTiming()?.meter ?? null,
    driftBpm:() => driftTiming()?.bpm ?? null,
    hit,
    async visibility(isVisible,isHidden=false) {
      if (visible===isVisible && hidden===isHidden) return;
      visible=isVisible; hidden=isHidden;
      stopEffects();
      if (!gain()) stopMusic();
      apply();
      const opened=ctx;
      if (!opened) return;
      if (hidden) await opened.suspend();
      else if (prefs.audio_enabled && admitted) {
        await opened.resume();
        if (ctx===opened && !hidden) prepare();
      }
    },
    close() {
      stopMusic(true); stopEffects();
      requested=null; sourceScope?.abort();
      clips.clear(); failedAt.clear(); primed.clear(); driftKits.clear(); kitRequests.clear();
      resetWork();
      for (const node of [...cueBuses.values(),music,effects,master,limiter]) node?.disconnect();
      cueBuses.clear();
      const opened=ctx;
      ctx=master=music=effects=limiter=sourceScope=null;
      admitted=false;
      last=null;
      settleEnable(false);
      if (opened) void opened.close().catch(()=>{});
    },
  };
}
