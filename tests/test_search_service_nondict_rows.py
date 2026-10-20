import asyncio

import services.search.service as svc_mod
from services.search.service import SearchService

def test_search_skips_non_dict_results(monkeypatch):
    def fake_provider(*_args):
        return [
            {"url": "https://a.com", "title": "A"},
            "junk-row",
            None,
            {"url": "https://b.com", "title": "B"},
        ]
    monkeypatch.setattr(svc_mod, "_call_provider", fake_provider)
    monkeypatch.setattr(svc_mod, "_get_search_settings", lambda: {"search_provider": "searxng"})
    svc = SearchService(fetch_content=False)
    res = asyncio.run(svc.search("anything"))
    assert [r.url for r in res.results] == ["https://a.com", "https://b.com"]
    assert res.total == 2
