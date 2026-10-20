"""Canonical browser applet addresses for Python-side URL emitters.

Mirrors ``static/js/appletRoutes.js`` so tool envelopes and generated links
never emit legacy ``/copal/*`` destinations. Those stay as compatibility
aliases only (server 302 redirect / client resolve input).
"""

from __future__ import annotations

# View name (Copal action prefix, /copal/<view> alias, registry segment) ->
# canonical direct applet path, including the query state that distinguishes
# editor-family leaves (Bases) and graph modes (Mind/Galaxy).
_CANONICAL_APPLET_PATH: dict[str, str] = {
    "notes": "/editor",
    "editor": "/editor",
    "code": "/editor",
    "bases": "/editor?open=bases",
    "base": "/editor?open=bases",
    "wiki": "/wiki",
    "timeline": "/timeline",
    "todo": "/todo",
    "graph": "/graph",
    "mind": "/graph?mode=mind",
    "galaxy": "/graph?mode=galaxy",
    "treehouse": "/treehouse",
    "files": "/files",
    "calendar": "/calendar",
}


def canonical_applet_path(view: str | None) -> str | None:
    """Canonical direct path for a view name, or None when it is not an applet."""
    return _CANONICAL_APPLET_PATH.get(str(view or "").lower())


def canonical_open_url(view: str | None, doc_id: str | None = None) -> str | None:
    """Canonical address for a view, optionally focused on one document.

    Returns None for non-applet views (e.g. trash/maintenance) so callers can
    omit a navigation hint rather than emit a dead legacy alias.
    """
    path = canonical_applet_path(view)
    if path is None:
        return None
    if doc_id:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}doc={doc_id}"
    return path
