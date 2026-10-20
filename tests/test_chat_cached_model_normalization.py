from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_context_keeps_the_normalized_route_model_id():
    source = (ROOT / "routes" / "chat_helpers.py").read_text()

    assert "sess.model = str(getattr(sess, \"model\", \"\") or \"\").strip()" in source
    assert "def _normalize_model_id_from_cache" not in source
    assert "normalize_model_id(" not in source


def test_chat_context_does_not_probe_legacy_endpoint_model_caches():
    source = (ROOT / "routes" / "chat_helpers.py").read_text()

    assert "ModelEndpoint" not in source
    assert "cached_models" not in source
    assert "build_models_url" not in source
