// Notes view, V1 skeleton: doc list + plain-textarea editor wired to
// DocSync. V2 replaces the textarea with the vendored CodeMirror bundle
// (ui/packages/) and adds side panes, history panel, reading mode.

import { listDocs, getDoc } from '../api.js';
import { createDocSync } from '../sync.js';

const TEXT_KINDS = new Set(['markdown', 'base', 'canvas', 'planning']);

export function initNotesView({ store }) {
  const docList = document.getElementById('doc-list');
  const docCount = document.getElementById('doc-count');
  const docFilter = document.getElementById('doc-filter');
  const docName = document.getElementById('doc-name');
  const docStatus = document.getElementById('doc-status');
  const editor = document.getElementById('editor');
  const statusHead = document.getElementById('status-head');
  const statusSync = document.getElementById('status-sync');

  const sync = createDocSync({
    getContent: () => editor.value,
    setContent: (text) => { editor.value = text; },
    onStatus: ({ text, head, dirty }) => {
      statusSync.textContent = text;
      statusHead.textContent = `head ${head ? head.slice(0, 12) : '—'}`;
      docStatus.textContent = dirty ? 'unsaved' : '';
      docStatus.classList.toggle('dirty', dirty);
    },
  });

  editor.addEventListener('input', () => sync.edited());

  async function refresh() {
    const res = await listDocs();
    if (!res.ok) return;
    store.set({ docs: res.body.docs });
  }

  function render() {
    const { docs = [], activeDocId } = store.get();
    const filter = docFilter.value.trim().toLowerCase();
    const visible = docs.filter((doc) =>
      TEXT_KINDS.has(doc.kind) && (!filter || doc.name.toLowerCase().includes(filter)));
    docCount.textContent = `${visible.length} docs`;
    docList.replaceChildren(...visible.map((doc) => {
      const button = document.createElement('button');
      button.className = 'doc-item' + (doc.id === activeDocId ? ' active' : '');
      button.textContent = doc.name;
      const kind = document.createElement('span');
      kind.className = 'doc-kind';
      kind.textContent = doc.kind;
      button.appendChild(kind);
      button.addEventListener('click', () => openDoc(doc.id));
      return button;
    }));
  }

  async function openDoc(id) {
    const res = await getDoc(id);
    if (!res.ok) return;
    const doc = res.body;
    store.set({ activeDocId: id });
    docName.textContent = doc.name;
    editor.value = doc.text ?? '';
    editor.disabled = false;
    sync.open(id, doc.head);
    editor.focus();
  }

  docFilter.addEventListener('input', render);
  store.subscribe((_, keys) => {
    if (keys.includes('docs') || keys.includes('activeDocId')) render();
  });

  return {
    refresh,
    sync,
    // SSE handlers
    onDocChanged(doc) {
      if (doc.deleted) {
        refresh();
        return;
      }
      sync.applyRemote(doc);
      // Name/kind/head changed → keep the list fresh without a full refetch.
      const { docs = [] } = store.get();
      const index = docs.findIndex((entry) => entry.id === doc.id);
      if (index >= 0) {
        const next = [...docs];
        next[index] = { ...next[index], name: doc.name, head: doc.head, ts: doc.ts };
        store.set({ docs: next });
      } else {
        refresh();
      }
    },
    async onReconnect() {
      await refresh();
      const id = sync.docId;
      if (id) {
        const res = await getDoc(id);
        if (res.ok) sync.resync(res.body);
      }
    },
  };
}
