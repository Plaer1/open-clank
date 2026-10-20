# Carry an editable icon into a game

Optional practice.

- Save this original editable source as marker.svg:

```xml
<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <path d="M4 28 L16 4 L28 28 Z" fill="#ffd34e"/>
  <rect x="8" y="21" width="5" height="4" fill="#1b1e24"/>
</svg>
```

- Candidate route: Inkscape 1.4.2 → PNG → Godot 4.7.2. Record actual versions. Open the SVG; export the page at32×32 with transparent background. Equivalent CLI:

```sh
inkscape marker.svg --export-area-page --export-type=png --export-filename=marker.png --export-width=32 --export-height=32
```

- Confirm RGBA dimensions and corner(0,0) alpha0. Copy to art/marker.png in the Godot project. Add Sprite2D, set Texture to that PNG, position(320,180); use lossless import. Change SVG yellow to cyan, export/reimport; lower-left dark mark must stay lower-left. Keep the editable SVG separately.
- This source-inspected route has no author export/import receipt. Reading the input is a conceptual preview.

**Self-check:** Perform the source edit/export/import. Supply versions, actual32×32/alpha result and visible orientation evidence; explain why changing artwork need not change collection rules.
