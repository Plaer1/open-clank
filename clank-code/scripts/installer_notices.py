"""Collect the actual release's plain-text notices for offline installer readers."""
import argparse
import json
import os
import sys
import posixpath
import re
import tarfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TERMS = ROOT / "static/brand/open-clank/installer/terms.txt"


def is_notice(path):
    p = PurePosixPath(path)
    if p.suffix.lower() not in ("", ".md", ".txt", ".rst", ".html", ".apache", ".bsd", ".mit", ".gpl", ".psf"):
        return False
    return bool(re.search(r"(?:^|[-_.])(licen[cs]es?|copying|copyright|ofl|notices?|acknowledg[e]?ments|authors|credits)(?:$|[-_.])",
                          p.name, re.I) or "licenses" in p.parts)


def collect(payload, *, compiled_engine=False):
    docs = {}
    docs["ACKNOWLEDGMENTS.md"] = (ROOT / "ACKNOWLEDGMENTS.md").read_text(encoding="utf-8")
    docs["LICENSE"] = (ROOT.parent / "LICENSE").read_text(encoding="utf-8")
    for pattern in ("licenses/*", "static/fonts/*-OFL.txt", "static/js/copal/*LICENSE*",
                    "static/vendor/google-emoji/noto-svg/*LICENSE", "packages/mimo-code/LICENSE",
                    "packages/openclank-history/vendor/lore/LICENSE"):
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file():
                docs[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    mimo = "packages/mimo-code/"
    declarations = {}
    aliases = {}

    def wanted(name):
        return is_notice(name) or (name.startswith(mimo) and
                                   PurePosixPath(name).name in {"package.json", "SKILL.md"})

    def record(name, body):
        if is_notice(name):
            docs[name] = body
        elif name.endswith("package.json"):
            package = json.loads(body)
            if package.get("license"):
                declarations[name] = str(package["license"])
        elif body.startswith("---"):
            frontmatter = body.split("---", 2)[1]
            license_line = re.search(r"^license:[ \t]*(.+)$", frontmatter, re.M)
            if license_line:
                declarations[name] = license_line[1].strip()

    if payload.is_dir():
        for path in sorted(payload.rglob("*")):
            name = path.relative_to(payload).as_posix()
            if wanted(name) and path.is_file() and path.resolve().is_relative_to(payload.resolve()):
                record(name, path.read_text(encoding="utf-8", errors="replace"))
    else:
        with tarfile.open(payload, "r|gz") as archive:
            for member in archive:
                name = member.name.removeprefix("openclank/")
                if not wanted(name):
                    continue
                if member.isfile():
                    record(name, archive.extractfile(member).read().decode("utf-8", "replace"))
                elif member.issym() or member.islnk():
                    target = (posixpath.join(posixpath.dirname(name), member.linkname) if member.issym()
                              else member.linkname.removeprefix("openclank/"))
                    aliases[name] = posixpath.normpath(target)
        for name, target in aliases.items():
            if target in docs:
                docs[name] = docs[target]
    if compiled_engine:
        # The Windows portable ships a sealed engine instead of its source tree.
        # Use the same admitted source directories as the engine build.
        sys.path.insert(0, str(ROOT))
        from src.openclank.engine_build import _source_directory_excluded, _source_file_excluded

        vendor = ROOT / mimo
        for current, dirs, files in os.walk(vendor):
            rel = Path(current).relative_to(vendor)
            dirs[:] = sorted(d for d in dirs if not _source_directory_excluded(rel / d))
            for file in sorted(files):
                relative = rel / file
                name = mimo + relative.as_posix()
                if wanted(name) and not _source_file_excluded(relative):
                    source = vendor / relative
                    # Pinned source links retain POSIX separators on Windows.
                    # Open the resolved notice and credit its original alias.
                    if source.is_symlink():
                        source = source.parent / source.readlink()
                    source = source.resolve()
                    if source.is_relative_to(vendor.resolve()):
                        record(name, source.read_text(encoding="utf-8", errors="replace"))

    if declarations:
        lines = ["# MiMo Code package and skill licenses", "", "## Declared licenses", ""]
        lines.extend(f"- `{name[len(mimo):]}` — {value}" for name, value in sorted(declarations.items()))
        lines.extend(["", "## Component notices", ""])
        lines.extend(f"- [{name[len(mimo):]}]({name})" for name in sorted(docs) if name.startswith(mimo))
        docs["MiMo Code package and skill licenses.md"] = "\n".join(lines)
    return docs


def rtf_text(text):
    """Escape literal input, including non-BMP characters, for a Unicode RTF reader."""
    escaped = []
    for char in text:
        if char in "\\{}":
            escaped.append("\\" + char)
        elif char == "\n":
            escaped.append(r"\line ")
        elif char.isascii():
            escaped.append(char)
        else:
            units = char.encode("utf-16-le")
            for start in range(0, len(units), 2):
                unit = int.from_bytes(units[start:start + 2], "little", signed=True)
                escaped.append(rf"\u{unit}?")
    return "".join(escaped)


class CreditsRtf(HTMLParser):
    """Map rendered Markdown elements to RichEdit formatting, preserving link destinations."""
    style = {"h1": r"\b\fs30 ", "h2": r"\b\fs25 ", "h3": r"\b\fs23 ",
             "h4": r"\b ", "strong": r"\b ", "em": r"\i ", "code": r"\f1 ",
             "pre": r"\f1 ", "th": r"\b ", "li": r"\li220 \bullet\tab ", "a": r"\ul "}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.href = ""
        self.preformatted = False

    def handle_starttag(self, tag, attrs):
        if tag in self.style:
            self.parts.append("{" + self.style[tag])
        if tag == "a":
            self.href = dict(attrs).get("href", "")
        if tag == "pre":
            self.preformatted = True
        if tag == "br":
            self.parts.append(r"\line ")

    def handle_endtag(self, tag):
        if tag in self.style:
            self.parts.append("}")
        if tag == "a" and self.href:
            self.parts.append(" (" + rtf_text(self.href) + ")")
            self.href = ""
        if tag in ("p", "h1", "h2", "h3", "h4", "li", "tr", "pre"):
            self.parts.append(r"\par ")
        if tag in ("td", "th"):
            self.parts.append(r"\tab ")
        if tag == "pre":
            self.preformatted = False

    def handle_data(self, data):
        if self.preformatted or data.strip():
            self.parts.append(rtf_text(data if self.preformatted else re.sub(r"\s+", " ", data)))
        elif data == " ":
            self.parts.append(" ")


def windows_notices(directory, docs):
    import markdown

    directory.mkdir()
    header = (
        r"{\rtf1\ansi\uc1\deff0{\fonttbl{\f0 Comic Clank;}{\f1 Comic Clank Mono;}}"
        r"{\colortbl;\red255\green244\blue214;}\cf1\f0\fs21\sa85 "
    )
    terms = rtf_text(TERMS.read_text(encoding="utf-8"))
    (directory / "terms.rtf").write_text(header + terms + "}", encoding="ascii")
    names = []
    for number, (name, body) in enumerate(docs.items()):
        names.append(name.replace("\n", " ").replace("\r", " "))
        if name.lower().endswith(".md"):
            rich = CreditsRtf()
            rich.feed(markdown.markdown(body, extensions=["tables", "fenced_code"]))
            rich.close()
            body = "".join(rich.parts)
        else:
            body = rtf_text(body)
        (directory / f"notice-{number}.rtf").write_text(header + body + "}", encoding="ascii")
    (directory / "notice-list.txt").write_text("\n".join(names), encoding="utf-8-sig")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", action="store_true", help="write indexed RichEdit documents")
    parser.add_argument("payload", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    docs = collect(args.payload, compiled_engine=args.windows)
    if args.windows:
        windows_notices(args.out, docs)
    elif args.out.suffix == ".js":
        from markdown_it import MarkdownIt

        renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
        renderer.disable("image")
        rendered = {name: renderer.render(body) for name, body in docs.items() if name.lower().endswith(".md")}
        data = dict(terms=TERMS.read_text(), notices=docs, markdown=rendered)
        args.out.write_text("window.setupDocuments = " + json.dumps(data) + ";\n", encoding="utf-8")
    else:
        args.out.write_text("\n\n".join(f"{name}\n\n{body}" for name, body in docs.items()), encoding="utf-8-sig")
