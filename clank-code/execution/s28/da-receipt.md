# S28 da receipt — Danish catalog completion

Date: 2026-09-26
Locale: **da (Dansk)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 da translation author (Sol-only rule revoked; authorized)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| Translated | ~4800 | **6123** |
| English-identical fallback | 4822 | **3499** |
| Priority S27 keys (`treehouse.*` / `award.*` / `docs.*`) | 356 English | **352 authored / 4 intentional** |

### What was authored this session

1. **352 S27 structured-prose keys** (priority):
   - `award.oc.*` — 74 achievement titles + summaries (playful names rendered as natural DA achievement titles)
   - `docs.openclank-docs-*.title/.body` — 22 handbook pages (Markdown structure preserved: headings, tables, lists, `[[wikilinks]]`, `[text](clank://…)` app links, fenced code)
   - `treehouse.class.*` / `treehouse.section.*` / `treehouse.manifest.*` — 15 class/section/manifest strings
   - `treehouse.lesson.*` — 235 lesson fields across 30 lessons (body, explanation, title, result, whyThisHelps, practice.*)
2. **~1020 UI labels/messages** (`ui.*`): short labels, dialogs, errors, settings copy, long help prose — course section titles, add/edit/delete flows, provider/connection copy, import/export messages, `Failed to …` error family, file/folder actions, filter and editor labels.

### Intentionally unchanged (exempt: code / brands / cognates / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / style strings | ~450 | not user prose |
| Code identifiers, shell/SQL/regex fragments | ~200 | code exemption |
| Garbled / binary-looking fixtures | ~160 | not translatable content |
| Locked brands / effect names / protocol tokens | rest | `glossary.json` + `brands.json` lock (Clanker effect names, Field Guide, Meatbag Tasks, Copal, etc.) |
| `treehouse.lesson.*.practice.seed` (3) | 3 | fixture seeds (Python sample, YAML props, Markdown table) — user content / code |
| `docs.openclank-docs-home.title` | 1 | brand title "Open Clank Handbook" kept as product name |
| Format-only / metric / identifier tokens (`4K — 3840 × 2160`, `/1k`, `A–Z`) | rest | technical constants |
| HTML-entity fragments and pure tokens | rest | not prose |

House terms consistent with existing da.json + glossary:
Workspace→arbejdsområde, Location→Location, document→dokument, chat→chat,
Memory→hukommelse (product Memory kept), lesson→lektion, Class→Class,
Base→Base, Timeline→Timeline, Graph→Graph, Wiki→Wiki, template→skabelon,
skill→færdighed, provider→udbyder, task→opgave, note→note, file→fil,
folder→mappe, vault→hvælving/vault, token→token, checkpoint→checkpoint,
achievement→præstation, scoped export→afgrænset eksport, manifest→manifest.
Voice: concise labels; complete sentences for errors/help (matches file).

Locked brands kept untranslated: Open Clank, OpenClank, Copal, Imps, Lore,
TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo, Field Guide,
Meatbag Tasks, plus provider brands (Anthropic, Claude, Ollama, GitHub, …).

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` preserved, none invented) |
| Locked-brand drops (word-boundary) | **0** (fixed one `Lore` possessive → `materialet fra Lore`) |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |

---

## Ambiguity / director notes

- **`award.oc.return-of-the-byte.title`** — pun "Return of the Byte"; rendered **« Byttens tilbagevenden »**.
- **`award.oc.same-clank-new-digs.title`** — brand wordplay on Clanker; kept **Clank** intact (« Samme Clank, nye gemakker »).
- **`award.oc.nothing-up-sleeve.summary`** — `rich/source/rich` is a mode-cycle token; left as literal tokens.
- **`docs.openclank-docs-editor` / `…-graph`** — these pages have `.body` only (no `.title` key in `en.json`); no invented keys.
- **`ui.what.does`**-style fragments and `ui.head` (`head —`) left conservative.
- Residual 3495 English-identical `ui.*` values are the exempt classes above (CSS, code, brands, cognates, fixtures, metric labels). Follow-on slice can target any subset if director wants stricter localization.

---

## Evidence index

| Evidence | Path |
| --- | --- |
| Updated catalog | `worktree/static/i18n/da.json` |
| Analysis scratch | `worktree/.s28-da-scratch/` (not committed) |
| This receipt | `worktree/execution/s28/da-receipt.md` |
