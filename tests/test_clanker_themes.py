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


def test_clanker_backgrounds_are_asset_free_and_include_texture_inspired_choices():
    theme = _text("static/js/theme.js")
    style = _text("static/style.css")
    index = _text("static/index.html")
    assert "'clanker-dark':  'clanker-routefield'" in theme
    assert "'clanker-light': 'clanker-blueprint'" in theme
    assert "body.bg-pattern-clanker-routefield" in style
    assert "body.bg-pattern-clanker-blueprint" in style
    assert "body.bg-pattern-clanker-sweep" not in style
    assert "function _initClankerRoutefield()" in theme
    assert "id: 'clanker-routefield-canvas'" in theme
    assert "canvas.dataset.motion = motion.matches ? 'reduced' : 'active'" in theme
    assert "canvas.dataset.backgroundEffectCanvas = 'true'" in theme
    assert "window.requestAnimationFrame(frame)" in theme
    assert "window.cancelAnimationFrame(animationFrame)" in theme
    assert not re.search(r"previousFrame\s*&&\s*time\s*-\s*previousFrame\s*<", theme)
    assert "animationTime += Math.min(time - previousFrame, 50)" in theme
    assert "paint(motion.matches ? 0 : animationTime, motion.matches)" in theme
    assert "_disposeBackgroundEffect();" in theme
    assert "requestAnimationFrame(draw)" not in theme
    assert "const outer = [...upper, ...lower.slice(0, -1).reverse()];" in theme
    assert "return { paths, junctions, snakePoints, maxSnakeStep:" in theme
    assert "const tail = [];" in theme
    assert "window.matchMedia('(prefers-reduced-motion: reduce)')" in theme
    assert "@keyframes clanker-lcars-status-sweep" in style
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert '<option value="clanker-routefield">Clanker Signal Routes</option>' in index
    assert '<option value="clanker-blueprint">Clanker LCARS Status Sweep</option>' in index
    login = _text("static/login.html")
    assert "body.theme-clanker-dark .card" in login
    assert "body.theme-clanker-light .card" in login
    assert "repeating-linear-gradient" not in login
    for pattern, function_name, canvas_id in (
        ("clanker-kene-weave", "_initClankerKeneWeave", "clanker-kene-weave-canvas"),
        ("clanker-radar", "_initClankerRadar", "clanker-radar-canvas"),
        ("clanker-gem-drift", "_initClankerGemDrift", "clanker-gem-drift-canvas"),
    ):
        assert f"'{pattern}': {function_name}" in theme
        assert f"id: '{canvas_id}'" in theme
        assert f'<option value="{pattern}">' in index
    for name in (
        "midnight-speckle.png",
        "cyan-scanline-panel.png",
        "cream-paper-pulp.png",
        "cobalt-ribbed-panel.png",
    ):
        asset = ROOT / "static/themes/clanker" / name
        assert asset.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    for name in (
        "midnight-speckle.png",
        "cyan-scanline-panel.png",
        "cream-paper-pulp.png",
        "cobalt-ribbed-panel.png",
    ):
        assert f"/static/themes/clanker/{name}" not in style
    assert "body.bg-pattern-synapse {\n  background-image: none;" in style
    assert "body.bg-pattern-clanker-gem-drift,\nbody.bg-pattern-synapse" in style


def test_clanker_palettes_use_neutral_fields_and_bounded_command_colors():
    theme = _text("static/js/theme.js")
    style = _text("static/style.css")
    index = _text("static/index.html")
    login = _text("static/login.html")

    assert "bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:'#5A9EF5'" in theme
    assert "bg:'#F3EEDB', fg:'#17202A', panel:'#FFF9E7', border:'#26323D', red:'#2469D8'" in theme
    for color in ("#62C7E8", "#F6BE48", "#A8DE53", "#FF776E", "#ED6AB0", "#B7A7E8"):
        assert color in style
    assert 'content="#191A1E"' in index
    assert 'content="#191A1E"' in login
    assert "body.theme-clanker-dark .chat-input-bar::before" in style
    assert "body.theme-clanker-light #welcome-screen::before" in style
    assert "body.theme-clanker-dark .mimo-plan-dock" in style
    assert "background: color-mix(in srgb, var(--clanker-surface) 78%, transparent) !important" in style
    assert "backdrop-filter: blur(7px) saturate(1.08)" in style


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


def test_project_mark_replaces_the_sailboat_and_stays_theme_colorable():
    mark = _text("static/icons/open-clank-mark.svg")
    index = _text("static/index.html")
    login = _text("static/login.html")
    theme = _text("static/js/theme.js")
    docs = _text("docs/index.html")

    for source in (mark, index, login, theme, docs):
        assert "M16 3 29 27H3Z" in source
        assert "M16 4L16 22L6 22Z" not in source
    assert 'stroke="currentColor"' in mark
    assert "stroke='${fg}'" in theme
    assert "stroke='\" + ac + \"'" in index
