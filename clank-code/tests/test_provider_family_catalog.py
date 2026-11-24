"""Parity and safety contracts for the portable provider-family bundle."""

from pathlib import Path
import shutil

import pytest

from src.openclank.provider_family_catalog import (
    ProviderFamilyCatalogError,
    bundled_provider_family_catalog,
    generate_provider_family_catalog,
)


ROOT = Path(__file__).resolve().parents[1]


def _families_by_id(payload):
    return {family["id"]: family for family in payload["families"]}


def test_bundled_family_catalog_matches_pinned_engine_inputs():
    bundled = bundled_provider_family_catalog()
    generated = generate_provider_family_catalog(ROOT)

    assert bundled == generated
    assert bundled["schema_version"] == 1
    assert bundled["catalog_key"].startswith("pfcat_")
    assert len(bundled["catalog_key"]) == len("pfcat_") + 64
    assert bundled["catalog_identity"]["model_catalog_sha256"] == (
        "33f836f532fd8ada58f255f11030ff5500d45b0cf7e63587772221050ffb1f48"
    )
    assert len(bundled["families"]) == 88
    assert "google-vertex" not in _families_by_id(bundled)["google"]["adapters"]


def test_bundle_never_invents_dynamic_oauth_methods():
    payload = bundled_provider_family_catalog()
    families = _families_by_id(payload)

    assert families["openai"]["auth_methods"] == [
        {"id": "api_key", "type": "api", "label": "API key", "acquisition_method": "manual-key", "credential_kinds": ["api"], "billing_lanes": ["metered_api", "custom"]}
    ]
    assert families["openai"]["auth_methods_complete"] is False
    assert families["github-copilot"]["auth_methods"] == []
    assert families["github-copilot"]["auth_methods_complete"] is False
    assert families["ollama"]["auth_methods"] == [
        {"id": "api_key", "type": "api", "label": "API key", "acquisition_method": "manual-key", "credential_kinds": ["api"], "billing_lanes": ["local"]},
        {"id": "none", "type": "none", "label": "No credential"},
    ]
    assert families["xiaomi"]["endpoint_policies"] == {
        "metered_api": "mimo-metered-api-v1",
        "subscription": "mimo-token-plan-v1",
    }
    assert families["opencode-console"]["adapters"] == ["opencode-native"]
    assert families["opencode-console"]["billing_lanes"] == ["metered_api"]
    assert families["opencode-console"]["endpoint_policies"] == {
        "metered_api": "opencode-console-inference-v1",
    }
    assert families["opencode-go"]["adapters"] == ["opencode-native"]
    assert families["opencode-go"]["billing_lanes"] == ["subscription"]
    assert families["opencode-go"]["endpoint_policies"] == {
        "subscription": "opencode-go-inference-v1",
    }
    assert families["xai"]["billing_lanes"] == ["metered_api", "subscription", "custom"]
    assert families["grok"]["adapters"] == ["grok-cli-chat"]
    assert families["grok"]["billing_lanes"] == ["subscription"]
    assert families["grok"]["endpoint_policies"] == {"subscription": "grok-cli-proxy-v1"}
    assert families["kimi-code-plan-cn"]["adapters"] == ["models-dev-openai-compatible"]
    assert families["kimi-code-plan-cn"]["billing_lanes"] == ["subscription"]
    assert families["kimi-code-plan-cn"]["endpoint_policies"] == {"subscription": "kimi-code-plan-cn-v1"}
    assert families["kimi-code-plan-cn"]["auth_methods"][0]["label"] == "Kimi Code plan key"
    assert families["kimi-code-plan-global"]["endpoint_policies"] == {"subscription": "kimi-code-plan-global-v1"}
    assert families["kilo"]["display_name"] == "Kilo Gateway"
    assert families["kilo"]["adapters"] == ["models-dev-openai-compatible"]
    assert families["kilo"]["billing_lanes"] == ["metered_api"]
    assert len([family for family in payload["families"] if family["id"] == "kilo"]) == 1
    coding_plan = families["zai-coding-plan"]
    assert coding_plan["billing_lanes"] == ["subscription"]
    assert coding_plan["auth_methods_complete"] is True
    assert coding_plan["auth_methods"][0]["label"] == "Z.AI Coding Plan API key"
    assert coding_plan["endpoint_policies"] == {"subscription": "zai-coding-plan-openai-v1"}
    assert families["zai"]["billing_lanes"] == ["metered_api"]
    assert families["zhipuai"]["billing_lanes"] == ["metered_api"]
    assert len([family for family in payload["families"] if family["id"] == "zai-coding-plan"]) == 1
    for family_id in (
        "xiaomi",
        "opencode-go",
        "kimi-code-plan-cn",
        "kimi-code-plan-global",
        "llmgateway-devpass",
        "zai-coding-plan",
    ):
        assert "subscription" in families[family_id]["auth_methods"][0]["billing_lanes"]
    for family_id in ("openai", "anthropic"):
        assert "subscription" not in families[family_id]["auth_methods"][0]["billing_lanes"]
    assert families["llmgateway"]["billing_lanes"] == ["metered_api"]
    assert families["llmgateway-devpass"]["billing_lanes"] == ["subscription"]
    assert families["llmgateway-devpass"]["auth_methods"][0]["label"] == "DevPass API key"
    assert len([family for family in payload["families"] if family["id"] == "llmgateway"]) == 1
    assert families["kimi-for-coding"]["auth_methods_complete"] is True
    assert all(
        method["type"] in {"api", "none"}
        for family in payload["families"]
        for method in family["auth_methods"]
    )


def test_bundle_returns_defensive_copies():
    first = bundled_provider_family_catalog()
    first["families"][0]["display_name"] = "tampered"
    first["catalog_identity"]["managed_acp_version"] = 999

    second = bundled_provider_family_catalog()

    assert second["families"][0]["display_name"] != "tampered"
    assert second["catalog_identity"]["managed_acp_version"] == 1


def test_generator_rejects_model_snapshot_hash_mismatch(tmp_path):
    source_vendor = ROOT / "packages" / "mimo-code"
    target_vendor = tmp_path / "packages" / "mimo-code"
    model_relative = Path("packages/cli/test/tool/fixtures/models-api.json")
    (target_vendor / model_relative.parent).mkdir(parents=True)
    shutil.copy2(source_vendor / "openclank-vendor.json", target_vendor)
    shutil.copy2(source_vendor / model_relative, target_vendor / model_relative)
    with (target_vendor / model_relative).open("ab") as handle:
        handle.write(b"\n")

    with pytest.raises(
        ProviderFamilyCatalogError,
        match="catalogue inputs do not match",
    ):
        generate_provider_family_catalog(tmp_path)
