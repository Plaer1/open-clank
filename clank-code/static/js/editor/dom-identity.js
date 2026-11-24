/** Local selector for an editor ID, supporting legacy and namespaced nodes. */
export function editorIdSelector(id) {
  if (!/^ge-[a-zA-Z0-9_-]+$/.test(id)) throw new TypeError('invalid editor ID');
  return `:is(#${id},[data-ge-id="${id}"])`;
}

/** Namespace editor-owned IDs and local ID references before they enter a document. */
export function namespaceEditorIds(root, prefix) {
  if (!root || !/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(prefix)) throw new TypeError('invalid editor identity');
  const nodes = [root, ...root.querySelectorAll('[id], [for], [aria-labelledby], [aria-describedby], [aria-controls], [aria-owns], [aria-activedescendant], [href], [data-spin-for]')];
  const ids = new Map();
  for (const node of nodes) {
    const original = node.getAttribute?.('id');
    if (!original || !original.startsWith('ge-') || node.hasAttribute('data-ge-id')) continue;
    ids.set(original, `${prefix}--${original}`);
    node.setAttribute('data-ge-id', original); node.id = ids.get(original);
  }
  for (const node of nodes) {
    for (const attribute of ['for','aria-labelledby','aria-describedby','aria-controls','aria-owns','aria-activedescendant','data-spin-for']) {
      const value = node.getAttribute?.(attribute); if (!value) continue;
      const next = value.split(/\s+/).map(id => ids.get(id) || (id.startsWith('ge-') && !id.startsWith(`${prefix}--`) ? `${prefix}--${id}` : id)).join(' ');
      if (next !== value) node.setAttribute(attribute, next);
    }
    const href = node.getAttribute?.('href');
    if (href?.startsWith('#ge-') && !href.startsWith(`#${prefix}--`)) node.setAttribute('href', '#' + (ids.get(href.slice(1)) || `${prefix}--${href.slice(1)}`));
  }
  return root;
}
