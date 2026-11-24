// The one data stream: /api/events (SSE). Every commit landing arrives as
// `doc-changed`; op-level changes as `view-changed`; derived-index rebuilds
// as `index-changed`. EventSource auto-reconnects; we surface open/error so
// callers can resync heads after a gap.

export function connectEvents({ onDoc, onView, onIndex, onState }) {
  const source = new EventSource('/api/events');
  source.addEventListener('hello', () => onState?.('live'));
  source.addEventListener('doc-changed', (event) => onDoc?.(JSON.parse(event.data)));
  source.addEventListener('view-changed', (event) => onView?.(JSON.parse(event.data)));
  source.addEventListener('index-changed', (event) => onIndex?.(JSON.parse(event.data)));
  source.onopen = () => onState?.('live');
  source.onerror = () => onState?.('offline');
  return source;
}
