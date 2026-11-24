import { DRIFT_TRACK, driftHarmony, driftMotif, driftPalette } from './driftHarmony.js';

// Each group is measured in eighth notes. The first pulse of each group
// receives a fresh drum accent, including the long last group in 7/8.
const METER_GROUPS = Object.freeze({
  '3/4':[2,2,2], '4/4':[2,2,2,2], '5/4':[2,2,2,2,2],
  '6/8':[3,3], '7/8':[2,2,3], '9/8':[3,3,3], '12/8':[3,3,3,3],
});
export const DRIFT_METERS = Object.keys(METER_GROUPS);
export const DRIFT_STYLES = Object.freeze({'rally-house':'Rally house',garage:'Garage',phonk:'Phonk'});

const activityMeters = ['3/4','6/8','4/4','7/8','5/4','9/8','12/8'];
const meterThresholds = [1.5,3,5,7,9,11];

export function driftMeter(mode, rate, current = '4/4') {
  if (Object.hasOwn(METER_GROUPS,mode)) return mode;
  const active = activityMeters.indexOf(current);
  const index = meterThresholds.reduce((count,threshold,i) =>
    count + Number(rate >= threshold + (i < active ? -.35 : .35)),0);
  return activityMeters[index];
}

export function driftStyle(key) {
  return Object.hasOwn(DRIFT_STYLES,key) ? key : 'rally-house';
}

const sliceSeconds = 30 / DRIFT_TRACK.bpm;

export function createDriftTransport(ctx, kits, outputs, choose, startBar = 0) {
  let nextBar=startBar, nextStart=ctx.currentTime+.055, meter='4/4', style;
  let bpm, palette, cycle=0;
  const queued=[], nodes=new Set();

  function play(buffer, output, start, duration, {rate=1,offset=0,volume=1,attack=.004,release=.016}={}) {
    const length=Math.min(duration,(buffer.duration-offset)/rate);
    if (length <= 0) return;
    const source=ctx.createBufferSource(), gain=ctx.createGain(), end=start+length;
    source.buffer=buffer;
    source.playbackRate.value=rate;
    source.connect(gain);
    gain.connect(output);
    gain.gain.setValueAtTime(0,start);
    gain.gain.linearRampToValueAtTime(volume,start+Math.min(attack,length/4));
    gain.gain.setValueAtTime(volume,end-Math.min(release,length/2));
    gain.gain.linearRampToValueAtTime(0,end);
    const entry={source,gain};
    nodes.add(entry);
    source.onended=()=>{
      nodes.delete(entry);
      source.disconnect();
      gain.disconnect();
    };
    source.start(start,offset,length*rate);
  }

  function queueBar() {
    const chosen=choose(meter,style);
    const bar=nextBar, start=nextStart;
    const previousMeter=meter;
    meter=chosen.meter;
    const targetPalette=driftPalette(chosen.palette);
    const changing=bar===0 && !!palette && targetPalette!==palette;
    if (bar===0 || !palette) palette=targetPalette;
    if (kits.has(chosen.style)) style=chosen.style;
    const {bed,layer,bass,doo}=kits.get(style);
    bpm=bpm===undefined ? chosen.bpm : bpm+Math.max(-3,Math.min(3,chosen.bpm-bpm));
    const groups=METER_GROUPS[meter];
    const slice=30/bpm, count=groups.reduce((sum,size)=>sum+size,0);
    const end=start+count*slice;
    queued.push({bar,meter,style,palette,bpm,changing,start,end,beatLength:meter.endsWith('/8') ? slice : 2*slice});
    const sliceCount=Math.round(bed.duration/sliceSeconds);
    const rate=sliceSeconds/slice;
    const {root,turn}=driftHarmony(palette,bar);
    const bassRate=semitones=>2**((semitones>=8 ? semitones-12 : semitones)/12);
    const pickup=!changing && root!==turn;
    let pulse=0;
    for (const [group,length] of groups.entries()) {
      const accent=start+pulse*slice;
      for (let step=0;step<length;step++,pulse++) {
        const sample=group===0 && step===0 ? 0 : (step===2 ? 3 : group*2+step)%sliceCount;
        const timing=start+pulse*slice;
        const volume=step ? .78 : group ? .9 : 1;
        const options={rate,offset:sample*sliceSeconds,volume};
        play(bed,outputs[0],timing,slice+.004,options);
        play(layer,outputs[1],timing,slice+.004,options);
      }
      const volume=group===0 ? .68 : style==='phonk' ? .57 : .48;
      const bassStart=accent+(style==='garage' && group%2 ? .16*slice : 0);
      const lastGroup=group===groups.length-1;
      const sustain=changing ? 1.25 : pickup && lastGroup ? length-1.1 : length*.91;
      play(bass,outputs[0],bassStart,slice*sustain,
        {rate:bassRate(root),volume,attack:.01,release:.064});
    }
    if (pickup) {
      play(bass,outputs[0],end-slice,slice*.82,
        {rate:bassRate(turn),volume:.32,attack:.01,release:.06});
    }
    // The shared E downbeat clears out the upper parts for one bar during a scale change.
    if (doo && !changing && previousMeter===meter && bar===2 && cycle%3===1) {
      for (const [index,note] of driftMotif(palette,bar).entries()) {
        play(doo,outputs[0],start+(count-4+index)*slice,slice*.88,
          {rate:2**((note-64)/12),volume:.28,attack:.009,release:.045});
      }
    }
    nextBar=(bar+1)%DRIFT_TRACK.chords.length;
    if (nextBar===0) cycle++;
    nextStart=end;
  }

  function schedule() {
    const time=ctx.currentTime;
    if (nextStart<time) nextStart=time+.012;
    while (queued.length>1 && queued[0].end<=time) queued.shift();
    while (nextStart<time+.12) queueBar();
  }

  function position(time=ctx.currentTime) {
    const item=queued.findLast(entry=>entry.start<=time) || queued[0];
    const beat=item ? Math.max(0,Math.min(item.end-item.start,time-item.start))/item.beatLength : 0;
    return {...item,bar:item?.bar ?? nextBar,meter:item?.meter ?? meter,beat,
      beatLength:item?.beatLength ?? 60/DRIFT_TRACK.bpm,origin:item?.start ?? nextStart};
  }

  schedule();
  const timer=setInterval(schedule,35);
  return {
    position,
    stop() {
      clearInterval(timer);
      const {bar}=position();
      for (const {source,gain} of nodes) {
        source.onended=null;
        try { source.stop(); } catch {}
        source.disconnect();
        gain.disconnect();
      }
      nodes.clear();
      queued.length=0;
      return bar;
    },
  };
}
