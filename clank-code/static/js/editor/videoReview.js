// Read-only Files media projection; the player never authorizes an action.
export function normalizeRecordingCoverage(value, jobID) {
  if (!value || value.version !== 1 || !/^[a-f0-9]{32}$/.test(jobID || '') || value.jobID !== jobID || value.inputEvents !== 'unavailable') return null;
  const {video,audio}=value;
  const count=n=>Number.isSafeInteger(n) && n >= 0;
  const size=video?.outputPixels;
  if (video?.state !== 'observed' || video.upstreamLoss !== 'unknown' || !count(video.encodedBuffers) ||
      !count(video.missingTimestampBuffers) || video.missingTimestampBuffers > video.encodedBuffers ||
      !(size === null || Array.isArray(size) && size.length === 2 && size.every(n=>count(n) && n > 0))) return null;
  if (!['observed','not_selected','unavailable'].includes(audio?.state) || audio.physicalSync !== 'unknown' || !Array.isArray(audio.tracks) ||
      audio.tracks.length > 2 || (audio.state === 'observed') !== Boolean(audio.tracks.length) ||
      audio.tracks.some(track=>!['microphone','system_output'].includes(track?.kind) || !count(track.submittedFrames)) ||
      new Set(audio.tracks.map(track=>track.kind)).size !== audio.tracks.length) return null;
  return {
    video:{size:size && [...size],encodedBuffers:video.encodedBuffers,missingTimestampBuffers:video.missingTimestampBuffers},
    audio:{state:audio.state,tracks:audio.tracks.map(({kind,submittedFrames})=>({kind,submittedFrames}))},
  };
}

function coverageDetails(coverage) {
  const details=document.createElement('details'), summary=document.createElement('summary');
  summary.textContent='Capture evidence'; details.append(summary);
  const line=text=>{const row=document.createElement('p');row.textContent=text;details.append(row);};
  if (!coverage) { line('Capture evidence is unavailable for this saved revision.'); return details; }
  const {video,audio}=coverage;
  line(video.size ? `Recorded output: ${video.size[0]} × ${video.size[1]} pixels.` : 'Recorded output size unavailable.');
  line(`Encoded video: ${video.encodedBuffers} buffers observed, ${video.missingTimestampBuffers} without timestamps. Upstream frame loss is unknown.`);
  if (audio.state === 'observed') {
    for (const track of audio.tracks) line(`${track.kind === 'microphone' ? 'Microphone' : 'System output'}: ${track.submittedFrames} audio frames submitted.`);
  } else line(audio.state === 'not_selected' ? 'Audio was not selected.' : 'Audio evidence is unavailable.');
  line('Physical audio/video sync is unknown. Input events are unavailable.');
  return details;
}

export function mountVideoReview(root,{url,name,resourceRef,isCurrent,recordingCoverage=null,nativeRecording=false,onFeedback,onSaveFrame,onCheckFrame,hasPendingFrame,onTrim,onCheckTrim,onTeach,hasPendingTrim=()=>false,onExport,onCheckExport,hasPendingExport=()=>false}) {
  const media=document.createElement('video'), controls=document.createElement('div'), status=document.createElement('p'), listeners=new AbortController();
  media.controls=true;media.preload='metadata';media.src=url;media.setAttribute('aria-label',`Recording: ${name}`);media.style.maxWidth='100%';status.setAttribute('role','status');
  const button=(label)=>{const node=document.createElement('button');node.type='button';node.textContent=label;return node;};
  const start=button('Mark start'),end=button('Mark end'),annotate=button('Annotate selection…'),explain=button('Explain selection…'),saveFrame=button('Save displayed frame…'),checkFrame=button('Check frame save status');
  const trim=button('Trim selected range…'),checkTrim=button('Check / save trim'),teach=button('Teach from selection…');
  const exportBundle=button('Export selected feedback…'),checkExport=button('Check export status');
  const menu=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Recording actions';menu.append(summary,start,end,annotate,explain,teach,saveFrame,checkFrame,trim,checkTrim,exportBundle,checkExport);checkExport.hidden=!hasPendingExport();checkFrame.hidden=!hasPendingFrame();checkTrim.hidden=!hasPendingTrim();
  let disposed=false,selection=null;
  const valid=()=>!disposed && isCurrent() && media.isConnected;
  const point=()=>{const time=media.currentTime,duration=media.duration;if(!valid()||media.readyState<2||media.seeking||!Number.isFinite(time)||time<0||time>86400||(Number.isFinite(duration)&&(duration<=0||time>duration)))throw Error('Playback position is unavailable.');return Math.round(time*1000);};
  const show=()=>{status.textContent=selection?.kind==='time_range' ? `Selected ${selection.startMs}–${selection.endMs} ms · playback hint, unverified` : selection?.kind==='start' ? `Start ${selection.startMs} ms · choose an end` : `${Number.isFinite(media.duration) ? '' : 'Duration unavailable. '}Select a time or mark a range. Playback position is unverified.`;};
  start.onclick=()=>{try{selection={kind:'start',startMs:point()};show();}catch(error){status.textContent=error.message;}};
  end.onclick=()=>{try{const time=point();if(selection?.kind!=='start'||time<=selection.startMs)throw Error('Choose an end after the start.');selection={kind:'time_range',startMs:selection.startMs,endMs:time};show();}catch(error){status.textContent=error.message;}};
  async function request(explaining){try{const anchor=selection?.kind==='time_range'?{...selection}:{kind:'frame',timeMs:point()};const current=()=>valid();await onFeedback(anchor,current,explaining);if(current())status.textContent='Feedback draft opened. Review, save and send separately.';}catch(error){if(valid())status.textContent=error.message || 'Feedback is unavailable.';}}
  annotate.onclick=()=>void request(false);explain.onclick=()=>void request(true);
  teach.onclick=async()=>{try {const anchor=selection?.kind==='time_range'?{...selection}:{kind:'frame',timeMs:point()};
    await onTeach(Object.freeze(anchor),()=>valid());if(valid())status.textContent='Review the human-authored skill draft before saving.';
  }catch(error){if(valid())status.textContent=error.message || 'Teach is unavailable.';}};
  saveFrame.onclick=async()=>{try {
    if(hasPendingFrame())throw Error('Check the previous frame save status first.');
    media.pause();const timeMs=point();
    const current=()=>valid();
    const width=media.videoWidth,height=media.videoHeight;if(!width||!height||width>4096||height>4096||width*height>16000000)throw Error('Displayed frame dimensions are unavailable or too large.');
    const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const ctx=canvas.getContext('2d');if(!ctx)throw Error('Frame copy is unavailable.');ctx.drawImage(media,0,0);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!current() || !blob || blob.size>16*1024*1024)throw Error('Frame or source changed, or PNG is too large.');
    await onSaveFrame(blob,timeMs,current);if(current())status.textContent='Frame copy saved; playback time and pixel correspondence are unverified.';
  }catch(error){if(valid())status.textContent=error.message || 'Frame copy unavailable.';}finally{checkFrame.hidden=!hasPendingFrame();}};
  checkFrame.onclick=async()=>{try{const result=await onCheckFrame();if(valid())status.textContent=result;}catch(error){if(valid())status.textContent=error.message || 'Frame save status unavailable.';}finally{checkFrame.hidden=!hasPendingFrame();}};

  trim.onclick=async()=>{try{
    if(hasPendingTrim())throw Error('Check the original trim job before starting another.');
    if(selection?.kind!=='time_range')throw Error('Mark a start and end first.');
    const range=Object.freeze({startMs:selection.startMs,endMs:selection.endMs});
    if(!Number.isSafeInteger(range.startMs)||!Number.isSafeInteger(range.endMs)||range.startMs<0||range.endMs>86400000||range.endMs<=range.startMs||range.endMs-range.startMs>60000)throw Error('Choose a range of at most 60 seconds.');
    await onTrim(range,()=>valid());
  }catch(error){if(valid())status.textContent=error.message || 'Trim is unavailable.';}finally{checkTrim.hidden=!hasPendingTrim();}};
  exportBundle.onclick=async()=>{try{const result=await onExport(()=>valid());if(valid() && result===true)status.textContent='Recording feedback export saved.';}catch(error){if(valid())status.textContent=error.message || 'Export is unavailable.';}finally{checkExport.hidden=!hasPendingExport();}};
  checkExport.onclick=async()=>{try{const result=await onCheckExport();if(valid())status.textContent=result;}catch(error){if(valid())status.textContent=error.message || 'Export status is unavailable.';}finally{checkExport.hidden=!hasPendingExport();}};
  checkTrim.onclick=async()=>{try{const result=await onCheckTrim();if(valid())status.textContent=result;}catch(error){if(valid())status.textContent=error.message || 'Trim status unavailable.';}finally{checkTrim.hidden=!hasPendingTrim();}};


  media.addEventListener('loadedmetadata',show,{signal:listeners.signal});
  media.addEventListener('error',()=>{if(valid())status.textContent='Video playback is unavailable or this codec is unsupported. The original remains downloadable.';},{signal:listeners.signal});
  const retire=()=>dispose();
  document.addEventListener('openclank:auth-context-changed',retire,{signal:listeners.signal});document.addEventListener('openclank:file-policy-changed',retire,{signal:listeners.signal});window.addEventListener('workspace-change',retire,{signal:listeners.signal});
  window.addEventListener('openclank-files-resource-mutated',event=>{if(String(event.detail?.resourceRef || '')===resourceRef)dispose();},{signal:listeners.signal});
  function dispose(){if(disposed)return;disposed=true;listeners.abort();media.pause();media.removeAttribute('src');media.load();root.replaceChildren();}
  controls.append(menu);
  if (nativeRecording) controls.append(coverageDetails(recordingCoverage));
  root.replaceChildren(media,controls,status);show();return Object.freeze({dispose,media});
}
