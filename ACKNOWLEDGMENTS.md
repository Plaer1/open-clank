# Acknowledgments and third-party notices

Open Clank builds on Odysseus and brings together Copal’s document workspace, MiMo Code/opencode agent foundations, and Epic Games’ Lore history technology. We thank the authors of included code and assets, and the projects that informed its design.

Open Clank is distributed under **AGPL-3.0-or-later**; see [LICENSE](LICENSE). Included components keep their own copyright and license terms. This document does not relicense them or describe the core as MIT/permissive. Full path/pin/version qualifications and asset fingerprints are in [Included component provenance](licenses/BUNDLED-COMPONENTS.md). If a credit is missing or misattributed, please open an issue.

## Integrated roots and adapted code

- **[Odysseus](https://github.com/odysseus-dev/odysseus)** — major inherited application/code lineage, AGPLv3; incorporated base and adaptation scope in the [source-root record](licenses/BUNDLED-COMPONENTS.md#integrated-source-roots).
- **Copal** — integrated document/knowledge workspace, Editor/Wiki and native storage/shell foundations. Its local source is packages/Copal/ and related Open Clank adapters. No verified public upstream/author or separate package-root license was found; the record retains that qualification rather than inventing a license.
- **[MiMo Code](https://github.com/XiaomiMiMo/mimo-code) / [opencode](https://github.com/anomalyco/opencode)** — included managed agent/model engine in packages/mimo-code/. MIT, copyright Xiaomi Corporation / MiMo Code 2026 and opencode 2025; [retained license](packages/mimo-code/LICENSE), [historical opencode notice](licenses/opencode-MIT-LICENSE.txt). Open Clank adaptations do not imply upstream parity.
- **[Lore](https://github.com/EpicGames/lore)** — included storage technology under packages/openclank-history/vendor/lore/, used by the Open Clank History wrapper. MIT, copyright 2026 Epic Games Inc.; [license](packages/openclank-history/vendor/lore/LICENSE) and [nested notices](licenses/BUNDLED-COMPONENTS.md#integrated-source-roots).
- **[llmfit](https://github.com/AlexsJones/llmfit)**, Alex Jones — Cookbook hardware/model fit adaptations; [MIT notice](licenses/llmfit-MIT-LICENSE.txt).
- **[Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)**, Alibaba-NLP/Tongyi Lab — research/search pipeline adaptations; [Apache-2.0 notice](licenses/DeepResearch-Apache-2.0.txt).

## Inspiration and studies

**[AgentsView](https://github.com/kenn-io/agentsview)**, Kenn Software LLC, informed Usage/activity interface layout and workflows. **[llm_intercept](https://github.com/mlech26l/llm_intercept)**, Mathias Lechner, and **[llm.log](https://github.com/lanesket/llm.log)**, lanesket, informed logging design studies. These credits describe the established study/design relationship; current records do not establish a substantial copied-source ledger or shipment of their proxy/telemetry products. Immutable inspected pins and distinctions are in the [study record](licenses/BUNDLED-COMPONENTS.md#adapted-code-and-design-studies).

Open Clank Hexes was inspired by **[Henxels](https://github.com/benquemax/henxels)**. The first-party implementation has no runtime Henxels dependency; its [existing MIT notice](licenses/henxels-MIT-LICENSE.txt) is preserved.

## Google artwork

**Google** provides [Noto Emoji](https://github.com/googlefonts/noto-emoji) and Emoji Kitchen artwork; **[Xavier Salazar](https://github.com/xsalazar/emoji-kitchen)** provides the Kitchen combination catalogue. Noto **SVG artwork is Apache-2.0**, with its [retained SVG text](static/vendor/google-emoji/noto-svg/SVG-LICENSE). The [separate Noto font OFL text](static/vendor/google-emoji/noto-svg/FONT-OFL-LICENSE) is distinct. Neither license is applied to Google Kitchen mashups. The accepted Google/Xavier Kitchen attribution is retained without inventing a redistribution-license label.

The local pack has 146,983 available Kitchen combinations and 17 recorded upstream 404 exceptions (of 147,000 expected). Noto acquisition records and runtime/sample identities are different counts. See the [artwork record](licenses/BUNDLED-COMPONENTS.md#google-artwork-and-catalogue) for pins, exact counts and local delivery scope.

## Editors, spelling, icons and fonts

- **CodeMirror / Lezer** — selected editor/parser packages and lazy language chunks; [versions and full MIT/dependency notices](licenses/CODEMIRROR-LICENSES.md).
- **Shiki** and its JavaScript regex/TextMate dependencies, plus **Mermaid** — local highlighting/diagram bundles; [retained frontend notices](licenses/FRONTEND-LICENSES.md).
- **nspell 2.1.5**, Titus Wormer and contributors, with **dictionary-en 4.0.0 / SCOWL** — [complete spelling and compound dictionary notices](static/js/copal/SPELLING-LICENSES.md). The dictionary is not described as a single MIT work.
- **Icons** — original shared Open Clank SVG geometry is separate from Lucide assets used in Copal React source; [Lucide ISC / Feather MIT texts](licenses/FRONTEND-LICENSES.md).
- **Fonts** — Fira Code 6.002, Inter 4.001, Fredoka 2.001, Comic Neue 2.003, OpenDyslexic 0.920 and their authors; [exact variants, copyright and OFL texts](licenses/FONT-NOTICES.md). Liga Comic Mono 0.1.1 credits Comic Mono and Ilya Skriblovsky’s Fira Code ligatures; its combined-font provenance remains unresolved. The file named GohuFont.ttf identifies internally as Untitled1/Unknown 2025, so the inherited Gohu/WTFPL claim is unverified for these bytes. Both files remain present.

## Other frontend and service dependencies

Bundled SheetJS/xlsx, docx, mammoth.js, html2pdf.js (including jsPDF/html2canvas) and node-qrcode retain their component terms. KaTeX 0.16.22 and optional browser Python/Pyodide 0.27.5 load from CDNs. Mermaid 11.16.1 is local. Historical highlight.js/PDFObject credits are qualified separately; see the [actual load/version/license record and remaining acquisition gaps](licenses/BUNDLED-COMPONENTS.md#other-frontend-bundles-and-runtime-loads).

Current Docker Compose files pull SearXNG and ntfy alongside Open Clank; their own licenses apply to those services. Chroma is a historical/compatibility credit and is absent from current Compose/requirements. [Service/dependency scope](licenses/BUNDLED-COMPONENTS.md#services-and-installation-dependencies) distinguishes those services from optional install/runtime dependencies.

## Python dependencies

Core (`requirements.txt`) and optional (`requirements-optional.txt`):

| Package | License |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| python-dotenv | BSD-3-Clause |
| HTTPX | BSD-3-Clause |
| Pydantic / pydantic-settings | MIT |
| SQLAlchemy | MIT |
| pypdf | BSD-3-Clause |
| BeautifulSoup4 | MIT |
| charset-normalizer | MIT |
| NumPy | BSD-3-Clause |
| ChromaDB (historical compatibility; absent from current requirements) | Apache-2.0 |
| fastembed | Apache-2.0 |
| youtube-transcript-api | MIT |
| markdown | BSD-3-Clause |
| icalendar | BSD-2-Clause |
| caldav | GPL-3.0-or-later OR Apache-2.0 (dual; used under Apache-2.0) |
| cryptography | Apache-2.0 / BSD-3-Clause |
| bcrypt | Apache-2.0 |
| MCP (Model Context Protocol SDK) | MIT |
| pyotp | MIT |
| qrcode\[pil] | BSD-3-Clause |
| croniter | MIT |
| pytest / pytest-asyncio | MIT / Apache-2.0 |
| ddgs (optional, replaces the former duckduckgo-search package name) | MIT |
| markitdown (optional — Office/EPUB text extraction) | MIT |
| **PyMuPDF** *(optional — form-filling only)* | **AGPL-3.0** — see note below |

## Companion services and tools

Open Clank interoperates with or invokes these when configured; this credit does not claim their complete products are bundled:

- [Ollama](https://github.com/ollama/ollama) — local model serving (MIT).
- [Radicale](https://github.com/Kozea/Radicale) — CardDAV/CalDAV (GPL-3.0).
- [Dovecot](https://www.dovecot.org/) — IMAP server.
- [isync / mbsync](https://isync.sourceforge.io/) — mailbox sync (GPL-2.0).
- [tmux](https://github.com/tmux/tmux) — terminal multiplexer (ISC).
- [OpenSSH](https://www.openssh.com/) — remote server/key-management tools (BSD-style terms).
- Configured model/API providers, including Anthropic, OpenAI, Google and DuckDuckGo.

## Optional features and license scope

pypdf and charset-normalizer support extraction/encoding; PyMuPDF is optional for PDF forms and has its own AGPL/commercial terms. markitdown is optional for Office/EPUB extraction (MIT); the configured extras are declared in requirements-optional.txt. caldav is dual GPL-3.0-or-later/Apache-2.0. Installing or omitting these does not change Open Clank’s root AGPL license. Earlier prose about an MIT core or copyleft applying only to one feature was incorrect and is withdrawn. The manifests, lockfiles and dependency distributions determine installed versions and their full notices; this table preserves inherited credits, not a complete resolved dependency bill of materials.

## Thanks to

The inherited Odysseus acknowledgments thanked these models and contributors.
We preserve that historical credit:

- **gpt-oss-120b** — the legend that kicked this project off.
- **Qwen3-235B**
- **DeepSeek V3.1 · DeepSeek V4 Pro · DeepSeek V4 Flash**
- **Claude** (Anthropic)
- **Codex** (OpenAI)
- Friends, for helping me debug.
