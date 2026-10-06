"""Personal-directory indexing must not block the request event loop."""

import asyncio

from routes import personal_routes
from src.request_models import DirectoryRequest


class _PersonalDocs:
    def __init__(self):
        self.directories = []

    def add_directory(self, directory, *, index=False):
        self.directories.append((directory, index))


class _Rag:
    def __init__(self):
        self.calls = []

    def index_personal_documents(self, directory, *, owner=None):
        self.calls.append((directory, owner))
        return {"success": True, "indexed_count": 3, "failed_count": 0}


def _add_directory_endpoint(personal_docs, rag):
    router = personal_routes.setup_personal_routes(personal_docs, rag, True)
    for route in router.routes:
        if route.path == "/api/personal/add_directory":
            return route.endpoint
    raise AssertionError("personal add-directory endpoint not found")


def test_add_directory_indexes_through_worker_thread(monkeypatch, tmp_path):
    personal_root = tmp_path / "personal"
    personal_root.mkdir()
    target = personal_root / "admitted"
    target.mkdir()
    personal_docs = _PersonalDocs()
    rag = _Rag()
    calls = []

    async def immediate_to_thread(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr(personal_routes, "PERSONAL_DIR", str(personal_root))
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)
    monkeypatch.setattr(personal_routes.asyncio, "to_thread", immediate_to_thread)

    result = asyncio.run(
        _add_directory_endpoint(personal_docs, rag)(
            request=object(),
            directory_request=DirectoryRequest(directory=str(target)),
            owner="alice",
            _admin=None,
        )
    )

    assert result["success"] is True
    assert calls == [(rag.index_personal_documents, (str(target),), {"owner": "alice"})]
    assert rag.calls == [(str(target), "alice")]
    assert personal_docs.directories == [(str(target), False)]
