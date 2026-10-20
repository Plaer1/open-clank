"""Focused S05 shell contract checks for the Usage destination."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _text(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_usage_is_a_shell_route_and_precached():
    app = _text("app.py")
    routes = _text("static/js/appletRoutes.js")
    sw = _text("static/sw.js")
    assert '"/usage"' in app
    assert "'/usage'" in routes
    assert "usage: '/usage'" in routes
    assert "usage:     { target: 'usage' }" in routes
    assert "'/usage'" in sw
    assert "/static/js/statsUsage.js" in sw


def test_usage_has_one_accessible_opener_and_preserves_legacy_namespaces():
    index = _text("static/index.html")
    usage = _text("static/js/statsUsage.js")
    entry = _text("static/js/usageEntry.js")
    app = _text("static/app.js")
    slash = _text("static/js/slashCommands.js")
    assert index.count('id="tool-usage-btn"') == 1
    assert 'role="button" tabindex="0"' in index
    assert "window.__openStatsUsage = openUsage" in entry
    assert "window.__openStatsUsage = openStatsUsage" not in usage
    assert "id: 'stats-usage-window'" in usage
    assert "usage:    () => window.__openStatsUsage?.()" in app
    assert "target === 'usage' || target === 'stats'" in slash
    # The existing slash commands remain separate product surfaces.
    assert "'/api/db/stats'" not in usage
    assert "cmd === 'usage'" in slash or "usage" in slash


def test_usage_scope_refresh_is_safe_and_single_coordinator():
    usage = _text("static/js/statsUsage.js")
    assert "SAFE_RANGES" in usage
    assert "localStorage" in usage
    assert "refreshController?.abort()" in usage
    assert "generation !== refreshGeneration" in usage
    assert "lastGood" in usage
    assert "createStatsRefreshLifecycle" in usage
    assert "statsLifecycle?.stop()" in usage
    assert "role=\"status\"" in usage
    assert "role=\"img\"" in usage
    assert usage.count("const VIEWS") == 1
    assert usage.count("createOpenClankWindow") == 2  # import + single construction


def test_quota_view_uses_owner_resource_and_keeps_ring_text_truthful():
    usage = _text("static/js/statsUsage.js")
    assert "/api/stats/v1/quota" in usage
    assert "quotaObservations" in usage
    assert "Math.max(0, Math.min(100, percent))" in usage
    assert "Usage unavailable" in usage
    assert "Official headline remains authoritative" in usage
    assert "No configured provider quota" in usage


def test_quota_surface_has_safe_filters_timeline_and_overview_semantics():
    usage = _text("static/js/statsUsage.js")
    assert "stats-overview-quota-strip" in usage
    assert "usage timeline" in usage
    assert "data-stats-filter=\"account\"" in usage
    assert "accountSelectionSignature" in usage
    assert "Official API capacity" in usage
    assert "threshold" in usage
    assert "percent >= 100 ? 'critical'" in usage
    assert "stats-usage-timeline-chart" in usage


def test_usage_shortcut_is_opt_in_and_does_not_change_slash_stats():
    shortcuts = _text("static/js/keyboard-shortcuts.js")
    settings = _text("static/js/settings.js")
    slash = _text("static/js/slashCommands.js")
    assert "open_usage: ''" in shortcuts
    assert "open_usage:    'tool-usage-btn'" in shortcuts
    assert "open_usage" in settings
    assert "stats" in slash


def test_official_account_usage_shortcuts_are_exact_and_safe_new_tab_actions():
    usage = _text("static/js/statsUsage.js")
    assert "https://claude.ai/settings/usage" in usage
    assert "https://chatgpt.com/codex/settings/usage" in usage
    assert "noopener,noreferrer" in usage
    assert "window.open(url, '_blank'" in usage
    assert "stats-official-usage" in usage


def test_capabilities_do_not_claim_mounted_resources_are_pending():
    source = _text("routes/stats_routes.py")
    assert '"pricing": {"status": "implemented"' in source
    assert '"activity": {"status": "implemented"' in source
    assert '"trends": {"status": "implemented"' in source
    assert '"quality": {"status": "implemented"' in source
    assert '"precision_logging": {"status": "unavailable"' in source
