from src.hex_contract import statement


@statement(
    "canonical_layout_paths",
    help="Reject legacy and singular/plural typo Clanker namespaces.",
)
def canonical_layout_paths(param, scope):
    """Keep new tracked and generated paths on the canonical vocabulary."""
    if not param:
        return None
    bad_prefixes = (
        ".futures/",
        ".robonotes/",
        "robonotes/",
        ".clankers/futures/",
        ".clanker/hexes/",
        ".clanker/robonotes/",
    )
    offenders = sorted(path for path in scope.all_files if path.startswith(bad_prefixes) and scope.exists(path))
    return "reject legacy or typo Clanker paths: " + ", ".join(offenders[:5]) if offenders else None


@statement(
    "background_animation_lifecycle",
    help="Keep canvas backgrounds single-looped when their pattern is reapplied.",
)
def background_animation_lifecycle(param, scope):
    """Guard the shared canvas lifecycle and browser acceptance coverage."""
    if not isinstance(param, dict):
        return "configure background_animation_lifecycle with theme_module and browser_test paths"
    theme_module = param.get("theme_module")
    browser_test = param.get("browser_test")
    if not isinstance(theme_module, str) or not isinstance(browser_test, str):
        return "configure string theme_module and browser_test paths for background_animation_lifecycle"
    theme_source = scope.read_text(theme_module)
    browser_source = scope.read_text(browser_test)
    if theme_source is None or browser_source is None:
        return "keep the shared theme module and browser lifecycle test in this Hex's scope"
    theme_markers = (
        "function _activeBgPattern()",
        "const samePattern = _activeBgPattern() === p;",
        "const hasActiveOwner = window[_BACKGROUND_OWNER_KEY] || _activeBackgroundEffectDispose;",
        "function scheduleFrame()",
        "if (!disposed && !motion.matches && !animationFrame)",
        "animationFrame = 0;",
        "const nextViewportKey = `${window.innerWidth}:${window.innerHeight}:${window.devicePixelRatio || 1}`;",
        "let resizeTimer = 0;",
        "window.clearTimeout(resizeTimer);",
        "paint(motion.matches ? 0 : animationTime, motion.matches);",
        "function _initClankerEmojiDrift()",
        "id: 'clanker-emoji-drift-canvas'",
    )
    if any(marker not in theme_source for marker in theme_markers):
        return "preserve the shared theme background lifecycle and single RAF owner"
    test_markers = (
        "const canvasCadenceStable",
        "rendered twice inside a single frame",
        "'clanker-emoji-drift':'clanker-emoji-drift-canvas'",
    )
    if any(marker not in browser_source for marker in test_markers):
        return "keep browser coverage that proves the shared RAF owner is single-looped"
    return None
