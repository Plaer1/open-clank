"""Refresh the native wizard's PNGs and private desktop fonts from its canonical web assets."""
from pathlib import Path
import subprocess

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1] / "static"
BRAND = ROOT / "brand/open-clank"
OUT = BRAND / "installer/windows"


def main():
    OUT.mkdir(exist_ok=True)
    for stem, name, width in (("open-clank-pyramid-eye", "triangle", 96),
                              ("open-clank-wordmark-wide", "wordmark", 1040)):
        subprocess.run(["rsvg-convert", "-w", str(width), "-o", str(OUT / (name + ".png")),
                        str(BRAND / "SVG" / (stem + ".svg"))], check=True)
    for face in ("ComicClank-Regular", "ComicClank-Bold", "ComicClankMono-Regular"):
        font = TTFont(ROOT / "fonts" / (face + ".woff2"), recalcTimestamp=False)
        font.flavor = None
        # The installer is English. Include Latin names, common punctuation and UI symbols.
        keep = subset.Subsetter()
        keep.populate(unicodes=[*range(0x20, 0x250), *range(0x2000, 0x2100),
                                *range(0x2190, 0x2200), 0x25c6, 0x25cb, 0x2713])
        keep.subset(font)
        font.save(OUT / (face + ".ttf"))


if __name__ == "__main__":
    main()
