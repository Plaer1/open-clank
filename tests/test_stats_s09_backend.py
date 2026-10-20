import threading
import time

import pytest

from services.stats.quality import FORMULA_VERSION, QualityError, capability, project_quality
from services.stats.trends import TrendError, parse_term_groups, project_trends


def test_trends_are_opt_in_literal_bounded_and_rate_limited():
    messages = [{"owner": "alice", "text": "Load-bearing seam; blast radius."}, {"owner": "alice", "text": "LOAD BEARING seam."}]
    assert project_trends(messages, owner="alice")["state"] == "unavailable"
    result = project_trends(messages, owner="alice", content_opt_in=True,
                             terms="load bearing | load-bearing\nseam")
    assert result["message_count"] == 2
    assert result["groups"][0]["occurrences"] == 2
    assert result["groups"][1]["per_1000_messages"] == 1000.0
    assert "text" not in str(result)


def test_trends_treat_injection_terms_as_literals_and_cancel():
    term = "x.*|DROP TABLE stats"
    result = project_trends([{"owner": "a", "content": "x.* DROP TABLE stats"}], owner="a", content_opt_in=True, terms=term)
    assert result["groups"][0]["occurrences"] == 2
    event = threading.Event(); event.set()
    with pytest.raises(TrendError, match="cancelled"):
        project_trends([{"owner": "a", "text": "x"}], owner="a", content_opt_in=True, terms="x", cancel_event=event)


def test_quality_is_versioned_coverage_aware_and_evidence_bounded():
    result = project_quality([
        {"owner": "alice", "prompt_unverified": True, "context_pressure": False,
         "abandoned": False, "tool_failure": False, "evidence_id": "ordinal-1", "secret": "ignore"},
        {"owner": "alice", "prompt_unverified": False, "context_pressure": False,
         "abandoned": False, "tool_failure": True, "evidence_id": "ordinal-2"},
    ], owner="alice", coverage={"covered": 10, "total": 10})
    assert result["formula_version"] == FORMULA_VERSION
    assert result["state"] == "scored"
    assert result["families"]["prompt_maturity"]["state"] == "critical"
    evidence = result["families"]["prompt_maturity"]["evidence"][0]
    assert evidence["id"] == "ordinal-1" and evidence["ordinal"] == 1
    assert evidence["source_state"] == "unavailable"
    assert evidence["session_handle"] is None and evidence["source_ref"] is None
    assert "secret" not in evidence and "owner" not in evidence
    assert "secret" not in str(result)


def test_quality_low_coverage_is_unscored_and_unknown_is_not_healthy():
    result = project_quality([{"owner": "alice", "context_pressure": True}], owner="alice", coverage={"covered": 1, "total": 10})
    assert result["state"] == "unscored"
    assert result["score"] is None and result["grade"] is None
    assert result["families"]["context_health"]["state"] == "unavailable"


def test_quality_validates_owner_coverage_and_deadline():
    with pytest.raises(QualityError):
        project_quality([], owner="", coverage={"covered": 0, "total": 0})
    with pytest.raises(QualityError):
        project_quality([], owner="a", coverage={"covered": 2, "total": 1})
    with pytest.raises(QualityError, match="deadline_exceeded"):
        project_quality([], owner="a", deadline=time.monotonic() - 1)


def test_trends_buckets_boundaries_overlap_and_owner_isolation():
    rows = [
        {"owner_id": "alice", "text": "seam seamless seam", "event_time": "2026-09-01T12:00:00Z"},
        {"owner_id": "alice", "text": "load-bearing", "event_time": "2026-09-08T12:00:00Z"},
    ]
    result = project_trends(rows, owner="alice", content_opt_in=True,
                            terms="seam | seam\nload-bearing", resolution="week")
    assert result["groups"][0]["occurrences"] == 2
    assert result["groups"][0]["points"][0]["bucket"] == "2026-08-31"
    assert result["groups"][1]["points"][-1]["bucket"] == "2026-09-07"
    with pytest.raises(TrendError, match="owner"):
        project_trends(rows + [{"owner_id": "bob", "text": "seam"}], owner="alice", content_opt_in=True)


def test_trends_stream_consumption_stops_at_bound_and_capability_gates():
    def endless():
        index = 0
        while True:
            yield {"owner": "alice", "text": "x", "event_time": "2026-01-01T00:00:00Z"}
            index += 1
    with pytest.raises(TrendError, match="exceeds bound"):
        project_trends(endless(), owner="alice", content_opt_in=True, terms="x")


def test_quality_owner_coverage_drivers_sparkline_and_optional_gates():
    rows = [{"owner": "alice", "outcome": "failure", "tool_failure": True,
             "prompt_unverified": False, "context_pressure": False, "abandoned": False,
             "evidence_id": f"e-{i}"} for i in range(20)]
    result = project_quality(rows, owner="alice", coverage={"covered": 20, "total": 20})
    assert result["state"] == "scored"
    assert len(result["families"]["tool_reliability"]["sparkline"]) <= 16
    assert result["outcomes"] == {"success": 0, "failure": 20, "unknown": 0}
    assert result["families"]["tool_reliability"]["state"] == "critical"
    with pytest.raises(QualityError, match="owner"):
        project_quality(rows + [{"owner": "bob"}], owner="alice")
    assert capability("vcs")["state"] == "unavailable"
    assert capability("insight")["reason"] == "explicit_gate_required"
