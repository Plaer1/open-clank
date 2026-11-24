import sys
sys.path.insert(0, ".")
from src.openclank.memory_context_admission import (FTS_ORDINAL_KEY, ROUTE_KIND, S07ContextEvidence,
                                                    pop_fts_ordinal, s07_features)
from src.memery_ranking_feedback import encode_features

KEYS = {"v", "fts_ordinal", "route_kind", "directness", "entity_match", "freshness"}


def _hits():
    # five FTS hits; positions 1 and 3 were dropped by qualification, survivors carry 0, 2, 4
    return [{"record": {"id": f"c{i}", "content": f"secret text {i}"}, FTS_ORDINAL_KEY: i} for i in (0, 2, 4)]


def _ordinals(hits):
    return {h["record"]["id"]: pop_fts_ordinal(h) for h in hits}


def test_ordinals_are_pre_drop_positions():
    f = s07_features(_ordinals(_hits()), (("c0", 1), ("c2", 2), ("c4", 3)))
    assert [f[k]["fts_ordinal"] for k in ("c0", "c2", "c4")] == [0, 2, 4]


def test_only_delivered_have_entries():
    f = s07_features(_ordinals(_hits()), (("c0", 1), ("c4", 3)))
    assert set(f) == {"c0", "c4"}


def test_missing_ordinal_gets_no_entry():
    assert s07_features({"c0": 0}, (("c0", 1), ("zz", 2))).keys() == {"c0"}
    assert pop_fts_ordinal({"record": {}}) is None
    assert pop_fts_ordinal({FTS_ORDINAL_KEY: True}) is None
    assert pop_fts_ordinal({FTS_ORDINAL_KEY: -1}) is None
    assert pop_fts_ordinal("not a dict") is None


def test_shape_exact_and_unknowns_none():
    e = s07_features({"c0": 3}, (("c0", 1),))["c0"]
    assert set(e) == KEYS and e["v"] == 1 and type(e["fts_ordinal"]) is int
    assert e["route_kind"] == ROUTE_KIND == "curated_fts"
    assert e["directness"] is None and e["entity_match"] is None and e["freshness"] is None


def test_feeds_encode_features():
    e = s07_features({"c0": 3}, (("c0", 1),))["c0"]
    x = encode_features(e, [ROUTE_KIND])
    assert type(x) is list and len(x) == 9


def test_no_text_in_features():
    hits = _hits()
    f = s07_features(_ordinals(hits), (("c0", 1), ("c2", 2), ("c4", 3)))
    assert "secret" not in repr(f) and "content" not in repr(f)
    for e in f.values():
        assert all(type(v) in (int, str, type(None)) for v in e.values())


def test_hits_restored_to_original_keys():
    hits = _hits()
    _ordinals(hits)
    assert all(set(h) == {"record"} for h in hits)


def test_old_constructor_still_works():
    ev = S07ContextEvidence("o", "a", None, None, None, ())
    assert ev.features is None
    assert S07ContextEvidence("o", "a", None, None, None, (), {"c0": {}}).features == {"c0": {}}
