"""Retained candidate-shaping tests for the fail-closed fallback guards."""

from src import llm_core


def test_dedupe_candidates_keeps_first_of_each_route():
    candidates = [
        ("u1", "m1", {"h": 1}),
        ("u1", "m1", {"h": 2}),
        ("u2", "m2", {}),
        ("", "m3", {}),
        ("u3", "", {}),
    ]
    assert llm_core._dedupe_candidates(candidates) == [("u1", "m1", {"h": 1}), ("u2", "m2", {})]
    assert llm_core._dedupe_candidates([]) == []
    assert llm_core._dedupe_candidates(None) == []
