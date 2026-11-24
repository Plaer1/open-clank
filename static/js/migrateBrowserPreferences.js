// Run before first paint or account detection so every consumer sees the same settings.
(() => {
  const marker = 'openclank-browser-namespace-v1';

  function replacement(key) {
    if (key.startsWith('odysseus-mimo-')) return 'openclank-agent-' + key.slice('odysseus-mimo-'.length);
    if (/^odysseus[-.:_]/.test(key) || key === 'odysseusArrowRecallDebug') {
      return 'openclank' + key.slice('odysseus'.length);
    }
    return null;
  }

  function migrate(storage) {
    if (storage.getItem(marker)) return;
    const keys = Array.from({ length: storage.length }, (_, i) => storage.key(i));
    let complete = true;
    for (const key of keys) {
      if (!key) continue;
      const target = replacement(key);
      if (!target) continue;
      try {
        const value = storage.getItem(key);
        if (value === null) continue;
        if (storage.getItem(target) === null) storage.setItem(target, value);
        storage.removeItem(key);
      } catch {
        // Retain the original and retry once storage becomes writable.
        complete = false;
      }
    }
    if (complete) storage.setItem(marker, '1');
  }

  for (const area of ['localStorage', 'sessionStorage']) {
    try { migrate(window[area]); } catch { /* Storage can be disabled by the browser. */ }
  }
})();
