// DocSync — the write pipeline client (DB metaplan §1, recanonized):
//
//   - batch keystrokes: 1.11 s after the LAST edit, POST the full content
//   - every accepted write is an amend commit; response carries the new head
//   - stale baseCommit (another tab landed first): adopt the authoritative
//     head from the response, then resend our content once (last-writer-wins
//     at batch granularity — server serializes)
//   - remote `doc-changed` events: apply into the editor when we have no
//     pending local edits; if we do, our next send resolves it (LWW)
//
// The head commit id is the only revision concept.

import { writeDoc } from './api.js';

export const SNAPSHOT_IDLE_MS = 1110;

// One client token per tab: lets us skip our own SSE echo.
export const CLIENT_ID = (crypto.randomUUID?.() ?? String(Math.random())).slice(0, 12);

export function createDocSync({ getContent, setContent, onStatus }) {
  let docId = null;
  let head = null;      // head commit id we believe in
  let timer = null;
  let dirty = false;    // local edits not yet sent
  let sending = false;

  function status(text, extra = {}) {
    onStatus?.({ text, head, dirty, ...extra });
  }

  async function send() {
    if (!docId || sending) return;
    sending = true;
    dirty = false;
    status('saving…');
    const content = getContent();
    const res = await writeDoc({ id: docId, content, baseCommit: head, client: CLIENT_ID });
    sending = false;
    if (res.ok && (res.body.outcome === 'committed' || res.body.outcome === 'unchanged')) {
      head = res.body.doc.head;
      status(res.body.outcome === 'committed' ? 'saved' : 'idle');
    } else if (res.status === 409 && res.body.outcome === 'stale') {
      // Rebase: adopt the authoritative head, then resend our content once.
      head = res.body.doc.head;
      status('rebased');
      const again = await writeDoc({ id: docId, content: getContent(), baseCommit: head, client: CLIENT_ID });
      if (again.ok) {
        head = again.body.doc.head;
        status('saved');
      } else {
        status('save failed', { error: true });
      }
    } else {
      dirty = true; // retry on next edit
      status('save failed', { error: true });
    }
    if (dirty) schedule();
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(send, SNAPSHOT_IDLE_MS);
  }

  return {
    clientId: CLIENT_ID,

    open(id, docHead) {
      if (timer) clearTimeout(timer);
      docId = id;
      head = docHead;
      dirty = false;
      status('idle');
    },

    close() {
      if (timer) clearTimeout(timer);
      docId = null;
      head = null;
    },

    /** Call on every editor input. */
    edited() {
      dirty = true;
      status('editing…');
      schedule();
    },

    /** Remote doc-changed event for the open doc. */
    applyRemote(doc) {
      if (doc.id !== docId) return false;
      if (doc.client === CLIENT_ID) {
        // Our own echo — head already updated from the POST response.
        return false;
      }
      if (dirty || sending) {
        // Local edits in flight; our next send rebases (LWW). Just track head.
        head = doc.head;
        return false;
      }
      head = doc.head;
      setContent(doc.text ?? '');
      status('synced from another tab');
      return true;
    },

    /** After an SSE gap: caller refetches the doc and hands it here. */
    resync(doc) {
      head = doc.head;
      if (!dirty && !sending) setContent(doc.text ?? '');
      status('resynced');
    },

    get head() { return head; },
    get docId() { return docId; },
    get dirty() { return dirty; },
  };
}
