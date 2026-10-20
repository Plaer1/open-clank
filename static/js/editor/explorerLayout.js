// Scoped presentation state. No resource authority or content lives here.
const PREFIX = 'openclank-explorer-layout-v1';
const bounded = (value, low, high, fallback) => Number.isFinite(Number(value)) ? Math.max(low, Math.min(high, Number(value))) : fallback;
const element = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };

export function createExplorerLayout({ getScope, onApplied = null } = {}) {
  const descriptors = new Map();
  let identity = null, preferences = { entries:{}, share:0.5 };
  let applying = false;
  const scopeKey = () => {
    const scope = getScope?.() || {};
    return scope.owner ? `${PREFIX}:${encodeURIComponent(scope.owner)}:${encodeURIComponent(scope.workspace || 'default')}:${encodeURIComponent(scope.surface || 'files')}` : null;
  };
  const load = () => {
    const key = scopeKey();
    if (key === identity) return;
    identity = key; preferences = { entries:{}, share:0.5 };
    try {
      const stored = key && JSON.parse(localStorage.getItem(key) || 'null');
      if (stored && typeof stored.entries === 'object' && !Array.isArray(stored.entries)) preferences = { entries:stored.entries || {}, share:bounded(stored.share,0.15,0.85,0.5) };
    } catch (_) { /* Optional presentation preferences. */ }
  };
  const save = () => { try { if (identity) localStorage.setItem(identity, JSON.stringify(preferences)); } catch (_) {} };
  const records = () => {
    load(); return [...descriptors.values()].map((descriptor, index) => {
      const stored = preferences.entries[descriptor.id] || {};
      return { ...descriptor, hidden:stored.hidden === true, collapsed:typeof stored.collapsed === 'boolean' ? stored.collapsed : descriptor.getCollapsed?.() === true,
        height:stored.height === 'cap' ? 'cap' : 'auto', cap:bounded(stored.cap,64,1200,240),
        order:bounded(stored.order,0,10000,index), hasCollapse:typeof stored.collapsed === 'boolean' };
    });
  };
  const apply = () => {
    if (applying) return;
    applying = true;
    try {
      const groups = new Map();
      for (const record of records()) {
        const node = record.element?.();
        if (!node) continue;
        node.hidden = record.hidden;
        node.dataset.explorerCategory = record.id;
        const content = record.content?.();
        if (content) {
          content.style.maxHeight = record.height === 'cap' ? `${record.cap}px` : '';
          content.style.overflowY = record.height === 'cap' ? 'auto' : '';
          if (!record.setCollapsed) content.hidden = record.collapsed;
        }
        record.setCollapsed?.(record.collapsed, record.hasCollapse);
        record.sync?.(record);
        const parent = node.parentElement;
        if (parent) { const group = groups.get(parent) || []; group.push({node,record}); groups.set(parent,group); }
      }
      for (const [parent, group] of groups) {
        const ordered = [...group].sort((a,b) => a.record.order - b.record.order).map(item => item.node);
        const current = [...parent.children].filter(node => ordered.includes(node));
        if (ordered.some((node,index) => node !== current[index])) for (const node of ordered) parent.append(node);
      }
      onApplied?.(records(), preferences.share);
    } finally { applying = false; }
  };
  const update = (id, patch) => {
    load(); if (!descriptors.has(id)) return;
    const current = records().find(item => item.id === id);
    if (patch.move) {
      const peers = records().filter(item => (item.group || '') === (current.group || '')).sort((a,b) => a.order-b.order);
      const index = peers.findIndex(item => item.id === id), other = index + (patch.move === 'up' ? -1 : 1);
      if (other >= 0 && other < peers.length) {
        [peers[index], peers[other]] = [peers[other], peers[index]];
        peers.forEach((item, order) => preferences.entries[item.id] = { ...preferences.entries[item.id], order });
      }
    }
    const previous = preferences.entries[id] || {};
    preferences.entries[id] = { ...previous,
      ...(typeof patch.hidden === 'boolean' ? {hidden:patch.hidden} : {}),
      ...(typeof patch.collapsed === 'boolean' ? {collapsed:patch.collapsed} : {}),
      ...(patch.height ? {height:patch.height === 'cap' ? 'cap' : 'auto'} : {}),
      ...(patch.cap != null ? {cap:bounded(patch.cap,64,1200,240)} : {}),
    };
    save(); apply();
  };
  return {
    register(descriptor) { descriptors.set(descriptor.id,descriptor); apply(); },
    remove(id) { descriptors.delete(id); },
    records, apply, update, scopeKey,
    getShare() { load(); return preferences.share; },
    setShare(share) { load(); preferences.share = bounded(share,0.15,0.85,0.5); save(); apply(); },
    reset() { load(); preferences = { entries:{}, share:0.5 }; save(); for (const descriptor of descriptors.values()) descriptor.reset?.(); apply(); },
  };
}

export function showExplorerCustomization(controllers, { title = 'Customize Explorer', onClose = null } = {}) {
  const active = controllers.filter(Boolean), scopes = active.map(controller => controller.scopeKey());
  const dialog = element('dialog'); dialog.className = 'oc-explorer-customize';
  const heading = element('h2', title), intro = element('p','These are categories inside Files, separate from the navigation tabs. Hiding a category only changes this surface; resources stay available in their own applets. Chats remain outside Editor.');
  heading.id='oc-explorer-customize-title';dialog.setAttribute('aria-labelledby',heading.id);
  const content = element('div'); content.className = 'oc-explorer-customize-rows';
  const status = element('p'); status.setAttribute('role','status');
  const valid = () => active.every((controller,index) => controller.scopeKey() === scopes[index]);
  const change = (controller,id,patch) => { if (!valid()) { status.textContent = 'Account or workspace changed. Reopen Customize Explorer.'; return; } controller.update(id,patch); };
  const draw = () => {
    content.replaceChildren();
    for (const controller of active) for (const record of controller.records().sort((a,b) => String(a.group || '').localeCompare(String(b.group || '')) || a.order-b.order)) {
      const row = element('fieldset'); row.dataset.layoutId=record.id; row.append(element('legend',record.label));
      const toggle = (label, checked, callback) => {
        const wrapper = element('label',label), input = element('input'); input.type = 'checkbox'; input.checked = checked;
        input.addEventListener('change',()=>callback(input.checked)); wrapper.prepend(input); row.append(wrapper);
      };
      toggle('Show',!record.hidden,value=>change(controller,record.id,{hidden:!value}));
      toggle('Collapsed',record.collapsed,value=>change(controller,record.id,{collapsed:value}));
      for (const [move,label] of [['up','Move up'],['down','Move down']]) {
        const button = element('button',label); button.type = 'button'; button.setAttribute('aria-label',`${label}: ${record.label}`);
        button.addEventListener('click',()=>{ change(controller,record.id,{move}); draw(); [...content.querySelectorAll('fieldset')].find(item=>item.dataset.layoutId===record.id)?.querySelector(`button[aria-label="${label}: ${record.label}"]`)?.focus(); }); row.append(button);
      }
      const height = element('select'); height.setAttribute('aria-label',`${record.label} category height`);
      for (const [value,label] of [['auto','Height: Auto'],['cap','Height: capped']]) { const option = element('option',label); option.value=value; height.append(option); }
      height.value=record.height;
      const capLabel = element('label','Cap in pixels'), cap = element('input'); cap.type='number'; cap.min='64'; cap.max='1200'; cap.step='16'; cap.value=String(record.cap);
      capLabel.hidden=record.height !== 'cap'; capLabel.append(cap);
      height.addEventListener('change',()=>{ capLabel.hidden=height.value !== 'cap'; change(controller,record.id,{height:height.value}); });
      cap.addEventListener('change',()=>change(controller,record.id,{cap:cap.value})); row.append(height,capLabel); content.append(row);
    }
  };
  const actions=element('footer'), reset=element('button','Reset layout and sizes'), done=element('button','Done'); reset.type=done.type='button';
  reset.addEventListener('click',()=>{ if (valid()) active.forEach(controller=>controller.reset()); draw(); });
  done.addEventListener('click',()=>dialog.close()); actions.append(reset,done);
  dialog.addEventListener('close',()=>{dialog.remove();onClose?.();});
  dialog.append(heading,intro,content,status,actions); draw(); document.body.append(dialog); dialog.showModal(); done.focus(); return dialog;
}

export function createExplorerDivider({ getContainer, getShare, setShare, getDirection = () => 1, label = 'Files places and Copal documents heights' }) {
  const divider=element('div'); divider.className='copal-explorer-section-divider'; divider.tabIndex=0;
  divider.setAttribute('role','separator'); divider.setAttribute('aria-orientation','horizontal'); divider.setAttribute('aria-label',label);
  divider.setAttribute('aria-valuemin','15'); divider.setAttribute('aria-valuemax','85');
  const update = value => { setShare(value); divider.setAttribute('aria-valuenow',String(Math.round(getShare()*100))); divider.setAttribute('aria-valuetext',`${Math.round(getShare()*100)} percent Files places`); };
  divider.addEventListener('keydown',event=>{
    const value=event.key==='ArrowUp' ? getShare()-0.05 : event.key==='ArrowDown' ? getShare()+0.05 : event.key==='Home' ? 0.15 : event.key==='End' ? 0.85 : ['Enter','0'].includes(event.key) ? 0.5 : null;
    if (value!=null) {event.preventDefault();update(value);}
  });
  divider.addEventListener('dblclick',()=>update(0.5));
  divider.addEventListener('pointerdown',event=>{
    if (event.button!==0) return;
    event.preventDefault(); divider.focus(); divider.setPointerCapture(event.pointerId);
    const start=event.clientY, initial=getShare(), height=Math.max(1,getContainer()?.getBoundingClientRect().height || 1);
    const move=e=>{if(e.pointerId===event.pointerId)update(initial+getDirection()*(e.clientY-start)/height);};
    const stop=e=>{if(e.pointerId!==event.pointerId)return;divider.removeEventListener('pointermove',move);divider.removeEventListener('pointerup',stop);divider.removeEventListener('pointercancel',stop);divider.removeEventListener('lostpointercapture',stop);if(divider.hasPointerCapture(event.pointerId))divider.releasePointerCapture(event.pointerId);};
    divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',stop);divider.addEventListener('pointercancel',stop);divider.addEventListener('lostpointercapture',stop);
  });
  divider.setAttribute('aria-valuenow',String(Math.round(getShare()*100))); return divider;
}
