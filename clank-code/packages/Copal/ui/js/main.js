// Copal vanilla UI — entry point (V1: Notes + sync core + ops/undo).

import { createStore } from './store.js';
import { connectEvents } from './events.js';
import { listOps, undo } from './api.js';
import { initNotesView } from './views/notes.js';

const store = createStore({ docs: [], activeDocId: null });
const notes = initNotesView({ store });

// ── Sync-state badge + the one data stream ──
const syncState = document.getElementById('sync-state');
let wasOffline = false;

connectEvents({
  onState(state) {
    syncState.textContent = state;
    syncState.classList.toggle('offline', state === 'offline');
    if (state === 'live' && wasOffline) {
      wasOffline = false;
      notes.onReconnect();
    }
    if (state === 'offline') wasOffline = true;
  },
  onDoc(doc) {
    notes.onDocChanged(doc);
  },
  onView() {
    // Op landed (checkpoint/undo/import/delete) — refresh doc list.
    notes.refresh();
  },
});

// ── Header: undo + op log ──
document.getElementById('undo-btn').addEventListener('click', async () => {
  await undo();
  notes.refresh();
});

const opsPanel = document.getElementById('ops-panel');
const opsList = document.getElementById('ops-list');
document.getElementById('ops-btn').addEventListener('click', async () => {
  opsPanel.classList.toggle('hidden');
  if (opsPanel.classList.contains('hidden')) return;
  const res = await listOps(40);
  if (!res.ok) return;
  opsList.replaceChildren(...res.body.ops.map((op) => {
    const row = document.createElement('div');
    row.className = 'ops-item';
    const kind = document.createElement('span');
    kind.className = 'op-kind';
    kind.textContent = op.kind;
    row.append(kind, document.createTextNode(op.description));
    return row;
  }));
});
document.getElementById('ops-close').addEventListener('click', () => opsPanel.classList.add('hidden'));
document.getElementById('refresh-btn').addEventListener('click', () => notes.refresh());

// ── Boot ──
notes.refresh();
