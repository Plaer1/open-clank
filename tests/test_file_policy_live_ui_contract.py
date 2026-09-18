from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_code_policy_event_aborts_then_revalidates_without_treating_5xx_as_revocation():
    source = (ROOT / "static/js/codeEditor.js").read_text(encoding="utf-8")
    handler = section(
        source,
        "async function handleCodeFilePolicyChanged()",
        "async function handleAuthenticatedOwnerReady",
    )
    abort = section(
        source,
        "function abortWorkspaceAuthorityRequests()",
        "function confirmedWorkspaceFailure",
    )

    assert "document.addEventListener('openclank:file-policy-changed'" in source
    assert "buffer.saveController?.abort?.();" in abort
    assert "buffer.reloadController?.abort?.();" in abort
    assert handler.index("abortWorkspaceAuthorityRequests()") < handler.index("authenticatedOwner()")
    assert "identity.status === 'unavailable'" in handler
    assert "unsaved changes were retained" in handler
    assert "workspaceModule.resolveWorkspaceId(workspaceId, 'app_folder')" in handler
    assert "resolveWorkspaceRoot(priorRoot)" in handler
    assert "confirmedWorkspaceFailure(error)" in handler
    assert "purgeWorkspaceAuthority" in handler
    assert "const page = await loadInitialCodeRoot(resolvedRoot, generation);" in handler


def test_files_policy_event_transactionally_rebuilds_current_projection():
    source = (ROOT / "static/js/files.js").read_text(encoding="utf-8")
    handler = section(
        source,
        "async function handleFilesPolicyChanged()",
        "async function handleAuthUserReady",
    )
    loader = section(
        source,
        "async function loadNavigationRoots(options = {})",
        "function renderFavorites",
    )
    managed = section(
        source,
        "async function refreshManagedFilesProjection",
        "async function handleFilesPolicyChanged",
    )

    assert "void handleFilesPolicyChanged();" in source
    assert handler.index("abortFilesAuthorityRequests()") < handler.index("loadNavigationRoots({ force: true })")
    assert "refreshRawFilesProjection(snapshot, lifecycle)" in handler
    assert "refreshManagedFilesProjection(snapshot, lifecycle)" in handler
    assert "result.status === 'unavailable'" in handler
    assert "the prior view was retained" in handler
    assert "clearFilesAuthorityContent" in handler
    assert "current.resource_ref" in managed
    assert "entry.resource_id === prior.resourceId" in managed
    assert "Number(error?.status || 0) !== 404" in loader


def test_settings_announces_each_completed_policy_or_reset_mutation():
    source = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    reset = section(
        source,
        "async function initPermissionResetControls()",
        "function locationKindLabel",
    )
    visibility = section(
        source,
        "async function loadVisibilityAssignments",
        "async function loadFilesystemRoots",
    )
    roots = section(
        source,
        "async function loadFilesystemRoots()",
        "/* ═══════════════════════════════════════════",
    )

    assert "function announceFilePolicyChanged" in source
    assert "source: 'settings'" in source
    assert "mutation: 'permission-reset'" in reset
    assert "mutation: 'people-access'" in visibility
    assert "mutation: 'agent-access'" in roots
    assert "mutation: 'location-remove'" in roots

