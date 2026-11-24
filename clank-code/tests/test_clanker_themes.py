import asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bundled_theme_fonts_are_woff2():
    for name in ("LigaComicMono-Regular.woff2", "ComicClankMono-Bold-Wordmark.woff2"):
        with (ROOT / "static/fonts" / name).open("rb") as font:
            assert font.read(4) == b"wOF2"


def test_ai_ui_control_accepts_both_clanker_presets():
    from src.ai_interaction import do_ui_control

    for name in ("clanker-dark", "clanker-light"):
        result = asyncio.run(do_ui_control(f"set_theme {name}"))
        assert result["ui_event"] == "set_theme"
        assert result["theme_name"] == name
