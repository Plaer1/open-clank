const normalizeNewlines = text => String(text ?? '').replace(/\r\n?|\n/g, '\n');

export function changedTextRange(before, after) {
  const previous=normalizeNewlines(before), current=normalizeNewlines(after);
  if (previous === current) return null;
  let from=0, oldTo=previous.length, to=current.length;
  while (from < oldTo && from < to && previous[from] === current[from]) from++;
  while (oldTo > from && to > from && previous[oldTo-1] === current[to-1]) { oldTo--; to--; }
  const firstLine=1+(current.slice(0,from).match(/\n/g)?.length || 0);
  const lastLine=firstLine+(current.slice(from,to).match(/\n/g)?.length || 0);
  return {from, oldTo, to, firstLine, lastLine, insert:current.slice(from,to)};
}

export function replaceEditorTextPreservingView(editor, text) {
  const view=editor?.view;
  if (!view) return;
  const change=changedTextRange(view.state.doc.toString(),text);
  if (!change) { editor.setValue(text); return; }
  const positionMap=view.state.changes({from:change.from,to:change.oldTo,insert:change.insert});
  const {ranges,mainIndex}=view.state.selection;
  const position=value=>positionMap.mapPos(value,-1);
  const {scrollTop,scrollLeft}=view.scrollDOM;
  editor.setValue(text,{ranges:ranges.map(({anchor,head})=>({anchor:position(anchor),head:position(head)})),mainIndex});
  view.scrollDOM.scrollTop=scrollTop;
  view.scrollDOM.scrollLeft=scrollLeft;
}

/** Watch each authorized containing directory once. Events signal a read, never a revision. */
export function createHostFileWatch({client, isCurrent, reconcile, unavailable}) {
  const files=new Map(), folders=new Map();
  let disposed=false;

  function valid(entry) {
    return !disposed && isCurrent() && !entry.controller.signal.aborted && files.get(entry.id)===entry;
  }

  function queue(entry, immediate=false) {
    if (!valid(entry)) return;
    if (entry.running) { entry.queued=true; return; }
    clearTimeout(entry.timer);
    const run=async () => {
      entry.timer=null;
      if (!valid(entry)) return;
      entry.running=true;
      try { await reconcile(entry,()=>valid(entry)); }
      catch (_) { if (valid(entry)) unavailable(entry,'read'); }
      finally {
        entry.running=false;
        if (entry.queued) { entry.queued=false; queue(entry); }
      }
    };
    if (immediate) void run();
    else entry.timer=setTimeout(run,180);
  }

  function start(folder) {
    if (disposed || !isCurrent() || folder.controller || !folder.entries.size) return;
    const controller=folder.controller=new AbortController();
    void (async () => {
      try {
        for await (const event of client.watch(folder.ref,{signal:controller.signal})) {
          if (disposed || !isCurrent() || controller.signal.aborted) return;
          // Watch events are intentionally path-free, including after a gap or overflow.
          for (const entry of folder.entries) queue(entry);
        }
      } catch (_) {
        // The directory can lose watch capability while exact file reads still work.
      } finally {
        if (folder.controller === controller) folder.controller=null;
        if (!controller.signal.aborted && !disposed && isCurrent()) {
          for (const entry of folder.entries) if (valid(entry)) { unavailable(entry,'watch'); queue(entry); }
        }
      }
    })();
  }

  async function identifyFolder(entry) {
    if (entry.identifying) return;
    entry.identifying=true;
    try {
      const result=await client.reveal(entry.ref,{signal:entry.controller.signal});
      if (!valid(entry)) return;
      const {resource,parent}=result || {};
      if (resource?.provider !== 'host' || resource.id !== entry.resourceId ||
          parent?.provider !== 'host' || !parent.id || !parent.ref || !parent.capabilities?.includes('watch')) {
        unavailable(entry,'watch');
        return;
      }
      let folder=folders.get(parent.id);
      if (!folder) { folder={id:parent.id,ref:parent.ref,entries:new Set(),controller:null}; folders.set(parent.id,folder); }
      else folder.ref=parent.ref;
      entry.folder=folder;
      folder.entries.add(entry);
      start(folder);
    } catch (_) {
      if (valid(entry)) unavailable(entry,'watch');
    } finally { entry.identifying=false; }
  }

  function release(entry) {
    entry.controller.abort();
    clearTimeout(entry.timer);
    const folder=entry.folder;
    if (!folder) return;
    folder.entries.delete(entry);
    if (!folder.entries.size) { folder.controller?.abort(); folders.delete(folder.id); }
  }

  function sync(targets) {
    const targetsById=new Map(targets.map(target=>[target.id,target]));
    for (const [id,entry] of files) if (!targetsById.has(id)) { release(entry); files.delete(id); }
    for (const target of targets) {
      const entry=files.get(target.id);
      if (entry?.resourceId === target.resourceId) {
        entry.ref=target.ref;
        continue;
      }
      if (entry) release(entry);
      const next={...target,controller:new AbortController(),folder:null,timer:null,running:false,queued:false,identifying:false};
      files.set(target.id,next);
      queue(next);
      void identifyFolder(next);
    }
  }

  function reconcileAll({reconnect=false}={}) {
    for (const entry of files.values()) {
      if (reconnect) {
        if (entry.folder) start(entry.folder);
        else void identifyFolder(entry);
      }
      queue(entry);
    }
  }

  return {
    sync, reconcileAll,
    manual(id) { const entry=files.get(id); if (!entry) return false; if (entry.folder) start(entry.folder); else void identifyFolder(entry); queue(entry,true); return true; },
    dispose() { disposed=true; for (const entry of files.values()) release(entry); files.clear(); folders.clear(); },
  };
}
