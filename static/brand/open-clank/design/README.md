# Open Clank design standard 1.0.0

This data-only packet defines the original Open Clank deliverable vocabulary. `standard.json` identifies the standard, saved baseline provenance, the 26 studies and SHA-256 hashes of each SVG and thumbnail. SVGs are editable and expose `--oc-*` CSS role properties with dark baseline fallbacks. The 64 × 64 PNGs show the same identities in both baseline palettes. Bind effective roles explicitly for standalone output; SVG custom property inheritance is not reliable through an HTML `img` boundary.

The saved palettes document a source revision; the existing theme normalizer and effective semantic CSS roles remain authoritative for a selected theme. Save a sanitized resolved appearance, standard ID/version/hash and exact asset hashes with each output. Do not store account identifiers, browser storage keys or host paths. This packet does not provide components or executable imports.

Preserve compact, legible glyph silhouettes and treat the FT studies as construction examples, not icons. Apply theme hardness/radius and type choice; use Liga Comic Mono, Fira Code, monospace as existing fallbacks when available. Respect reduced-motion settings, avoid copying background animation loops, and keep reading planes quiet. Distinguish primary accent (`red` in the legacy palette) from semantic error.

Use the [brand pack](../README.txt) when a deliverable needs the Open Clank wordmark. The SVG masters contain outlined Comic Clank Mono Bold 0.300 glyphs. Theme typography is selected independently.
