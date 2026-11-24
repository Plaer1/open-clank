// Read-only shadow ranking block. Numbers and fixed sentences only; it observes and never changes recall.
export function mountShadowRanking(container) {
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = 'Shadow ranking';
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
  const num = (v) => Number(v) || 0;
  const pct = (v) => `${(num(v) * 100).toFixed(1)}%`;

  function show(d) {
    const lines = [
      `Recall items observed: ${num(d.receipts)}`,
      `With a helped / not helped mark: ${num(d.labelled)} (helped ${num(d.positive)}, not helped ${num(d.negative)})`,
    ];
    if (d.truncated) lines.push('Only part of the history was read.');
    const names = Object.keys(d.uninformative_features || {});
    if (names.length) lines.push(`These signals did not vary and carry no information yet: ${names.join(', ')}`);
    const a = d.agreement;
    if (a) {
      lines.push(`Ordering pairs compared: ${num(a.mixed_pairs)}`,
        `Shadow ordering got ${pct(a.shadow_pair_accuracy)} of them right; the delivered ordering got ${pct(a.delivered_pair_accuracy)}.`,
        'This compares orderings only. It does not show that recall would be better.');
    } else {
      lines.push(`Not enough marked items to compare yet (needs at least ${num(d.min_labelled)}).`);
    }
    say(...lines);
  }

  let seq = 0;
  async function load() {
    const mine = ++seq;
    say('Loading…');
    let data = null;
    try {
      const res = await fetch('/api/memory/shadow-ranking/report', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      data = res.ok ? await res.json() : null;
    } catch (_) { data = null; }
    if (mine !== seq) return;
    if (data?.state === 'off') return say('Shadow ranking is off. It only observes; it never changes what is recalled.');
    if (data && ['ok', 'no_receipts', 'insufficient_labels'].includes(data.state)) return show(data);
    say('Shadow ranking is not available on this memory store.');
  }

  details.addEventListener('toggle', () => { if (details.open) load(); });
  refresh.addEventListener('click', load);
}
