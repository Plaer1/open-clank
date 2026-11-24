import ast
import dataclasses
from pathlib import Path
from types import SimpleNamespace

from src.agent_settings import (AdmittedGcfPreference, DEFAULT_AGENT_SETTINGS, FIELD_SPECS, admitted_gcf_preference,
                                agent_settings_revision, effective_agent_settings, validate_agent_settings)

KEY = "gcf_graph_references"
# Captured from the code before the setting existed.
OLD_SETTINGS = {"tokenomics": {"gcf_filtering": True, "gcf_same_request_rows": True}}
OLD_REVISION = "cbedf281a87d30a31b3a7ff4b19e8eec5598c1076b9581822be67e937628f53a"


def _snapshot(settings):
    return SimpleNamespace(providers={"p": {"models": {"m": {"options": {"_openclankAgentSettings": settings}}}}})


def test_default_is_false_everywhere():
    assert DEFAULT_AGENT_SETTINGS["tokenomics"][KEY] is False
    effective = effective_agent_settings({}, context_window=100000, max_output=4000)
    assert effective["tokenomics"][KEY] is False
    assert dataclasses.fields(AdmittedGcfPreference)[-1].name == "gcf_graph_references_requested"
    assert dataclasses.fields(AdmittedGcfPreference)[-1].default is False


def test_validates_like_its_sibling():
    sibling, mine = FIELD_SPECS["tokenomics.gcf_same_request_rows"], FIELD_SPECS["tokenomics." + KEY]
    assert sibling[:3] == mine[:3] == ("boolean", None, None)
    assert mine[3].startswith("Experimental. Sends a later Code Graph result as a difference")
    for value in (True, False, None):
        validate_agent_settings({"tokenomics": {KEY: value}})
    for value in ("yes", 1, 0, [], {}):
        for name in (KEY, "gcf_same_request_rows"):
            try:
                validate_agent_settings({"tokenomics": {name: value}})
            except ValueError:
                continue
            raise AssertionError((name, value))


def test_preference_unchanged_when_off():
    pref = admitted_gcf_preference(_snapshot(OLD_SETTINGS), "p", "m")
    assert pref.revision == OLD_REVISION == agent_settings_revision(OLD_SETTINGS)
    assert pref.gcf_same_request_rows_requested is True and pref.requested is True
    assert pref.gcf_graph_references_requested is False
    others = {f.name: getattr(pref, f.name) for f in dataclasses.fields(pref)
              if f.name not in ("revision", "requested", "gcf_same_request_rows_requested", "gcf_graph_references_requested", "provider_id", "model_id", "line_cap_rows")}
    assert others and all(v is False for v in others.values())


def test_preference_carries_the_setting_when_on():
    on = {"tokenomics": {"gcf_filtering": True, KEY: True}}
    pref = admitted_gcf_preference(_snapshot(on), "p", "m")
    assert pref.gcf_graph_references_requested is True and pref.requested is True
    assert pref.gcf_same_request_rows_requested is False
    assert pref.revision != OLD_REVISION


def test_bridge_admission_shape():
    source = Path("src/openclank/acp_bridge.py").read_text()
    # The unconditional admission literal is unchanged: no graphReferences key in it.
    start = source.index("prompt_meta['openclankGcfAdmission'] = {")
    literal = source[start:source.index("}", start) + 1]
    assert "graphReferences" not in literal and "'memoize':preference.gcf_memoization_requested" in literal
    guarded = source.index("prompt_meta['openclankGcfAdmission']['graphReferences'] = True")
    assert "getattr(preference, 'gcf_graph_references_requested', False) is True:" in source[guarded - 160:guarded]
    assert source.count("['graphReferences']") == 1
    ast.parse(source)
    reread = source[source.index("async def row_preference_current"):]
    reread = reread[:reread.index("except Exception: return False")]
    assert "gcf_graph_references_requested is not True" in reread
    assert "fresh.get('tokenomics', {}).get('gcf_graph_references') is True" in reread
    assert "preference.requested is not True" in reread
