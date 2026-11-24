export function folderChangeLabel(item) {
  const label = { added: 'Added', changed: 'Changed', removed: 'Removed' }[item.state];
  const date = new Date(item.at_ms);
  const when = item.at_ms && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Time unavailable';
  const size = Number.isSafeInteger(item.size_bytes) ? ` · ${item.size_bytes} bytes` : '';
  return `${label} · ${item.name} · ${when}${item.action ? ' · ' + item.action : ''}${size}`;
}

export function validFolderChanges(value) {
  return value?.version === 1 && value.scope === 'one_open_folder'
    && Array.isArray(value.items) && value.items.length <= 50
    && value.items.every(item => typeof item?.name === 'string' && ['added', 'changed', 'removed'].includes(item.state)
      && /^resource-[0-9a-f]{64}$/.test(item.resource_id || '') && Number.isSafeInteger(item.at_ms ?? 0));
}
