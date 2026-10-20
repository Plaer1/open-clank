# Included component and asset provenance

Reviewed October 4, 2026. [Acknowledgments](../ACKNOWLEDGMENTS.md) is the reader entry point. This record maps included paths, pins and notices; dependency manifests are not a claim every listed package ships. Component licenses apply to their own material. Root AGPL terms do not replace them. No reference payload is a runtime or build authority.

## Integrated source roots

| Root | Source and acquisition | Included scope / modifications | Retained notice |
| --- | --- | --- | --- |
| Odysseus | [odysseus-dev/odysseus](https://github.com/odysseus-dev/odysseus), incorporated base `d96c7af3df769508de01900b2264520b649caa4c` per canonical upstream-sync receipt | Inherited Python/web workspace across app.py, src/, routes/, services/, static/; Open Clank branding, native/editor/agent integrations and later changes. Pin is an incorporated base, not a claim of full later upstream parity. | AGPLv3 root [LICENSE](../LICENSE); original source notices preserved. No separate author name invented. |
| MiMo Code / opencode | [XiaomiMiMo/mimo-code](https://github.com/XiaomiMiMo/mimo-code); declared upstream `2bda17944b346ab85c8ee3cf0a0d4ab24819d37c`, import `f53c8a3e86f258164ff47eae00a03559a38895cf`, per [vendor manifest](../packages/mimo-code/openclank-vendor.json) | packages/mimo-code/, including packages/opencode/; managed engine, credentials/provider/IPC/history adaptations. The manifest pin is recorded provenance, not a claim every current modified byte equals upstream. | [MiMo/opencode MIT](../packages/mimo-code/LICENSE): Xiaomi Corporation / MiMo Code 2026 and opencode 2025; [historical opencode notice](opencode-MIT-LICENSE.txt). |
| Copal | Local integrated package; package version 0.2.0. No verified public upstream/author or separate package-root license in the inspected source/ledger. | packages/Copal/, src/openclank/copal_*.py, routes/copal_*.py, static/js/copal*; Editor/Wiki/database/native-shell integration. First-party root terms apply where appropriate; no inferred third-party Copal license is assigned. | Existing per-file/dependency notices preserved. Unknown public origin/package-license disposition is explicit. |
| Lore | [EpicGames/lore](https://github.com/EpicGames/lore/tree/fa606b08781705e24807cfc05e1518c79e1f688b), `fa606b08781705e24807cfc05e1518c79e1f688b`, 0.9.1-nightly | packages/openclank-history/vendor/lore/ storage dependency closure; Open Clank history service wrapper and explicit durability/compaction repairs. This is not the complete Lore desktop product. | [Epic Games Inc.2026 MIT](../packages/openclank-history/vendor/lore/LICENSE); nested [glob-match MIT](../packages/openclank-history/vendor/lore/vendor/glob-match/LICENSE), [quinn-proto MIT](../packages/openclank-history/vendor/lore/vendor/quinn-proto/LICENSE-MIT)/[Apache](../packages/openclank-history/vendor/lore/vendor/quinn-proto/LICENSE-APACHE), [rpmalloc](../packages/openclank-history/vendor/lore/lore-base/native/thirdparty/rpmalloc/LICENSE). |

## Adapted code and design studies

llmfit (Alex Jones, MIT): services/hwfit/, Cookbook routes/UI and scripts/odysseus-cookbook; [notice](llmfit-MIT-LICENSE.txt), [source](https://github.com/AlexsJones/llmfit). Tongyi DeepResearch (Alibaba-NLP/Tongyi Lab, Apache-2.0): research/search pipeline adaptations under services/research/, services/search/ and src/research_handler.py; [notice](DeepResearch-Apache-2.0.txt), [source](https://github.com/Alibaba-NLP/DeepResearch). These historical adaptation records do not establish a single exact acquisition pin for all current files.

Hexes was inspired by [Henxels](https://github.com/benquemax/henxels) v0.11.1; src/hex_contract.py identifies an Open Clank implementation, not a Henxels runtime dependency. [Existing MIT notice](henxels-MIT-LICENSE.txt) remains.

[AgentsView](https://github.com/kenn-io/agentsview/tree/94ddc2381c68ca39d8e02830385adc73c40dfacc), inspected pin `94ddc2381c68ca39d8e02830385adc73c40dfacc` (Kenn Software LLC 2026, MIT), informed Usage/activity interface layout and workflows. Native first-party Usage files are static/js/usage*.js and services/stats/. The available implementation/study ledger establishes inspiration, not a path-level copied-source ledger; no AgentsView telemetry service, session importer, fonts or kit-ui asset is claimed as included.

Logging design studies: [llm_intercept](https://github.com/mlech26l/llm_intercept/tree/f5c5a11aa3d257c94c42699f79803375c7140dc4), Mathias Lechner 2025 MIT; [llm.log](https://github.com/lanesket/llm.log/tree/9b292ebee2f613e8a0a61df9713bd0aebc645914), lanesket 2025 MIT. The current source/ledger does not establish substantial copied code from either. They are credited as study inputs, not distributed interception/CA tooling. If a future path-level copying record establishes incorporation, retain its exact applicable notice then.

## Google artwork and catalogue

[Local bundle manifest](../static/vendor/google-emoji/bundle-manifest.json): Noto source `e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e`; Xavier Salazar Kitchen catalogue source `062ffca53643d36946336cdc2c7655e2576f0a42`. Local `static/vendor/google-emoji/emoji-assets.pack` and associated index serve acquired artwork, with no runtime CDN fallback. 146,983 of 147,000 Kitchen combinations are available; 17 recorded upstream 404 exceptions mean `complete:false`. Google source acquisition records 4,038, resolvable runtime identities 1,924, and sampler 1,916 are different counts.

Google [Noto Emoji SVG artwork](https://github.com/googlefonts/noto-emoji/tree/e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e/svg) is Apache-2.0: [retained SVG license](../static/vendor/google-emoji/noto-svg/SVG-LICENSE). [Separate font OFL notice](../static/vendor/google-emoji/noto-svg/FONT-OFL-LICENSE) is retained from the source family; it does not relicense the SVGs or imply a Noto font is loaded. Google Emoji Kitchen artwork and [Xavier Salazar combination catalogue](https://github.com/xsalazar/emoji-kitchen/tree/062ffca53643d36946336cdc2c7655e2576f0a42) keep the accepted attribution. No MIT, Apache, or CC BY redistribution label is invented for the Google Kitchen mashups. The catalogue code license is distinct from the artwork.

## Editor, spelling and icons

[CodeMirror/Lezer notices](CODEMIRROR-LICENSES.md) enumerate selected source imports, installed closure versions and full copyright/license texts. [Frontend/icon notices](FRONTEND-LICENSES.md) retain Shiki/RegExp-engine dependency texts and Lucide ISC/Feather MIT notices. Spell worker: nspell 2.1.5 plus dictionary-en 4.0.0 en_US/SCOWL 2020.12.07; [complete compound notices](../static/js/copal/SPELLING-LICENSES.md) include Kevin Atkinson, Ispell/Geoff Kuenning, Princeton WordNet, UKACD/J Ross Beresford, VarCon/Benjamin Titze and public-domain sources. Do not flatten the dictionary to MIT. Original shared Open Clank SVGs are identified by static/js/uiIcons.js; separate Copal React sources import lucide-react 0.525.0. No Lucide inclusion is inferred in CodeMirror.

## Other frontend bundles and runtime loads

| Actual artifact / runtime load | Version authority / purpose | Component license and source |
| --- | --- | --- |
| static/lib/shiki.bundle.js | Shiki 3.23.0 selected 39-grammar build; installed build inputs/receipt; custom Open Clank CSS theme, JavaScript regex engine | MIT Shiki; separate Microsoft vscode-textmate and regex dependency notices in [frontend texts](FRONTEND-LICENSES.md). Individual grammar provenance beyond supplied package notices is not fully reconstructed. [Shiki](https://github.com/shikijs/shiki) |
| static/lib/mermaid.min.js |11.16.1 bundle version and build receipt; local, not CDN | MIT Mermaid with retained nested bundle notices (including DOMPurify, D3 and KaTeX); [texts](FRONTEND-LICENSES.md), [upstream](https://github.com/mermaid-js/mermaid) |
| static/lib/xlsx.full.min.js | Embedded SheetJS version 0.20.3; spreadsheet read/write | Apache-2.0 SheetJS; retained SheetJS header; [source](https://github.com/SheetJS/sheetjs). Exact original acquisition URL/companion notices not reconstructed; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/docx.umd.min.js | DOCX generation; exact top-level version not established from bundle | MIT; [source](https://github.com/dolanmiu/docx). Preserve embedded third-party notices; complete exact build closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/mammoth.browser.min.js | DOCX-to-HTML; exact top-level version not established | BSD-2-Clause; [source](https://github.com/mwilliamson/mammoth.js); exact acquisition/closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| static/lib/html2pdf.bundle.min.js | HTML/PDF export, includes jsPDF/html2canvas; exact html2pdf version not established | MIT components; [html2pdf](https://github.com/eKoopmans/html2pdf.js), [jsPDF](https://github.com/parallax/jsPDF), [html2canvas](https://github.com/niklasvh/html2canvas). Bundle refers to absent html2pdf.bundle.min.js.LICENSE.txt; exact companion/closure remains unresolved, not falsely marked complete; [upstream notice snapshots](FRONTEND-LICENSES.md) are retained separately. |
| static/lib/qrcode.min.js | QR setup; no embedded exact version | MIT node-qrcode; [source](https://github.com/soldair/node-qrcode); original exact build notice/closure unresolved; [upstream notice](FRONTEND-LICENSES.md) retained. |
| KaTeX CDN tags in static/index.html |0.16.22; math | MIT [KaTeX](https://github.com/KaTeX/KaTeX) |
| Pyodide lazy CDN in static/js/codeRunner.js |0.27.5; Python in browser, optional invoked runtime | MPL-2.0 [Pyodide](https://github.com/pyodide/pyodide), with its separately licensed Python/packages |

There is no current static/lib/highlight.min.js; historical highlight.js credit is not evidence of current shipment. PDFObject 2.1.1 (MIT, https://github.com/pipwerks/PDFObject) is a retained historical credit; no current runtime load was established in this review. Fonts and exact unresolved variant chains are in [font notices](FONT-NOTICES.md).

## Services and installation dependencies

Current Compose files pull SearXNG `2026.5.31-7159b8aed` and binwiederhier/ntfy (unversioned image). Their upstream AGPL-3.0 / Apache-2.0-or-GPL-2.0 licenses belong to those services. Chroma is no longer in current Compose or requirements; its older Apache-2.0 credit is historical/compatibility, not a required current memory service. Current requirements and Cargo/Bun lockfiles select installed libraries; their own distributions carry full notices. Optional faster-whisper, ddgs, PyMuPDF and markitdown remain feature-dependent; PyMuPDF has its own AGPL/commercial terms. Open Clank root is already AGPL, so the old MIT-core/feature-only copyleft explanation is withdrawn.

## Asset fingerprints

SHA256 values identify the inspected bytes, not a new license grant or a rebuild claim.

| Artifact | SHA256 |
| --- | --- |
| static/fonts/ComicNeue-Regular.woff2 | `dc603c8b97ea803cc1377f15a8cd952ca2fb7408e322b8025099ad1cbf6eaf4f` |
| static/fonts/FiraCode-Light.woff2 | `e3aa3db06cfb19dfc0b0f1f38355add3e8d1ef45d3af39ce95d9ca7d96114e6c` |
| static/fonts/FiraCode-Regular.woff2 | `a6ce59520b90e15d7062ffef214f94c8add5a4085c0bbb1683602ef227a4d1fe` |
| static/fonts/FiraCode-SemiBold.woff2 | `d16779aa6dfc7c4effe686ece5bdf4b1356a7352167e37fa256f596a9d428f11` |
| static/fonts/Fredoka-Variable.woff2 | `9e0bcbe720edcfdc16b33a4fe164cc4aba400f7b5b23755a725e6e333580ae9c` |
| static/fonts/Inter-Medium.woff2 | `7e80d9f65861ee6836a0081d4e75d88fb8789e5651d05edbc49640442a9610ee` |
| static/fonts/Inter-Regular.woff2 | `338239f6b590b8ced3bf857654d32da3fd3663294cd3003651ed57aa3abd7aa1` |
| static/fonts/Inter-SemiBold.woff2 | `5013f48d77ab627b1db7c2415914284ef09abc3f60a8e0d0d8f3cd1bfebefb5e` |
| static/fonts/LigaComicMono-Regular.woff2 | `c6bd8a66f2da82fc8b066514cad298029265fbc28ffa88c9f5b9efb5a8ceb561` |
| static/fonts/OpenDyslexic-Bold.woff2 | `dd9fa9c7991113b0dddefe9506a30ad26b48e302b7a8fb91719a4726e8fde85b` |
| static/fonts/OpenDyslexic-Regular.woff2 | `f007004af3cda5d8076e57c943f8cc8d00a0da25988b1ae1048683d60e7cac1a` |
| static/fonts/custom/GohuFont.ttf | `db1c6de2a3c60e441948f10a5078b11952531850e7cb205b9be6037963ab660a` |
| static/lib/docx.umd.min.js | `02d568d203c0180af37609bcf5ff6c0919d220f933a88ca896eba0556a08faad` |
| static/lib/html2pdf.bundle.min.js | `9edbed630ebd644bc5189b99e8138893041c89a3118a672047fa2b625a12e48c` |
| static/lib/mammoth.browser.min.js | `deb07bf230d1cb3e190bc5adc6743f35c6531b6571d1e5469b24f452a7f0f4ab` |
| static/lib/mermaid.min.js | `18327bef70d96fb505fe7287d9f6a7362ebf07ff6576ddfaffb1a06f3e1a2954` |
| static/lib/qrcode.min.js | `0935de514006b84fe54404b4a98fb3b5cc45478d9efa64d75e19719025663c19` |
| static/lib/shiki.bundle.js | `8d585f7b6e6a7f318281a33df0a64e21578f8c483654cff1a2f4d9e5432fedfc` |
| static/lib/xlsx.full.min.js | `cc015130aa8521e7f088f88898eba949ccdcbfb38df0bd129b44b7273c3a6f41` |

## Open provenance items

Copal public origin/separate package license; Liga exact combined-font acquisition/modification/license chain; file named GohuFont.ttf actual origin/license; exact older DOCX/PDF/QR/Mammoth acquisition and nested build notices; per-grammar Shiki source closure. These are candid source limitations, not claims of permission and not feature-removal instructions. Existing notices and assets are preserved.
