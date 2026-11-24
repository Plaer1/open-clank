'use client';

import { useEffect, useRef } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { fmtDate } from '@/lib/dates';

/**
 * Persistence to the on-disk JSON file via the tiny Python server (app.py).
 *
 *  - On mount: GET /api/data. If it has tracks, that becomes the source of truth
 *    (overriding the bundled seed). This is what lets you hand-edit move-data.json
 *    and see your changes, or let an AI calendar manager rewrite it.
 *  - After load: any change to `data` is debounced and POSTed back to /api/data,
 *    so in-app edits (add/edit/delete) are saved to disk immediately.
 *
 * Reliability note: this runs over http://localhost served by app.py, which is
 * exactly why we use a real server instead of relying on flaky file:// storage.
 */
const API = '/api/data';
const SAVE_DEBOUNCE_MS = 400;

export function usePersistence() {
  const loadedRef = useRef(false);
  const lastSavedRef = useRef<string>('');

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    // 1) Load the authoritative data from disk.
    fetch(API)
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => {
        if (cancelled) return;
        if (json && Array.isArray(json.tracks) && json.tracks.length) {
          // `today` is never restored from disk — it's always the real current
          // date. We also overwrite json.today so the saved file stops carrying
          // the stale baked-in value.
          const realToday = fmtDate(new Date());
          useMoveStore.setState({
            data: { ...json, today: realToday },
            today: realToday,
          });
          lastSavedRef.current = JSON.stringify({ ...json, today: realToday });
        }
      })
      .catch(() => {
        /* keep bundled seed */
      })
      .finally(() => {
        loadedRef.current = true;
      });

    // 2) Autosave whenever `data` actually changes (after the initial load).
    const unsub = useMoveStore.subscribe((state) => {
      if (!loadedRef.current) return;
      const serialized = JSON.stringify(state.data);
      if (serialized === lastSavedRef.current) return;
      lastSavedRef.current = serialized;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        fetch(API, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: serialized,
        }).catch(() => {
          /* offline / server down — edits stay in memory */
        });
      }, SAVE_DEBOUNCE_MS);
    });

    return () => {
      cancelled = true;
      unsub();
      if (timer) clearTimeout(timer);
    };
  }, []);
}
