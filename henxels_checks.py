from henxels import statement


@statement(
    "background_animation_lifecycle",
    help="Keep canvas backgrounds single-looped and stable when their pattern is reapplied.",
)
def background_animation_lifecycle(param, scope):
    """Guard the shared canvas lifecycle and its browser acceptance coverage."""
    if not isinstance(param, dict):
        return "configure background_animation_lifecycle with theme_module and browser_test paths"

    theme_module = param.get("theme_module")
    browser_test = param.get("browser_test")
    if not isinstance(theme_module, str) or not isinstance(browser_test, str):
        return "configure string theme_module and browser_test paths for background_animation_lifecycle"

    theme_source = scope.read_text(theme_module)
    browser_source = scope.read_text(browser_test)
    if theme_source is None or browser_source is None:
        return "keep the shared theme module and its browser lifecycle test in this Henxel's scope"

    theme_markers = (
        "function _activeBgPattern()",
        "function _isCurrentBgPatternHealthy(pattern)",
        "if (_activeBgPattern() === p && _isCurrentBgPatternHealthy(p))",
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
    missing_theme = [marker for marker in theme_markers if marker not in theme_source]
    if missing_theme:
        return (
            "preserve the shared background lifecycle: retain a healthy unchanged pattern, "
            "guard requestAnimationFrame ownership, and ignore same-viewport resize events"
        )

    test_markers = (
        "const canvasSceneStable",
        "const canvasPatternStable",
        "const canvasCadenceStable",
        "route field rebuilt its scene for an unchanged pattern",
        "${pattern} rebuilt its scene for an unchanged pattern",
        "rendered twice inside a single frame",
        "'clanker-emoji-drift':'clanker-emoji-drift-canvas'",
    )
    if any(marker not in browser_source for marker in test_markers):
        return (
            "keep browser coverage that proves unchanged pattern selection and no-op resize "
            "do not replace a running canvas scene"
        )

    return None
