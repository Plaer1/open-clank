/** Preserve each panel's visible resource anchor through scoped DOM updates. */
export function capturePanelPositions(root, saved = new Map()) {
  for (const panel of root.querySelectorAll('[data-note-panel]')) {
    const viewport = panel.getBoundingClientRect();
    const scale = panel.offsetHeight > 0 ? viewport.height / panel.offsetHeight : 1;
    const rows = [...panel.querySelectorAll('[data-note-tree-key]')].filter(row => row.getClientRects().length);
    const positions = rows.map(row => ({ key:row.dataset.noteTreeKey, top:(row.getBoundingClientRect().top - viewport.top) / scale }));
    const first = positions.findIndex((item, index) => item.top + rows[index].getBoundingClientRect().height / scale > 0);
    const focused = panel.contains(document.activeElement) ? document.activeElement : null;
    saved.set(panel.dataset.notePanel, {
      top:panel.scrollTop, left:panel.scrollLeft,
      // Prefer surviving rows in the viewport, then preceding parents/rows.
      anchors:first < 0 ? [] : [...positions.slice(first), ...positions.slice(0, first).reverse()],
      focusKey:focused?.closest('[data-note-tree-key]')?.dataset.noteTreeKey,
      focusParent:focused?.closest('[data-note-tree-key]')?.dataset.noteParent,
      focusLabel:focused?.getAttribute('aria-label'),
    });
  }
  return saved;
}

export function restorePanelPositions(root, saved) {
  for (const panel of root.querySelectorAll('[data-note-panel]')) {
    const position = saved.get(panel.dataset.notePanel);
    if (!position) continue;
    panel.scrollTop = position.top; panel.scrollLeft = position.left;
    const rows = new Map([...panel.querySelectorAll('[data-note-tree-key]')].filter(row => row.getClientRects().length).map(row => [row.dataset.noteTreeKey, row]));
    const anchor = position.anchors.find(item => rows.has(item.key));
    if (anchor) {
      const viewport = panel.getBoundingClientRect();
      const scale = panel.offsetHeight > 0 ? viewport.height / panel.offsetHeight : 1;
      panel.scrollTop += (rows.get(anchor.key).getBoundingClientRect().top - viewport.top) / scale - anchor.top;
    }
    let focused = rows.get(position.focusKey);
    // A collapsed focused descendant falls back to its nearest visible parent.
    if (!focused && position.focusKey) {
      let parent = position.focusParent;
      while (parent && !focused) { focused = rows.get(`folder:${parent}`); parent = parent.includes('/') ? parent.slice(0, parent.lastIndexOf('/')) : ''; }
    }
    if (!focused && position.focusLabel) focused = panel.querySelector(`[aria-label="${CSS.escape(position.focusLabel)}"]`);
    focused?.focus({ preventScroll:true });
  }
}
