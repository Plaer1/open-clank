let storageNamespace = null;

export function configureCopalStorage(namespace) {
  const next = String(namespace || '').trim();
  if (!next) throw new Error('Copal storage scope is unavailable');
  storageNamespace = next;
}

export function copalStorageKey(base, workspace = '') {
  if (!storageNamespace) throw new Error('Copal storage scope is not initialized');
  const suffix = workspace ? `:${workspace}` : '';
  if (storageNamespace === 'local') return `${base}${suffix}`;
  return `${base}:scope:${encodeURIComponent(storageNamespace)}${suffix}`;
}
