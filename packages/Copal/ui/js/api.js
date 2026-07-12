// Fetch client for the Copal native API (DB-backed doc endpoints).

async function json(res) {
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body };
}

export async function listDocs() {
  return json(await fetch('/api/docs'));
}

export async function getDoc(id) {
  return json(await fetch(`/api/doc?id=${encodeURIComponent(id)}`));
}

// Write = amend commit server-side. baseCommit is the head we believe in;
// a 409 "stale" response carries the authoritative doc to rebase onto.
export async function writeDoc({ id, content, baseCommit, client }) {
  return json(await fetch('/api/doc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, content, baseCommit, client }),
  }));
}

export async function createDoc({ name, kind, content, client }) {
  return json(await fetch('/api/doc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, kind, content, client }),
  }));
}

export async function docHistory(id) {
  return json(await fetch(`/api/doc/history?id=${encodeURIComponent(id)}`));
}

export async function listOps(limit = 30) {
  return json(await fetch(`/api/ops?limit=${limit}`));
}

export async function undo(op) {
  return json(await fetch('/api/undo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(op ? { op } : {}),
  }));
}

export async function importVault(path) {
  return json(await fetch('/api/import/vault', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(path ? { path } : {}),
  }));
}
