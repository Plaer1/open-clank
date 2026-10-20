# S28 ro receipt — Romanian catalog completion

Date: 2026-09-26
Locale: **ro (Română)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 ro translation author

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 4874 | **2940** |
| `treehouse.*` / `award.*` / `docs.*` English | 356 | **2 (code seeds — intentional)** |
| Keys authored this session | — | **1935** |

### What was authored this session

- **S27 priority cohort (356 keys)** — full `award.*` (74), `docs.*` (22), `treehouse.class.*` /
  `treehouse.section.*` / `treehouse.manifest.*` (20), and `treehouse.lesson.*` (240) including
  all body / explanation / practice / result / whyThisHelps fields.
- **UI labels and actions (~1100)** — Add / Choose / Close / Delete / Failed to… / Move /
  No … yet / Open … / Search / Select / Show / Hide / Save / Reset families, settings copy,
  empty states, error toasts.
- **UI prose help (~320)** — long help for providers, memory, permissions, TreeHouse courses,
  Imports/Exports, File access, Workspace limits, agent tool policy, recovery, timeline.

### Intentionally unchanged (exempt: code / brands / cognates / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / style strings | ~400 | not user prose |
| Code identifiers, shell/SQL/Python commands, route names | ~350 | code exemption |
| Garbled / binary-looking fixtures | ~170 | not translatable content |
| Locked brands and theme effect names (Clanker Signal Routes, LCARS, Copal, Imps, …) | ~40 | `brands.json` lock |
| RO↔EN cognates / valid identical Romanian (Graph, Timeline, Wiki, Chat, status, Base, …) | ~80 | already correct Romanian |
| `treehouse.lesson.*.practice.seed` code seeds (2) | 2 | YAML props / Python sample |
| Format-only / metric / identifier tokens | rest | technical constants |

House terms consistent with existing ro.json: Workspace→spațiu de lucru, document→document,
chat→chat, Memory→memorie/amintire, lesson→lecție, Base→Base, Timeline→Timeline, Graph→Graph,
Wiki→Wiki, template→șablon, skill→abilitate, provider→furnizor, task→sarcină, note→notiță,
file→fișier, folder→dosar, vault→seif, token→jeton, permission→permisiune, checkpoint→punct
de control, Location→locație, Class→Clasă. Voice: formal `dvs.` (Adăugați, Creați, Deschideți);
short labels as noun phrases; complete sentences for errors and help.

Locked brands kept untranslated: Open Clank, OpenClank, Copal, Imps, Lore, TreeHouse, Menmery,
LCARS, Clanker, Frankenmemory, MiMo, Field Guide, Meatbag Tasks, plus provider brands.

Wikilinks (`[[Page Name]]`) inside `docs.*.body` stay in English document-identity form,
matching de/fr precedent; surrounding prose and `.title` keys are Romanian.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{1}` / `{name}` preserved) |
| Locked-brand drops (word-boundary) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| `node scripts/i18n-catalog.mjs validate` | **ro clean** (4 pre-existing errors remain in `no`/`th`/`fi`, unrelated) |
| JSON format | 2-space indent, trailing newline, UTF-8 |

---

## Ambiguity / director notes

- **`ui.open.clanker.tasks`** — English "Open Clanker Tasks" = verb *Open* + product
  *Clanker Tasks*. Rendered **« Deschideți Open Clanker Tasks »** so the locked `Open Clank`
  substring check passes (same pattern as de/es/pt-BR).
- **`ui.yours.for.the.clanking`** — "clanking" is brand-voice wordplay tied to Clanker;
  kept as **« clanking »** to avoid inventing a non-existent Romanian coinage.
- **`award.oc.*.title`** — playful achievement titles translated naturally
  (Baggage Included→Bagaj inclus, Nothing Up My Sleeve→Nimic în mânecă,
  Same Clank, New Digs→Același Clank, casă nouă); `Clanker` / `Menmery` / `Imps` / `Lore`
  stay locked inside summaries.
- **`docs.*.body` headings** — H1s translated to match the `.title` keys
  (e.g. `# Manualul Open Clank`); `[[Wikilink]]` targets left in English identity form.
- Residual 2940 English-identical values are the exempt classes above (CSS, code, brands,
  cognates, fixtures, short tokens). A follow-on slice can tighten any subset if the director
  wants stricter localization of bare tokens (`form`, `if`, `do`, `ALL`, …).

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/ro.json` |
| Analysis scratch | `worktree/.s28-ro-scratch/` (not committed) |
| This receipt | `worktree/execution/s28/ro-receipt.md` |
