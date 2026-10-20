const JSON_HEADERS = Object.freeze({ 'Content-Type': 'application/json' });

async function request(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json())?.detail || ''; } catch { /* response text is not trusted UI */ }
    throw new Error(detail || 'Library operation failed');
  }
  return response;
}

export async function resolveLibraryDocument(resourceRef) {
  const response = await request('/api/documents/resolve-resource', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ resource_ref: resourceRef }),
  });
  const documentDto = await response.json();
  if (!String(documentDto?.id || '').trim()) throw new Error('Files did not resolve a Library document');
  return documentDto;
}

export async function createLibraryDocument({ sessionId = null } = {}) {
  const response = await request('/api/document', {
    method: 'POST', headers: JSON_HEADERS,
    body: JSON.stringify({ title: 'Untitled', language: 'markdown', content: '', session_id: sessionId || null }),
  });
  return response.json();
}

export async function importLibraryDocuments(files) {
  const library = await import('./documentLibrary.js');
  return library.libraryImportFiles(files, { refresh: false });
}

export async function cloneLibraryDocument(resourceRef, { sessionId = null } = {}) {
  const source = await resolveLibraryDocument(resourceRef);
  const response = await request('/api/document', {
    method: 'POST', headers: JSON_HEADERS,
    body: JSON.stringify({
      title: source.title || 'Untitled',
      language: source.language || 'markdown',
      content: source.current_content || source.content || '',
      session_id: sessionId || null,
    }),
  });
  return response.json();
}

export async function deleteLibraryDocument(resourceRef) {
  const source = await resolveLibraryDocument(resourceRef);
  await request('/api/document/' + encodeURIComponent(source.id), { method: 'DELETE' });
  return source;
}
