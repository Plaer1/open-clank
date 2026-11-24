// Shared reference tempo and the packaged Drift Arcade fallback progression.
export const DRIFT_TRACK = {
  asset:'drift-arcade', bpm:120, beatsPerBar:4, key:'E major',
  scale:[4,6,8,9,11,1,3],
  pentatonic:[4,6,8,11,1],
  chords:[
    {name:'E', pitches:[4,8,11]},
    {name:'A', pitches:[9,1,4]},
    {name:'C♯m', pitches:[1,4,8]},
    {name:'B', pitches:[11,3,6]},
  ],
};

// The nominal landing notes of the four prerecorded voice clips.
export const DRIFT_VOICE_NOTES = {chip:65,coil:69,cell:69,star:75,sustain:69};
const pickupOrder = Object.keys(DRIFT_VOICE_NOTES);

export function driftVoiceNote(variant, {bar,beat}, sequence, previous = null, palette = 'major', live = false) {
  const legacyChord = DRIFT_TRACK.chords[bar];
  const harmony = driftHarmony(palette,bar);
  const chord = live || !['major','pentatonic'].includes(palette)
    ? {name:legacyChord.name,pitches:harmony.chord} : legacyChord;
  const original = DRIFT_VOICE_NOTES[variant];
  const scale = harmony.pitches;
  const pitches = [];
  for (let midi=original-4; midi<=original+4; midi++) {
    if (scale.includes(midi%12)) pitches.push(midi);
  }
  const strong = variant === 'sustain' || beat < .5;
  const chordTones = pitches.filter(note => chord.pitches.includes(note%12));
  const pool = strong && chordTones.length ? chordTones : pitches;
  if (previous == null) {
    return {midi:pool[(sequence+pickupOrder.indexOf(variant))%pool.length],chord:chord.name,original};
  }
  const direction = sequence%4 < 2 ? 1 : -1;
  const neighbors = pool.filter(note => note !== previous && Math.abs(note-previous)<=4);
  const eligible = neighbors.length ? neighbors : pool;
  const fit = note => Math.abs(note-previous) + (note===previous ? 2.4 : 0)
    + (Math.sign(note-previous)!==direction ? .7 : 0) + Math.abs(note-original)*.12;
  const midi = eligible.reduce((best,note) => fit(note)<fit(best) ? note : best);
  return {midi,chord:chord.name,original};
}

// Match the baked chord progression when playing the straight 4/4 fallback.
export function driftPickupNote(variant, clock, duration, sequence, previous = null, palette = 'major') {
  const secondsPerBeat = 60 / DRIFT_TRACK.bpm;
  const beats = ((clock % duration) + duration) % duration / secondsPerBeat;
  const bar = Math.min(DRIFT_TRACK.chords.length-1,Math.floor(beats/DRIFT_TRACK.beatsPerBar));
  return driftVoiceNote(variant,{bar,beat:beats%DRIFT_TRACK.beatsPerBar},sequence,previous,palette);
}

// Each palette supplies its own bar roots and melodic notes. Every phrase
// begins on E and leads back to E, so a setting change can share that landing.
export const DRIFT_PALETTES = Object.freeze({
  major: {name:'E major', tones:[0,2,4,5,7,9,11], roots:[0,5,9,7], turn:11},
  pentatonic: {name:'E major pentatonic', tones:[0,2,4,7,9], roots:[0,9,4,7], turn:9},
  'minor-pentatonic': {name:'E minor pentatonic', tones:[0,3,5,7,10], roots:[0,10,5,7], turn:3},
  'natural-minor': {name:'E natural minor', tones:[0,2,3,5,7,8,10], roots:[0,8,10,7], turn:8},
  dorian: {name:'E Dorian', tones:[0,2,3,5,7,9,10], roots:[0,5,9,10], turn:9},
  phrygian: {name:'E Phrygian', tones:[0,1,3,5,7,8,10], roots:[0,1,8,7], turn:1},
  lydian: {name:'E Lydian', tones:[0,2,4,6,7,9,11], roots:[0,2,6,7], turn:6},
  mixolydian: {name:'E Mixolydian', tones:[0,2,4,5,7,9,10], roots:[0,10,5,7], turn:10},
  'harmonic-minor': {name:'E harmonic minor', tones:[0,2,3,5,7,8,11], roots:[0,8,5,7], turn:11},
  'melodic-minor': {name:'E melodic minor', tones:[0,2,3,5,7,9,11], roots:[0,9,5,7], turn:2},
});

export function driftPalette(key) {
  return Object.hasOwn(DRIFT_PALETTES,key) ? key : 'major';
}

export function driftHarmony(palette, bar) {
  const {tones,roots,turn}=DRIFT_PALETTES[driftPalette(palette)];
  const root=roots[bar % roots.length];
  const degree=tones.indexOf(root);
  const pitches=tones.map(semitones => (4+semitones)%12);
  const chord=[0,2,4].map(step => pitches[(degree+step)%pitches.length]);
  return {root,tones,pitches,chord,turn};
}

export function driftMotif(palette, bar) {
  const {root,tones}=driftHarmony(palette,bar);
  const degree=tones.indexOf(root);
  // The pickup, upper note and landing articulate a short rising/falling vocal phrase.
  return [1,3,2,0].map(step => {
    const index=degree+step;
    return 64+tones[index%tones.length]+(index>=tones.length ? 12 : 0)-(degree>=3 ? 12 : 0);
  });
}
