// Read-only journal coverage block (BQ-8). Counts of the NEXT page only; nothing here records or moves coverage. "Covered" is not
// claimed: coverage is acknowledged only by a recorded proposal or an explicit no-finding for the exact selection.
const LINES = [
  ['included', 'Ready for review'],
  ['already_covered', 'Already covered by an earlier review'],
  ['superseded', 'Superseded by a later revision'],
  ['omitted', 'Recorded, not processed (tool, media or unpaired assistant text)'],
  ['gap', 'Blocking: unresolved'],
];

export function mountJournalCoverage(container, chatId) {
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = 'Journal coverage';
  const body = document.createElement('div');
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.textContent = 'Refresh';
  details.append(summary, body, refresh);
  container.replaceChildren(details);

  const say = (...lines) => body.replaceChildren(...lines.map((text) => {
    const row = document.createElement('div');
    row.textContent = text;
    return row;
  }));

  let seq = 0;
  async function load() {
    const mine = ++seq;
    const chat = typeof chatId === 'function' ? chatId() : chatId;
    if (!chat) return say('Open a chat to see its journal coverage.');
    say('Loading…');
    let data = null;
    try {
      const res = await fetch('/api/memory/journal-coverage', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: String(chat) }),
      });
      data = res.ok ? await res.json() : null;
    } catch (_) { data = null; }
    if (mine !== seq) return;
    if (data?.state === 'off') return say('Journal coverage is off. Turn on memory consolidation to use it.');
    if (data?.state === 'ok' && data.counts) {
      const lines = LINES.filter(([key]) => key === 'included' || Number(data.counts[key]) > 0).map(([key, label]) => `${label}: ${Number(data.counts[key]) || 0}`);
      lines.push(`Acknowledged up to ${Number(data.ack_seq) || 0}; next page ends at ${Number(data.end_seq) || 0}`);
      if (Number(data.blocking_gaps) > 0) lines.push('A blocking gap keeps this page from being acknowledged until it is resolved.');
      return say(...lines);
    }
    say('Journal coverage is not available on this memory store.');
  }

  details.addEventListener('toggle', () => { if (details.open) load(); });
  refresh.addEventListener('click', load);
}
