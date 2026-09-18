const MAX_EXTERNAL_DROP_PATH_BYTES = 1024;

export function safeExternalDropSegment(value) {
  const segment = String(value || '');
  if (!segment || segment === '.' || segment === '..' || segment.includes('/') || segment.includes('\\') || segment.includes('\0')) return null;
  if (new TextEncoder().encode(segment).byteLength > 240) return null;
  return segment;
}

export function safeExternalDropPath(parts) {
  const safeParts = parts.map(safeExternalDropSegment);
  if (safeParts.some((part) => !part)) return null;
  const relativePath = safeParts.join('/');
  if (!relativePath || new TextEncoder().encode(relativePath).byteLength > MAX_EXTERNAL_DROP_PATH_BYTES) return null;
  return relativePath;
}

export function applyRectangleSelection(baseSelection = [], hitKeys = [], { toggle = false } = {}) {
  const base = new Set(baseSelection);
  for (const key of new Set(hitKeys)) {
    if (toggle && base.has(key)) base.delete(key);
    else base.add(key);
  }
  return [...base];
}
