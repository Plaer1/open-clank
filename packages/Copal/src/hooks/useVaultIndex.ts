'use client';

import { useEffect, useMemo, useState } from 'react';
import { buildVaultIndex, type VaultIndex, type VaultNote, type VaultNoteEntry } from '@/lib/notes';

interface NotesResponse {
  vaultPath: string;
  notes: VaultNoteEntry[];
}

async function fetchNotes(): Promise<NotesResponse> {
  const res = await fetch('/api/notes');
  if (!res.ok) throw new Error(`notes request failed: ${res.status}`);
  return res.json();
}

async function fetchNote(path: string): Promise<VaultNote> {
  const res = await fetch(`/api/note?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`note request failed: ${res.status}`);
  return res.json();
}

export function useVaultIndex() {
  const [vaultPath, setVaultPath] = useState('');
  const [notes, setNotes] = useState<VaultNote[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  async function refresh() {
    setLoading(true);
    setError('');
    try {
      const payload = await fetchNotes();
      setVaultPath(payload.vaultPath);
      const loaded = await Promise.all(payload.notes.map((note) => fetchNote(note.path)));
      setNotes(loaded);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'vault index request failed');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const index: VaultIndex = useMemo(() => buildVaultIndex(notes), [notes]);

  return { vaultPath, notes, index, loading, error, refresh };
}
