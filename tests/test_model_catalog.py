from src.model_catalog import build_model_catalog
from routes.model_routes import _catalog_entitlement


def test_catalog_keeps_state_capabilities_and_legacy_projection_metadata():
    catalog = build_model_catalog(
        endpoint_id="mimo",
        endpoint_url="mimo://acp",
        model_ids=["openai/gpt-5", "openai/gpt-5/low"],
        primary_ids=["openai/gpt-5"],
        extra_ids=["openai/gpt-5/low"],
        entitled=True,
        families={"openai/gpt-5": "OpenAI"},
        capabilities={"chat": True, "tools": True, "vision": None},
    )

    assert catalog == [
        {
            "endpoint_id": "mimo",
            "endpoint_url": "mimo://acp",
            "model_id": "openai/gpt-5",
            "display_name": "openai/gpt-5",
            "family": "OpenAI",
            "discovered": True,
            "entitled": True,
            "compatible": True,
            "curated": True,
            "hidden": False,
            "stale": False,
            "capabilities": {"chat": True, "tools": True, "vision": None},
            "compatibility": {},
            "reason": None,
        },
        {
            "endpoint_id": "mimo",
            "endpoint_url": "mimo://acp",
            "model_id": "openai/gpt-5/low",
            "display_name": "openai/gpt-5/low",
            "family": None,
            "discovered": True,
            "entitled": True,
            "compatible": True,
            "curated": False,
            "hidden": False,
            "stale": False,
            "capabilities": {"chat": True, "tools": True, "vision": None},
            "compatibility": {},
            "reason": None,
        },
    ]


def test_catalog_keeps_entitlement_separate_from_compatibility():
    catalog = build_model_catalog(
        endpoint_id="subscription",
        endpoint_url="https://chatgpt.com/backend-api",
        model_ids=["entitled-chat", "api-only", "hidden"],
        entitled=True,
        hidden_ids=["hidden"],
        compatibility={"compatible": False},
    )

    assert all(entry["entitled"] is True for entry in catalog)
    assert all(entry["compatible"] is False for entry in catalog)
    assert catalog[0]["reason"] == "incompatible"
    assert catalog[2]["reason"] == "hidden"


def test_only_chatgpt_subscription_discovery_is_marked_entitled():
    assert _catalog_entitlement("https://chatgpt.com/backend-api/codex", ["new-slug"]) is True
    assert _catalog_entitlement("https://api.openai.com/v1", ["api-model"]) is None
    assert _catalog_entitlement("https://chatgpt.com/backend-api/codex", []) is False
