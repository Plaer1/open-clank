export interface NoteJump {
  path: string;
  line?: number;
}

export function openNoteAt(path: string, line?: number) {
  const params = new URLSearchParams();
  params.set('note', path);
  if (line && line > 0) params.set('line', String(line));
  window.location.hash = params.toString();
}

export function readNoteJumpFromHash(hash: string): NoteJump | null {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!raw) return null;
  const params = new URLSearchParams(raw);
  const path = params.get('note');
  if (!path) return null;
  const lineRaw = params.get('line');
  const line = lineRaw ? Number(lineRaw) : undefined;
  return { path, line: Number.isFinite(line) ? line : undefined };
}
