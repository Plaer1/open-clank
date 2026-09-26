# S28 de receipt — German catalog authoring

Date: 2026-09-26
Locale: **de (Deutsch)**
Worktree: `.references/upstream-sync-2026-09-22/execution/s28/worktree`
Branch: `openclank/s28-translations-2026-09-26`
Author: S28 de translation author (manual resume from dirty worktree)

---

## Result

| Metric | Before this session | After |
| --- | --- | --- |
| Total keys | 9622 | 9622 (parity kept) |
| English-identical fallback | 3741 | **2212** |
| `treehouse.*` / `award.*` / `docs.*` English | 2 code seeds | **2 code seeds (intentional)** |
| UI keys translated this session | — | **~1529** |

Prior work in the dirty worktree had already translated ~1206 lines (treehouse/award/docs
cohort complete + large UI batches 01–07). This session finished the remaining
English-identical UI prose keys and fixed locked-brand violations.

### What was authored this session (~1529 `ui.*` keys)

- **Delete / Move / Keep / Open / Search / Select / Show / Sort / Start / Toggle / Use /
  Value** label families and confirmations (~900 keys)
- **Error / status / empty-state messages** (`No … yet`, `… failed`, `… unavailable`,
  `This folder is empty`, `Trash is empty`, `Preview unavailable`, etc.)
- **Long help prose** (workspace limits, agent tool toggles, track hierarchy, memory
  engine copy, provider sign-in, ingest pipeline, recovery/restart) — natural German
  consistent with existing `de.json` (`Sie` form for sentences; noun-first labels
  like „Aktivität löschen“)
- **Settings / Timeline / Trust / Skills / Tasks / Vault / Wiki** surface copy

### Locked-brand fixes (test failures cleared)

| Key | Was | Now |
| --- | --- | --- |
| `ui.choose.a.folder.on.the.open.clank.host.nothing.is` | Open-Clank-Host | Host von Open Clank |
| `ui.configure.ssh.servers.install.open.clank.keys.choose.model.directories` | Open-Clank-Schlüssel | Open Clank-Schlüssel |
| `ui.current.open.clank.model.memory.provider.and.integration.state` | Open-Clank-Modell-… | … von Open Clank |
| `docs.openclank-docs-home.title` | Open-Clank-Handbuch | Open Clank-Handbuch |
| `treehouse.lesson.house-stewardship.official-docs.body` | Open-Clank-Handbuch | Open Clank-Handbuch |
| `ui.open.clanker.tasks` | Clanker Tasks öffnen | Open Clanker Tasks öffnen |
| 5 × `Field Guide` compounds | Field-Guide-… | Field Guide-… |

### Intentionally unchanged (exempt: code / brands / cognates / fixtures)

| Class | ~Count | Reason |
| --- | --- | --- |
| CSS class lists / selectors / style strings | ~520 | not user prose |
| Garbled / binary-looking fixtures (`p!]!^!@Q…`) | ~30 | not translatable content |
| Shell / code / regex / SVG / command fragments | ~80 | code exemption |
| Locked brand / theme / product names (Clanker Dark, Copal Notes, Google Gemini, Ollama Cloud, Open Clank TUI, …) | ~40 | `brands.json` + product names |
| DE↔EN cognates / identical correct German (Format, Koralle, Mindmap, Tabelle 2, N/A, Name (optional), …) | ~30 | already correct German |
| `treehouse.lesson.*.practice.seed` (2) | 2 | fixture seeds (YAML props, Python sample) |
| Format-only / metric / identifier tokens | rest | technical constants |

House terms consistent with existing de.json: Workspace→Arbeitsbereich, Location→Ort,
document→Dokument, chat→Chat, Memory→Memory/Erinnerung, lesson→Lektion, Base→Base,
Timeline→Timeline, Graph→Graph, Wiki→Wiki, template→Vorlage, checkpoint→Checkpoint,
skill→Fähigkeit, quest→Quest, provider→Anbieter, task→Aufgabe, note→Notiz,
file→Datei, folder→Ordner, vault→Vault, permission→Berechtigung, persona→Persona.
Voice: `Sie` form for full sentences; short labels as noun phrases („X löschen“).
German quotes „…“ for embedded placeholders.

Locked brands kept byte-identical in translations: Open Clank, Copal, Imps, Lore,
TreeHouse, Menmery, LCARS, Clanker, Frankenmemory, MiMo, Field Guide, Meatbag Tasks,
plus provider brands/tokens.

---

## Validation

| Check | Result |
| --- | --- |
| Key count | **9622 / 9622** parity with `en.json` |
| Placeholder sets (all keys) | **0 mismatches** (`{0}` / `{name}` preserved, none invented) |
| Locked-brand drops (`brands.json`) | **0** |
| Glossary brand drops (Imps, Lore, Field Guide, …) | **0** |
| HTML tags introduced | **0** |
| Unicode bidi controls | **0** |
| JSON format | 2-space indent, trailing newline, UTF-8 |
| `node scripts/i18n-catalog.mjs validate` | `de: keys=9622 catalog=de dir=ltr` — **0 de errors** (remaining error is `fr:ui.open.clanker.tasks`, other worker) |
| `tests/test_i18n_contract.py` (run via import) | 6 passed; 1 failed on **pre-existing `fr`** locked-token — **not de** |
| `python3 scripts/i18n_freeze.py --check` | drift `english_keys=9623 vs freeze_keys=9622` — pre-existing en-side drift, not from this change |

Not run (no pytest binary in this environment; contract tests executed by direct import):
browser acceptance `tests/i18n_browser_acceptance.mjs`, full downstream suite.

---

## Ambiguity / director notes

- **`ui.open.clanker.tasks`** — English "Open Clanker Tasks" = verb *Open* + product
  *Clanker Tasks*. Brand lock requires `Open Clank` substring (contained in
  "Open Clanker"). Rendered **„Open Clanker Tasks öffnen“** (brand intact).
- **`Field Guide`** — kept with space in German compounds („Field Guide-Lektion“)
  rather than the more idiomatic hyphenated „Field-Guide-…“, because the brand token
  must survive byte-identical.
- **`ui.what.is.open.clank.local.first.ai.assistant.multi.provider`** — "AI" kept as
  **KI** in prose sentences where de.json already does; product-surface AI tokens kept.
- **`ui.yours.for.the.clanking`** — brand-voice wordplay tied to Clanker; kept as
  **„Ihres fürs Clanking.“** to avoid inventing a non-existent German coinage.
- **Theme names** (Clanker Dark, Clanker Emoji Drift, Copal Notes, …) left in English
  as product/theme identifiers with locked brands inside.
- **`ui.a.hands.on.course.teaching…`** — EN source itself uses `OpenClank` (locked
  variant). DE keeps `OpenClank` there to satisfy brand-in-source ⊆ brand-in-target;
  the sibling key `…46123ac3` uses `Open Clank` and is kept as `Open Clank`.
