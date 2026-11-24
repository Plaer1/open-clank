import { parseSlideDeck, serializeSlideDeck, editSlideObject, renderSlideSvg } from './slideModel.js';

// The caller owns the Notes/Files buffer, resource authority and persistence.
export function mountSlideView(root, { getSource, transact, readOnly = false, sharedTextMode = false, isCurrent = () => true,
  captureAppearance = () => null, getAssets = () => new Map(), onOpenSource = null, bindContext = null } = {}) {
  if (!root || typeof getSource !== 'function' || typeof transact !== 'function') throw new TypeError('Slide view requires a source and transaction owner');
  let slideId = null, objectId = null, disposed = false, timer = null, presentation = false, preview = null, status = null, mounted = null, selectionGeneration = 0;
  const current = () => !disposed && isCurrent() && (!mounted || root.contains(mounted));
  const el = (tag, value) => { const node = document.createElement(tag); if (value != null) node.textContent = String(value); return node; };
  const schedule = () => { if (!current()) return; clearTimeout(timer); timer = setTimeout(() => { timer = null; refresh(); }, 120); };
  const activeEditor = () => {
    const active = document.activeElement;
    return active && root.contains(active) && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
  };
  function assetMap(deck, slide) {
    if (!current()) return new Map();
    const resources = slide.objects.filter(o => o.type === 'image' || o.type === 'chart')
      .map(o => ({type:o.type, resourceRef:o.resourceRef, revision:o.revision, chartId:o.chartId}));
    try {
      const result = getAssets({deckId:deck.id, slideId:slide.id, resources});
      return current() && result instanceof Map ? result : new Map();
    } catch (_) { return new Map(); }
  }
  function draw(deck, slide) {
    if (!current() || !preview) return;
    try {
      preview.innerHTML = renderSlideSvg(deck, slide.id, {assets:assetMap(deck,slide), identifyObjects:Boolean(bindContext)});
      if (bindContext) for (const node of preview.querySelectorAll('[data-slide-object]')) {
        bindContext(node, {slideId:slide.id, objectId:node.dataset.slideObject, objectType:node.dataset.slideType});
      }
    }
    catch (error) { preview.textContent = `Slide preview unavailable: ${error.message}`; }
  }
  function transaction(input, update) {
    if (!current() || readOnly) return;
    // Capture value at the input event, before any later blur/save/refresh.
    const value = input.value;
    let appearance = null;
    try { appearance = captureAppearance(); } catch (_) { /* no theme producer */ }
    try {
      const result = transact(source => {
        if (!current()) throw new Error('Slide view is no longer current');
        const deck = update(source, value);
        if (!sharedTextMode && deck.appearance == null && appearance) deck.appearance = appearance;
        return serializeSlideDeck(deck);
      });
      if (result?.outcome === 'failed') throw new Error(result.message || 'Slide edit unavailable');
      input.setCustomValidity(''); if (status) status.textContent = ''; schedule();
    } catch (error) { input.setCustomValidity(error.message || 'Slide edit unavailable'); input.reportValidity(); if (status) status.textContent = 'Edit not saved'; }
  }
  function inputField(parent, labelText, value, update, {multiline = false, numeric = false} = {}) {
    const label = el('label', labelText), input = el(multiline ? 'textarea' : 'input');
    if (!multiline) input.type = numeric ? 'number' : 'text';
    input.value = String(value ?? ''); input.readOnly = readOnly; input.setAttribute('aria-label', labelText);
    const commit = () => transaction(input, (source, raw) => {
      if (numeric && (raw.trim() === '' || !Number.isFinite(Number(raw)))) throw new TypeError('Enter a valid number');
      return update(source, numeric ? Number(raw) : raw);
    });
    input.addEventListener('input', commit); input.addEventListener('compositionend', commit); input.addEventListener('blur', schedule);
    label.append(input); parent.append(label); return input;
  }
  function refresh() {
    if (!current()) return;
    let deck;
    try { deck = parseSlideDeck(getSource()); }
    catch (error) {
      if (activeEditor()) return;
      const msg = el('p', `Slide source unavailable: ${error.message}`), nodes = [msg];
      if (typeof onOpenSource === 'function') { const open = el('button', 'Open source'); open.type = 'button'; open.addEventListener('click', () => { if (current()) onOpenSource(); }); nodes.push(open); }
      root.replaceChildren(...nodes); mounted = msg; preview = null; return;
    }
    const slide = deck.slides.find(item => item.id === slideId) || deck.slides[0]; if (slideId !== slide.id) selectionGeneration++; slideId = slide.id;
    if (activeEditor()) { draw(deck,slide); return; }
    const object = slide.objects.find(item => item.id === objectId) || slide.objects[0] || null; if (objectId !== (object?.id || null)) selectionGeneration++; objectId = object?.id || null;
    const section = el('section'); section.setAttribute('aria-label', 'Slide deck');
    const slideLabel = el('label', 'Slide '), slideSelect = el('select'); slideSelect.setAttribute('aria-label', 'Slide');
    for (const item of deck.slides) { const option = el('option', item.name); option.value = item.id; option.selected = item.id === slideId; slideSelect.append(option); }
    slideSelect.value = slideId; slideSelect.addEventListener('change', () => { if (!current()) return; if (slideId !== slideSelect.value) { slideId = slideSelect.value; objectId = null; selectionGeneration++; } refresh(); }); slideLabel.append(slideSelect); section.append(slideLabel);
    const toggle = el('button', presentation ? 'Exit presentation' : 'Present'); toggle.type = 'button';
    toggle.addEventListener('click', () => { if (!current()) return; presentation = !presentation; refresh(); }); section.append(toggle);
    if (typeof onOpenSource === 'function') { const open = el('button', 'Open source'); open.type = 'button'; open.addEventListener('click', () => { if (current()) onOpenSource(); }); section.append(open); }
    preview = el('div'); preview.setAttribute('aria-label', `Preview: ${slide.name}`); section.append(preview);
    if (!presentation) {
      const controls = el('div'); controls.setAttribute('aria-label', 'Slide properties');
      const selectorLabel = el('label', 'Object '), selector = el('select'); selector.setAttribute('aria-label', 'Slide object');
      if (!object) { const empty = el('option','No objects'); empty.value = ''; selector.append(empty); selector.disabled = true; }
      for (const item of slide.objects) { const option = el('option', `${item.type}: ${item.id}`); option.value = item.id; option.selected = item.id === objectId; selector.append(option); }
      selector.value = objectId || ''; selector.addEventListener('change', () => { if (!current()) return; if (objectId !== selector.value) { objectId = selector.value; selectionGeneration++; } refresh(); }); selectorLabel.append(selector); controls.append(selectorLabel);
      if (sharedTextMode) controls.append(el('p', 'Shared editing is limited to existing text objects.'));
      if (object) {
        if (object.type === 'text') inputField(controls, 'Text', object.text, (source,value) => editSlideObject(source,slide.id,object.id,{text:value}), {multiline:true});
        if (!sharedTextMode) for (const property of ['x','y','width','height']) inputField(controls, property, object[property], (source,value) => editSlideObject(source,slide.id,object.id,{[property]:value}), {numeric:true});
        if (!sharedTextMode && object.type === 'vector') for (const property of ['fill','stroke']) inputField(controls, property, object[property] || '', (source,value) => editSlideObject(source,slide.id,object.id,{[property]:value}));
      }
      if (!sharedTextMode) inputField(controls, 'Speaker notes', slide.notes, (source,value) => {
        const next = parseSlideDeck(source), selected = next.slides.find(item => item.id === slide.id);
        if (!selected) throw new TypeError('Slide is no longer available'); selected.notes = value; return next;
      }, {multiline:true});
      status = el('p'); status.setAttribute('role','status'); controls.append(status); section.append(controls);
    } else status = null;
    root.replaceChildren(section); mounted = section; draw(deck,slide);
  }
  refresh();
  return { refresh:schedule, getSelection() { return current() ? {slideId,objectId,presentation,generation:selectionGeneration} : null; },
    dispose() { if (disposed) return; disposed = true; clearTimeout(timer); timer = null; preview = null; status = null; if (mounted && root.contains(mounted)) root.replaceChildren(); mounted = null; } };
}
