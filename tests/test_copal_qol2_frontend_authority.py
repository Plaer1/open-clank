from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mounted_editor_uses_facade_refs_for_production_workspace_flow():
    source = (ROOT / "static/js/codeEditor.js").read_text()

    assert "filesFacadeClient.roots" in source
    assert "filesFacadeClient.children" in source
    assert "filesFacadeClient.openResource" in source
    assert "filesFacadeClient.workspaceResource" in source
    assert "filesFacadeClient.saveResource" in source
    assert "filesFacadeClient.action" in source
    assert "filesFacadeClient.transferResources" in source
    assert "filesFacadeClient.createDirectory" in source
    assert "state.rootResourceRef" in source
    assert "resourceRef: opaqueRef" in source

    # Mounted Editor operations require an opaque ref before any request.
    assert "if (!opaqueRef)" in source
    assert "const response = await filesFacadeClient.openResource(buffer.resourceRef" in source
    assert "const rootRef = String(target?.parent?.ref || target?.resource?.ref || '').trim()" in source
    assert "filesFacadeClient.workspaceResource(workspaceId, '', {})" in source
    assert source.count("filesFacadeClient.workspaceResource(workspaceId, '', {})") >= 2
    assert "state.rootResourceRef = ''" in source
    assert "resolveEditorRelativeResource(relative)" in source
    assert "openFile(childPath(state.root, relative), null, resolved.resourceRef)" in source
    assert "filesServiceClient" not in source
    assert "/api/odysseus-files" not in source
    workspace_root = source[source.index("async function workspaceRoot") : source.index("const saved = savedWorkspaceRoot", source.index("async function workspaceRoot"))]
    assert "filesFacadeClient.roots" in workspace_root
    assert "No authorized Files folder is available" in workspace_root
    assert "resolveWorkspaceRoot(" not in workspace_root
    assert "Choose this item from the authorized Files view before opening it in Editor." in source
    assert "resource_reference_missing" in source

    policy = source[source.index("async function handleCodeFilePolicyChanged") : source.index("/** Open one opaque Host resource", source.index("async function handleCodeFilePolicyChanged"))]
    stale_root = policy[policy.index("} else if (priorRoot)") : policy.index("} else {", policy.index("} else if (priorRoot)"))]
    assert stale_root.index("return false") < stale_root.index("resolveWorkspaceRoot")
    assert "Choose the folder through the authorized Files picker." in source


def test_files_facade_rows_cannot_fall_back_to_host_paths_when_opening():
    source = (ROOT / "static/js/files.js").read_text()
    start = source.index("async function openManagedDirectory")
    end = source.index("async function reloadManagedColumn", start)
    managed_open = source[start:end]

    assert "filesFacadeClient.children(resourceRef" in managed_open
    assert "openDirectory(entry.path)" not in managed_open
    assert "entry.provider === 'host' && entry.path" not in managed_open


def test_external_directory_collection_preserves_nested_relative_identity_for_facade_imports():
    source = (ROOT / "static/js/files.js").read_text()
    client = (ROOT / "static/js/filesFacadeClient.js").read_text()
    start = source.index("async function collectExternalDirectory")
    end = source.index("async function collectExternalDropFiles", start)
    collection = source[start:end]

    assert "relativePath: childParts.join('/')" in collection
    assert "nested_directory_unsupported" not in collection
    assert "safeExternalDropPath(childParts)" in collection
    assert "relative_path: relative" in client
    assert "async function prepareImportedDirectories" in source
    assert "filesFacadeClient.createDirectory(parent.ref" in source
    assert "expectedRevision" in source
    assert "collision: 'reuse'" in source
    assert "filesFacadeClient.operationReceipt(operation" in source
    assert "importedDirectoryOperationId(batchId" in source
    assert "destinationRef: directory.ref" in source
    assert "relativePath: file.name" in source
    assert "limits?.create_directory !== true" in source


def test_mounted_files_never_rehydrates_path_only_history_or_startup_roots():
    source = (ROOT / "static/js/files.js").read_text()
    navigation = source[source.index("async function loadNavigationRoots"):source.index("function renderFavorites", source.index("async function loadNavigationRoots"))]
    opening = source[source.index("async function open()", source.index("function mount()")):source.index("/** Reveal one Workspace-relative", source.index("async function open()"))]
    history = source[source.index("async function restoreFilesHistory"):source.index("function renderFavorites")]

    assert "loadLegacyNavigationRoots" not in navigation
    assert "openDirectory(" not in opening
    assert "saved Files location has expired" in history
    assert "No authorized Host folder is available" in opening

    auth = source[source.index("async function handleAuthUserReady") : source.index("function mount()", source.index("async function handleAuthUserReady"))]
    favorites = source[source.index("async function loadFavorites") : source.index("async function loadLegacyNavigationRoots")]
    preview = source[source.index("async function previewEntry") : source.index("async function openDirectory")]
    assert "openDirectory(" not in auth
    assert "filesServiceClient" not in favorites[: favorites.index("// Raw path favorites")]
    assert "filesServiceClient" not in preview
    policy = source[source.index("async function handleFilesPolicyChanged") : source.index("async function handleAuthUserReady")]
    assert "snapshot.provider === 'host' && snapshot.hostPath" in policy
    assert "refreshRawFilesProjection(snapshot, lifecycle)" in policy
    assert "saved folder has expired" in policy
