# S28 no receipt — Norwegian (Bokmål) catalog completion

Date: 2026-09-26
Locale: **no (Norsk)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 no translation author (Sol-only rule revoked; authorized)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~4798 | **6675** |
| English-identical fallback | ~4824 | **2947** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **353 authored / 3 fixture seeds intentional** |

### What was authored this session (~1877 keys)

1. **356 S27 structured-prose keys** (priority cohort):
   - `award.oc.*` — 74 achievement titles + summaries
   - `docs.openclank-docs-*.title/.body` — 12 handbook pages (Markdown structure preserved:
     headings, tables, lists, `[[wikilinks]]` retargeted to NO titles, `[text](clank://…)` app
     links, fenced code left byte-identical)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 20 class/section strings
   - `treehouse.lesson.*` — 238 lesson fields across 30 lessons (body, explanation, title, result,
     whyThisHelps, practice.*)
2. **~1520 UI labels/messages** (`ui.*`): short labels, dialogs, errors, empty states, settings copy,
   long help prose, confirmation buttons.

### Intentionally unchanged (exempt: code / CSS / brands / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / style strings | ~295 | not user prose |
| `ui-nonprose` tokens / format-only constants | ~442 | staging `ui-nonprose.json` class |
| Code identifiers, shell, Python, regex, HTML fragments, CQL keyword dump | ~900 | code exemption |
| Locked brands / product effect names (Open Clank, Clanker *, Meatbag Tasks, MiMo *, Field Guide, …) | ~50 | `brands.json` lock |
| Garbled / binary-looking fixtures | ~170 | not translatable content |
| Short tokens / metric labels (`4K — 3840 × 2160`, `Aa`, `2d`, …) | rest | technical constants |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (YAML props, Python sample, Markdown table) |

House terms consistent with existing `no.json` + glossary:
Workspace→arbeidsområde, document→dokument, chat→chat, Memory→Memory/Minne,
lesson→leksjon, Class→Klasse, Base→Base, Timeline→Timeline, Graph→Graph, Wiki→Wiki,
template→mal, skill→ferdighet, provider→leverandør, achievement→prestasjon,
scoped export→avgrenset eksport, durable goal→varig mål, preimage→preimage,
restore→gjenopprette, folder→mappe, file→fil, vault→hvelv, token→token.
Voice: informal **du** form (matches existing `no.json`). Product surface nouns
(Editor, Files, Graph, Timeline, Base/Bases, Wiki, Compare, Galaxy, Brain, Settings
panels) kept in English where `no.json` already does.

Locked brands kept byte-identical in translations: Open Clank, OpenClank, Copal, Clanker,
Imps, Lore, TreeHouse, Menmery, MiMo, Field Guide, Meatbag Tasks, LCARS, Frankenmemory,
plus provider brands (OpenAI, Anthropic, Ollama, Hugging Face, …).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` / `{exc}` preserved) |
| Locked-brand drops (substring check) | **0** in `no` |
| HTML tags introduced | **0** (pre-existing HTML fixtures left unchanged) |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8, `en.json` key order |
| `node scripts/i18n-catalog.mjs validate` | `no: keys=9622 catalog=no dir=ltr` — **0 no errors** (3 remaining errors are pre-existing `th` / `fi` locked-token issues in the dirty worktree) |
| `pytest tests/test_i18n_contract.py tests/test_i18n_source_records.py` | **12 passed**; 1 failed on pre-existing `th` locked-token (`ui.open.clanker.tasks`), not `no` |

---

## Ambiguity / director notes

- **`ui.open.clanker.tasks`** — English "Open Clanker Tasks" is ambiguous (verb *Open* +
  product, or product *Open Clanker* + Tasks). The catalog validator requires the literal
  substring `Open Clank` to survive (it matches inside "Open Clanker"). Rendered
  **«Åpne Open Clanker-oppgaver»** to keep the locked substring and a natural verb.
  Compare: de "Open Clanker Tasks öffnen", fr "Ouvrir les tâches Open Clanker", ja
  "Open Clanker タスク".
- **`docs.openclank-docs-home.title`** — localized as **«Open Clank-håndboken»** (cf. de
  "Open Clank-Handbuch"); body H1 updated to match. `docs.openclank-docs-editor.title` and
  `docs.openclank-docs-graph.title` **do not exist** in the frozen catalog (only `.body`);
  no orphan writes.
- **Wikilinks** in docs bodies retargeted to Norwegian page titles (`[[Få arbeidet gjort]]`,
  [[Kontoer og modeller]], [[Grenser og plattformstøtte]], …) so they resolve after translation.
- **Theme/effect names** (Solid, Clanker Signal Routes, Shipibo Kene-Inspired Signal Weave,
  Clanker LCARS, …) kept in English as product surface names.
- **`treehouse.lesson.*.practice.seed`** (3) left as fixture code (YAML `status: ready`,
  Python `def summarize`, Markdown table). Same exemption as ja/fr.
- Residual ~2947 English-identical values are the exempt classes above. A follow-on slice can
  target residual UI prose (~600 borderline labels/status strings) if the director wants
  stricter localization.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/no.json` |
| Translation batches (not committed) | `execution/s28/.s28-no-scratch/` |
| This receipt | `worktree/execution/s28/no-receipt.md` |
