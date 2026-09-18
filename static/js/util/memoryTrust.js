// Memory trust + signal presentation helpers (memory-trust metaplan
// T2/T5/T7). Pure functions — mirrored from src/memory_trust.py; keep the
// two in lockstep. Node-testable (no DOM).

export const BEHAVIOR_KINDS = ['instruction', 'persona'];
export const KNOWLEDGE_KINDS = ['fact', 'episodic', 'fabric', 'wiki'];
export const TRUSTABLE_KINDS = [...BEHAVIOR_KINDS, ...KNOWLEDGE_KINDS];

// T7 defaults: flipping the master trusts auto-captured knowledge;
// auto-captured behavior needs a second, deliberate flip.
export const DEFAULT_KIND_TRUST = {
  instruction: false,
  persona: false,
  fact: true,
  episodic: true,
  fabric: true,
  wiki: true,
};

export function trustPrefs(prefs) {
  const safe = prefs && typeof prefs === 'object' ? prefs : {};
  // Persisted preferences are a typed contract.  Boolean("false") is true,
  // so malformed values must fail closed instead of silently changing the
  // injection policy.
  const master = typeof safe.memory_trust_auto === 'boolean'
    ? safe.memory_trust_auto
    : false;
  const kinds = { ...DEFAULT_KIND_TRUST };
  const raw = safe.memory_trust_auto_kinds;
  if (raw && typeof raw === 'object') {
    for (const [kind, value] of Object.entries(raw)) {
      if (kind in kinds) kinds[kind] = typeof value === 'boolean' ? value : false;
    }
  }
  return { master, kinds };
}

// Mirror of src/memory_trust.trusted(): human always; explicit pin always;
// otherwise master AND kind switch; raw/unknown never. Absent fields fail
// closed.
export function isTrusted(record, prefs) {
  if (!record || typeof record !== 'object') return false;
  if (String(record.source_type || '') === 'human') return true;
  if (record.pinned === true) return true;
  const kind = String(record.kind || '');
  if (!TRUSTABLE_KINDS.includes(kind)) return false;
  const { master, kinds } = trustPrefs(prefs);
  return master && Boolean(kinds[kind]);
}

// T5: bucketed chip text, raw float on hover (caller puts raw in title).
export function scoreBucket(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return null;
  if (v < 0.34) return 'low';
  if (v < 0.67) return 'med';
  return 'high';
}

const PROVENANCE_LABEL = {
  human: 'human',
  ai: 'ai',
  auto_extracted: 'auto',
  procedural: 'procedural',
};

// Chip data for one memory card: [{label, cls, title}] — presentation
// only, no DOM. Chips appear only when they carry information (defaults
// and empty fields render nothing).
export function memoryChips(record, prefs) {
  if (!record || typeof record !== 'object') return [];
  const chips = [];

  chips.push(
    isTrusted(record, prefs)
      ? { label: 'trusted', cls: 'trusted', title: 'Carries force at injection (endorsed by you or by your trust toggles)' }
      : { label: 'reference', cls: 'reference', title: 'Shown to the AI as untrusted reference data only' },
  );

  const kind = String(record.kind || '');
  if (kind === 'unknown') {
    // Open questions read as what they are, not as "kind: unknown".
    chips.push({ label: 'open question', cls: 'kind kind-unknown', title: 'Open question you want answered — the AI weaves it into relevant conversations' });
  } else if (kind && kind !== String(record.category || '')) {
    chips.push({ label: kind, cls: 'kind', title: `Engine kind: ${kind}` });
  }

  const provenance = PROVENANCE_LABEL[String(record.source_type || '')];
  if (provenance) {
    chips.push({ label: provenance, cls: 'provenance', title: `Captured by: ${record.source_type}${record.source ? ` (via ${record.source})` : ''}` });
  }

  const workspace = String(record.workspace_id || '');
  if (workspace && workspace !== 'global') {
    chips.push({ label: workspace, cls: 'workspace', title: `Scoped to workspace ${workspace}` });
  }

  if (record.archived) chips.push({ label: 'archived', cls: 'archived', title: 'Archived — excluded from recall' });
  if (record.exempt_from_decay) chips.push({ label: 'no-decay', cls: 'exempt', title: 'Exempt from decay' });
  if (record.exempt_from_dedup) chips.push({ label: 'no-dedup', cls: 'exempt', title: 'Exempt from dedup' });

  // Trust is the one user-facing epistemic measure. A durable owner
  // assignment is authoritative; legacy trust_score is intentionally not
  // promoted into an assignment and is shown only as a compatibility signal
  // when no assignment is available. Producer confidence is technical
  // quality, not “how much I trust this information”, so it is labeled
  // quality rather than surfaced as a second epistemic score.
  const assignment = record.trust && typeof record.trust === 'object'
    ? record.trust
    : null;
  const assignedValue = assignment && assignment.state === 'assigned'
    ? assignment.value
    : null;
  if (assignedValue !== null && Number.isFinite(Number(assignedValue))) {
    const bucket = scoreBucket(assignedValue);
    chips.push({
      label: `trust:${bucket}`,
      cls: 'trust-assignment',
      title: `Owner Trust assignment: ${Number(assignedValue).toFixed(3)}${assignment.actor_id ? ` by ${assignment.actor_id}` : ''}${assignment.reason_code ? ` (${assignment.reason_code})` : ''}`,
    });
  } else {
    chips.push({
      label: 'trust:unreviewed',
      cls: 'trust-assignment',
      title: 'No attributable owner Trust assignment; producer signals cannot set Trust',
    });
  }

  for (const [key, label] of [['confidence_score', 'quality'], ['importance_score', 'importance']]) {
    const bucket = scoreBucket(record[key]);
    if (bucket !== null) {
      chips.push({
        label: `${label}:${bucket}`,
        cls: `score score-${bucket}`,
        title: `${label === 'quality' ? 'technical producer quality (legacy confidence_score)' : 'importance score'}: ${Number(record[key]).toFixed(3)}`,
      });
    }
  }

  return chips;
}
