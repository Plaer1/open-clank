# Contributing to Open Clank

Thanks for helping. The project is moving quickly, so the best contributions are focused, easy to review, and easy to test.

## Branch model

The public default branch is **`main`**; target focused pull requests there.
This is Beta 1, the first of several betas. macOS is the current build/dogfood
focus. Windows and Linux Files/native integration remains unfinished; support
is coming as soon as possible, without a date or parity promise. Build artifacts
do not establish platform acceptance. See [known limits](docs/known-limits.md).

## Before You Start

- Search existing issues and pull requests before opening a new one.
- Prefer one bug fix or feature per pull request.
- Avoid broad rewrites, formatting-only changes, or moving many files unless the issue is specifically about structure.
- If you want to work on a large feature, open an issue first and describe the approach.

## Setup

Follow [setup](docs/setup.md) for fresh native installation, and
[upgrading](docs/upgrading.md) before using existing data. macOS native is the
current dogfood path. A disposable Compose environment is useful for contributor
checks that specifically concern container behavior:

```bash
git clone https://github.com/Plaer1/open-clank.git
cd open-clank
cp .env.example .env
docker compose up -d --build
```

Manual development uses Python 3.11+:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
openclank server start
openclank server status
# stop this CLI-managed service when finished:
openclank server stop
```

For a foreground source run use
`python scripts/openclank_bootstrap.py serve --host 127.0.0.1 --port 7777`.
Bootstrap validates engine/current-store state before app import. Setup may
fetch pinned Bun/build dependencies; native Files/History/memory/Copal workers
have separate artifact requirements. Neither Windows nor Linux is currently a
promise of complete Files/native parity. Authentication is required in local
development too.

## Running Checks

Use [tests/README.md](tests/README.md) to select the smallest meaningful checks
for the affected behavior. Typical focused commands include:

```bash
python -m pytest tests/<relevant-test>.py
python -m py_compile <changed-python-file>.py
node --check static/js/<file-you-changed>.js
```

For Docker-related changes:

```bash
docker compose config
docker compose up -d --build
docker compose logs --tail=120 odysseus
```

Mention what you ran in the pull request description. If you could not run a check, say so.

## Pull Requests

Good pull requests usually include:

- A short explanation of the bug or feature.
- The files or areas changed.
- Manual test steps or automated test results from running the actual app, not just the test suite.
- Screenshots or short recordings for UI changes.
- Links to related issues, for example `Fixes #123`.

Please keep PRs small. Large PRs that mix unrelated cleanup, formatting, refactors, and behavior changes are much harder to review.

> **Auto-generated PRs.** If you are running an LLM agent (Devin, Cursor, OpenHands, etc.) against this repo: please open an issue describing the problem first instead of opening a PR directly. Bulk agent-generated PRs that don't match the project's visual style or contribution format will be closed without review, even when the underlying fix is correct.

## Style and visual changes

Open Clank has an intentional visual style. PRs that ignore it will be closed without merge, no matter how correct the underlying code is.

Before submitting any change that affects what the app looks like — buttons, icons, fonts, colors, spacing, layout, CSS, HTML, SVG, or any `static/js/` module that draws to the DOM — please:

1. **Run the app locally** and view the change in a browser. Type-checks and unit tests are not enough.
2. **Attach a screenshot or short clip** of the change in the running app. Add a mobile screenshot too if the change affects mobile.
3. **Match the existing visual language.** Specifically:
   - Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …). Do not introduce new color values, font sizes, or spacing units.
   - Reuse existing button, input, card, and border classes. Don't invent parallel styling for similar widgets.
   - **No Unicode emoji in UI or code.** Use inline SVG (matching the monochrome icon style already in `static/index.html`) or plain text.
   - Use the active theme's font tokens. Clanker themes use the bundled Liga Comic Mono.
   - Dark theme is the default; any light-mode work goes through the existing theme system, not hard-coded.
4. **Don't add parallel components.** If a similar widget already exists in the app, extend it instead of writing a new one.

If you are unsure whether a change is "visual," it is. Default to attaching a screenshot.

## Code conventions

Don't hardcode values that the project already exposes through a constant or a helper. Hardcoded literals drift out of sync, break on non-default deployments, and reintroduce bugs we've already fixed.

- **Filesystem paths:** never build writable paths from `Path(__file__)...` into the source tree, hardcode `/app/...`, or use a relative `"data/..."` string. Every persisted file and directory has a named constant in `src/constants.py` (for example `AUTH_FILE`, `USER_PREFS_FILE`, `SETTINGS_FILE`, `TTS_CACHE_DIR`). Import and use that named constant; do not re-derive the path locally with `os.path.join(DATA_DIR, "x.json")` or `DATA_DIR / "x.json"`. `DATA_DIR` resolves `OPEN_CLANK_DATA_DIR` first, accepts the legacy
  `ODYSSEUS_DATA_DIR` alias and normalizes both to the same root, so use it directly only for dynamic paths that have no fixed name (for example per-owner files). If a data file or directory has no constant yet, add one to `src/constants.py`. The source tree is read-only in Docker and `/app/...` does not exist on native runs; guard directory creation so an unwritable path degrades gracefully instead of crashing at import.
- **Internal API / loopback URLs:** don't hardcode `http://localhost:7777`. Use `internal_api_base()` from `src.constants` (it honors `ODYSSEUS_INTERNAL_BASE` / `APP_PORT`).
- **Ports, limits, model lists, and similar:** reuse the existing constant if one exists; if it doesn't and the value is used in more than one place, add a constant rather than copying the literal.

If you need a value that has no constant or helper yet, add it to `src/constants.py` (the single source of truth for paths and config; `core/constants.py` only re-exports it for backward compatibility) and import it, rather than repeating a literal across files.

**Commits:** use [Conventional Commits](https://www.conventionalcommits.org), `type(scope): summary` (e.g. `fix(search): ...`, `feat(notes): ...`, `docs(contributing): ...`). Common types: `fix`, `feat`, `refactor`, `docs`, `test`, `chore`, `ci`. Keep the subject short and imperative; put the "why" in the body when it isn't obvious.

## Issue Reports

For bugs, include:

- Machine version and installed revision (Beta 1 label currently accompanies `1.0.2`).
- Install method: native launcher, source/CLI, packaged build, Compose, WSL, etc.
- OS, browser, and device if relevant.
- Exact steps to reproduce.
- Expected behavior and actual behavior.
- Logs, screenshots, or terminal output.

For model-serving issues, include:

- Backend: Ollama, vLLM, SGLang, llama.cpp, LM Studio, etc.
- Model name.
- GPU/CPU and operating system.
- Cookbook task logs or server logs.

Issues with only "help", "does not work", or a screenshot without context may be closed as not actionable.

## Security

Do not post secrets, API keys, private logs, personal documents, or public IPs in issues or pull requests.

For security reports, follow [SECURITY.md](SECURITY.md).

## Private workspaces and release inputs

Keep every singular `.clanker/` directory private, including contracts, tools,
plans and notes. Use public source/docs links in contributions; a private local
receipt is not a working GitHub documentation link. Preserve exact recoverable
preimages before changing dirty work. Keep user data, runtime credentials and
operator recovery material out of commits, images and package exports.

Review an explicit file list before staging. Publication checks cover the
selected index/history and assembled export separately; ignore rules cannot
sanitize prior commits. Never mirror local recovery/evidence refs. Focused
artifact/link/UI checks are appropriate for documentation and export changes;
run behavior checks when the changed behavior requires them.
