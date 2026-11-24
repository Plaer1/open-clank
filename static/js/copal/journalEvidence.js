// Load committed witnesses on demand. Each action resolves the page again.
export function journalEvidence({h, load, open, current}) {
  const box = h('details', {class:'copal-graph-evidence'});
  const list = h('div', {'aria-live':'polite'});
  const valid = () => box.isConnected && box.open && current();
  let generation = 0, watch;
  box.disposeEvidence = () => { generation++; watch?.abort(); list.replaceChildren(); };
  const identity = row => JSON.stringify([row.kind, row.from, row.to, row.line, row.path, row.span]);
  async function run(action) {
    const turn = ++generation;
    list.textContent = 'Checking evidence…';
    try {
      const data = await load();
      if (!valid() || turn !== generation) return;
      list.replaceChildren();
      if (!data.rows.length) list.append(h('p',{text:'No current witnesses in this selection.'}));
      for (const row of data.rows) {
        const stamp = row.timestamp;
        const date = typeof stamp === 'number' ? new Date(stamp < 1e12 ? stamp * 1000 : stamp) : new Date(stamp || NaN);
        const kind = {changed:'Operation → page', tracks:'Page → task', cites:'Page → fact', supports:'Evidence → fact', mentions:'Page → code'}[row.kind];
        const label = [kind, row.label, row.line && ('page line '+row.line),
          row.path && (row.path + (row.span?.start_line ? ' line '+row.span.start_line : '')),
          Number.isFinite(date.getTime()) && date.toLocaleString()].filter(Boolean).join(' · ');
        list.append(h('button', {type:'button',class:'copal-graph-inspector-chip',text:label,
          onclick:()=>void run(fresh=>{
            const selected = fresh.rows.find(r=>identity(r)===identity(row));
            if (!selected || !valid()) return;
            if (selected.kind === 'changed') {
              list.append(h('p',{text:`${selected.label} · ${selected.from.id} → ${selected.to.id}`}));
              return;
            }
            if (selected.kind === 'supports') {
              list.append(h('blockquote',{text:selected.quote}),h('p',{text:
                `${selected.sourceType} · ${selected.from.sourceId} · revision ${selected.from.revision} · ${selected.from.id}`}));
              return;
            }
            return open(selected, valid);
          })}));
      }
      if (Object.values(data.coverage).some(Boolean)) list.append(h('p',{text:'Some witnesses are unavailable or outside the displayed limits.'}));
      if (data.rows.length) list.append(h('button',{type:'button',text:'Export current evidence',onclick:()=>void run(fresh=>{
        const url = URL.createObjectURL(new Blob([JSON.stringify(fresh,null,2)],{type:'application/json'}));
        const a = h('a',{href:url,download:'journal-evidence.json'});
        a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
      })}));
      if (action) await action(data);
    } catch {
      if (valid() && turn === generation) list.textContent = 'Evidence changed or is unavailable. Reopen to refresh.';
    }
  }
  box.append(h('summary',{text:'Source evidence'}),list);
  box.addEventListener('toggle',()=>{
    box.disposeEvidence();
    if (!box.open) return;
    watch = new AbortController();
    const invalidate = () => { box.open = false; box.disposeEvidence(); };
    for (const name of ['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed',
      'openclank-policy-changed','openclank:file-policy-changed']) {
      document.addEventListener(name,invalidate,{signal:watch.signal});
      window.addEventListener(name,invalidate,{signal:watch.signal});
    }
    void run();
  });
  return box;
}
