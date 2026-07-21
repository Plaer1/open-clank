import asyncio
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_clanker_presets_lead_original_and_dark_is_default():
    theme = _text("static/js/theme.js")
    assert theme.index("'clanker-dark': {") < theme.index("'clanker-light': {") < theme.index("  dark:")
    assert "'clanker-dark': 'Clanker Dark'" in theme
    assert "'clanker-light': 'Clanker Light'" in theme
    assert "dark: 'original'" in theme
    assert "const DEFAULT_THEME = 'clanker-dark';" in theme


def test_clanker_fonts_are_product_bundled_and_locked():
    style = _text("static/style.css")
    theme = _text("static/js/theme.js")
    index = _text("static/index.html")
    for name in ("LigaComicMono-Regular.woff2", "Fredoka-Variable.woff2"):
        font = ROOT / "static/fonts" / name
        assert font.read_bytes()[:4] == b"wOF2"
        assert f"/static/fonts/{name}" in style
    assert "'liga-comic-mono': \"'Liga Comic Mono', 'Fira Code', monospace\"" in theme
    assert "font.disabled = locked" in theme
    assert '<option value="liga-comic-mono">Liga Comic Mono</option>' in index
    assert "font-family: 'Fredoka', 'Liga Comic Mono', sans-serif" in style


def test_clanker_dark_uses_asset_free_routefield_and_light_keeps_its_textures():
    theme = _text("static/js/theme.js")
    style = _text("static/style.css")
    index = _text("static/index.html")
    assert "'clanker-dark':  'clanker-routefield'" in theme
    assert "'clanker-light': 'clanker-blueprint'" in theme
    assert "body.bg-pattern-clanker-routefield" in style
    assert "body.bg-pattern-clanker-blueprint" in style
    assert "body.bg-pattern-clanker-sweep" not in style
    assert "function _initClankerRoutefield()" in theme
    assert "canvas.id = 'clanker-routefield-canvas'" in theme
    assert "canvas.dataset.motion = motion.matches ? 'reduced' : 'active'" in theme
    assert "window.requestAnimationFrame(draw)" in theme
    assert "window.matchMedia('(prefers-reduced-motion: reduce)')" in theme
    assert "@keyframes clanker-blueprint-conveyor" in style
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert '<option value="clanker-routefield">Clanker Night Route Field</option>' in index
    assert '<option value="clanker-blueprint">Clanker Blueprint Conveyor</option>' in index
    for name in (
        "midnight-speckle.png",
        "cyan-scanline-panel.png",
        "cream-paper-pulp.png",
        "cobalt-ribbed-panel.png",
    ):
        asset = ROOT / "static/themes/clanker" / name
        assert asset.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    for name in ("midnight-speckle.png", "cyan-scanline-panel.png"):
        assert f"/static/themes/clanker/{name}" not in style
    for name in ("cream-paper-pulp.png", "cobalt-ribbed-panel.png"):
        assert f"/static/themes/clanker/{name}" in style


def test_theme_font_setting_reaches_notes_and_workspace_ui():
    theme = _text("static/js/theme.js")
    style = _text("static/style.css")

    def rule(selector: str) -> str:
        match = re.search(
            rf"(?ms)^[ \t]*{re.escape(selector)}[ \t]*\{{(.*?)^[ \t]*\}}",
            style,
        )
        assert match, selector
        return match.group(1)

    assert "document.documentElement.style.setProperty('--font-family', family)" in theme
    for selector in (
        ".modal-content",
        ".doc-version-panel",
        ".notes-pane",
        ".notes-pane-title",
        ".copal-workspace",
        ".copal-note-live-preview, .copal-note-reading",
    ):
        assert "var(--font-family" in rule(selector)
    assert '.copal-codemirror-host[data-mode="source"] .cm-scroller { font-family: var(--font-mono' in style


def test_every_named_theme_entry_point_uses_full_theme_application():
    chat_stream = _text("static/js/chatStream.js")
    slash = _text("static/js/slashCommands.js")
    ai = _text("src/ai_interaction.py")
    assert "tm.applyTheme(themeName, colors);" in chat_stream
    assert slash.count("tm.applyTheme(") >= 3
    assert '"clanker-dark", "clanker-light", "dark"' in ai


def test_ai_ui_control_accepts_both_clanker_presets():
    from src.ai_interaction import do_ui_control

    for name in ("clanker-dark", "clanker-light"):
        result = asyncio.run(do_ui_control(f"set_theme {name}"))
        assert result["ui_event"] == "set_theme"
        assert result["theme_name"] == name
