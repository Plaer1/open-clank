# S28 fr receipt — French catalog completion

Date: 2026-09-26
Locale: **fr (Français)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 fr translation author (manual resume from dirty worktree)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 2226 | **1890** |
| `treehouse.*` / `award.*` / `docs.*` English | 2 (code seeds) | **2 (code seeds — intentional)** |
| UI keys translated this session | — | **336** |

Prior work in the dirty worktree had already translated ~2561 lines (treehouse/award/docs
cohort complete + large UI batch). This session finished the remaining English-identical
UI prose keys that are real user-facing French content.

### What was authored this session (336 `ui.*` keys)

- **Search / Select / Show / Sort** labels and placeholders (~80 keys)
- **System / Session / Security / Skill / Timeline / Track / Trust** settings copy
- **Error / status / empty-state messages** (`Unknown …`, `… unavailable`, `… failed`,
  `This folder is empty`, `Unsaved changes`, `Trash is empty`, etc.)
- **Long help prose** (workspace limits, agent tool toggles, track hierarchy, field-guide
  references, provider sign-in copy) — natural `vous`-form French consistent with existing
  `fr.json`
- **Action buttons / confirmations** (`Yes, delete remaining data`, `Withdraw from course`,
  `Submit evidence`, `Undo last operation`, `Verify now`, …)

### Intentionally unchanged (exempt: code / brands / cognates / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / style strings | ~210 | not user prose |
| Code identifiers (`ADD_TOAST`, `html`, `json`, `sql`, `ul`, `min(700px, 95vw)`, …) | ~80 | code exemption |
| Locked brands (Open Clank, Copal, Clanker, Clanker Light, Hugging Face, …) | ~30 | `brands.json` lock |
| Valid FR↔EN cognates (`exact`, `important`, `migration`, `Performance`, `Suggestions`, `visible`, `URL`, `Max`, …) | ~20 | already correct French |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) |
| Format-only / metric / identifier tokens | rest | technical constants |

House terms consistent with existing fr.json: Workspace→espace de travail, document→document,
chat→conversation/chat (matching file), Memory→mémoire, lesson→leçon, Base→Base,
Timeline→chronologie, Graph→graphe, Wiki→Wiki, template→modèle, skill→compétence,
provider→fournisseur, task→tâche, note→note, file→fichier, folder→dossier,
vault→coffre, token→jeton, permission→permission/autorisations. Voice: `vous` form;
`fr.json`-style narrow no-break space (`\xa0`) before `:` `?` `!` where established.

Locked brands kept untranslated: Open Clank, Copal, Imps, Lore, TreeHouse, Menmery,
LCARS, Clanker, plus provider brands (Anthropic, Claude, Ollama, …).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` preserved, none invented) |
| Locked-brand drops (word-boundary) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |

---

## Ambiguity / director notes

- **`ui.open.clanker.tasks`** — English "Open Clanker Tasks" = verb *Open* + product
  *Clanker Tasks*. Rendered **« Ouvrir Clanker Tasks »** (brand intact).
- **`ui.what.is.open.clank.local.first.ai.assistant.multi.provider`** — "AI" kept as
  **AI** (not "IA"), matching existing fr.json convention for product-surface AI.
- **`ui.yours.for.the.clanking`** — "clanking" is brand-voice wordplay tied to Clanker;
  kept as **« clanking »** to avoid inventing a non-existent French coinage.
- **`ui.what.does`** value is a fragment (`', 'what does`); translated the prose part
  (`que fait`) and preserved surrounding punctuation/prefix exactly.
- **`ui.head`** (`head —`) and **`ui.file.e1ae90fc`** (`file.`) are truncated fragments
  paired with surrounding UI; translated conservatively (`en-tête —`, `fichier.`).
- Residual 1890 English-identical values are the exempt classes above (CSS, code,
  brands, cognates, fixtures). Follow-on slice can target any subset if director wants
  stricter localization (e.g. bare tokens like `form`, `if`, `do`).

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/fr.json` |
| Analysis scratch | `worktree/.s28-fr-scratch/` (not committed) |
| This receipt | `worktree/execution/s28/fr-receipt.md` |
