import asyncio, json, sys
sys.path.insert(0, ".")
from dataclasses import asdict
from src.memory_provider import FoundObservation, RecallInvocation, MemoryRevisionRef
from src.memery_external_sources import SourceRef
from src.memery_provider import MemeryProvider as P, _HostObservationProjection, MemoryRequestRejectedError
from src.openclank.memory_context_admission import (found_features_for, found_features_rows, found_features_valid,
                                                    found_request_body, s07_features)
from src.memery_ranking_feedback import shadow_report

FEAT = {"v": 1, "fts_ordinal": 2, "route_kind": "curated_fts", "directness": None, "entity_match": None, "freshness": None}
READY = {"version": 1, "feature": "found_features_v1", "state": "ready"}
# Body of the pre-change code path (asdict of the old 6-field dataclass), captured from the ORIGINAL file before editing.
OLD_BODY = ('{"owner":"alice","workspace_id":"ws","source":{"version":1,"kind":"chat","account_id":"acct","chat_id":"chat1","actor_id":"main","message_id":"msg1","part_id":"part1","revision":1,"content_hash":"hhhhhhhh","workspace_id":"ws","project_id":null},'
            '"invocation":{"session_id":"s","root_operation_id":"msg1","actor_id":"main","execution_revision":3,"inner_turn_id":"t","recall_attempt_id":"att1"},'
            '"memory":{"owner":"alice","workspace_id":"ws","project_id":null,"block_id":"blk","revision":2,"value_sha256":"' + "a" * 64 + '","version":1,"provider":"fm-v2"},"version":1}')


def obs(**kw):
    src = SourceRef(1, "chat", "acct", "chat1", "main", "msg1", "part1", 1, "h" * 8, "ws", None)
    return FoundObservation("alice", "ws", src, RecallInvocation("s", "msg1", "main", 3, "t", "att1"),
                            MemoryRevisionRef("alice", "ws", None, "blk", 2, "a" * 64), **kw)


def dump(body): return json.dumps(body, separators=(",", ":"))


class Proc: pass
class Transport: process = Proc()
class Prep:
    def assert_valid(self): pass


class Fake:
    _host_found_features_ready = P._host_found_features_ready
    _host_found_features_page = P._host_found_features_page
    _host_negotiate_found_features = P._host_negotiate_found_features

    def __init__(self, reply=READY, exc=False):
        self._owned_transport = Transport(); self._owned_generation = object(); self._owner_task = object()
        self._guarded_preparation = Prep(); self.reply = reply; self.exc = exc; self.calls = []

    def _host_bounded_write_available(self): return True

    async def _s07_owner_rpc(self, owner, workspace, payload, deadline, **kw):
        self.calls.append((owner, workspace, payload))
        if self.exc: raise RuntimeError("boom")
        return self.reply


def negotiate(f): return asyncio.run(f._host_negotiate_found_features(owner="alice", workspace_id="ws"))


def test_off_path_body_bytes_identical():
    assert dump(found_request_body(obs())) == OLD_BODY


def test_no_features_key_when_none():
    body = found_request_body(obs(features=None))
    assert "features" not in body and "features" in asdict(obs())  # the asdict trap is real


def test_features_present_exact_dict():
    body = found_request_body(obs(features=dict(FEAT)))
    assert body["features"] == FEAT and list(body)[-1] == "features"


def test_negotiation_ok_and_cached():
    f = Fake()
    assert negotiate(f) is True and negotiate(f) is True
    assert len(f.calls) == 1 and f.calls[0][2] == {"phase": "found_features_capability"}


def test_negotiation_bad_reply_never_raises():
    for reply in (None, {}, {**READY, "state": "no"}, {**READY, "extra": 1}, "ready"):
        f = Fake(reply); assert negotiate(f) is False and not f._host_found_features_ready()
    f = Fake(exc=True); assert negotiate(f) is False


def test_new_generation_renegotiates():
    f = Fake(); negotiate(f); f._owned_generation = object()
    assert not f._host_found_features_ready()
    assert negotiate(f) is True and len(f.calls) == 2
    f._owned_transport = Transport(); assert not f._host_found_features_ready()


def test_decision_once_per_attempt():
    # the callback decides once before its loop; flipping the preference between writes cannot change later sends
    prefs = {"on": True}
    feats = s07_features({"c1": 0, "c2": 1}, (("c1", 1), ("c2", 2)))
    send = prefs["on"] is True and negotiate(Fake())
    bodies = []
    for cid in ("c1", "c2"):
        bodies.append(found_request_body(obs(features=found_features_for(send, feats, cid))))
        prefs["on"] = False
    assert all("features" in b for b in bodies)
    assert found_features_for(False, feats, "c1") is None and found_features_for(True, feats, "zz") is None
    assert found_features_for(True, None, "c1") is None


def test_validator_closed():
    assert found_features_valid(FEAT)
    for bad in ({**FEAT, "x": 1}, {**FEAT, "v": True}, {**FEAT, "fts_ordinal": 20}, {**FEAT, "route_kind": "A"},
                {**FEAT, "directness": 2}, {**FEAT, "entity_match": 1}, None):
        assert not found_features_valid(bad)


ADM = "cs_s06context_" + "1" * 64
FND = "cs_s06found_" + "2" * 64


def item(**kw):
    d = {"admitted_event_id": ADM, "found_event_id": FND, "recall_attempt_id": "att1", "features": dict(FEAT),
         "recorded": {"positive": True, "negative": False}}
    d.update(kw); return d


def origin():
    src = SourceRef(1, "chat", "acct", "chat1", "main", "msg1", "part1", 1, "h" * 8, "ws", None)
    return _HostObservationProjection("alice", "ws", "o1", src, "alice", "oh", "cf")


def page(reply, limit=64):
    f = Fake(); negotiate(f); f.reply = reply
    out = asyncio.run(f._host_found_features_page(origin(), limit=limit))
    return f, out


def page_refused(reply, limit=64):
    try: page(reply, limit)
    except MemoryRequestRejectedError: return True
    return False


def good(items=None, **kw):
    return {"version": 1, "state": "ready", "items": [item()] if items is None else items, "next_after": None, **kw}


def test_page_valid():
    f, out = page(good())
    call = f.calls[-1]
    assert call[0] == "alice" and call[2]["phase"] == "found_features_page" and call[2]["request"]["owner"] == "alice"
    assert out["items"][0]["found_event_id"] == FND


def test_page_refusals():
    assert page_refused(good(extra=1))
    assert page_refused(good([item(owner="bob")]))                       # foreign owner / extra key
    assert page_refused(good([item(), item(admitted_event_id="cs_s06context_" + "3" * 64)]), limit=1)  # over-long
    assert page_refused(good(["x"]))
    assert page_refused(good([item(display="saved text")]))              # text-like extra field
    assert page_refused(good([item(features={**FEAT, "text": "t"})]))
    assert page_refused(good([item(recorded={"positive": 1, "negative": False})]))
    assert page_refused(good([item(), item()]))                           # duplicate id
    assert page_refused(good(next_after="x"))                              # cursor with state ready
    assert page_refused(None)
    try:  # no witness -> refused
        asyncio.run(Fake()._host_found_features_page(origin())); assert False
    except MemoryRequestRejectedError: pass


def test_page_feeds_shadow_report():
    items = []
    for i in range(3):
        items.append(item(admitted_event_id="cs_s06context_" + str(i) * 64, found_event_id="cs_s06found_" + str(i) * 64,
                          recall_attempt_id=f"a{i}", features={**FEAT, "fts_ordinal": i},
                          recorded={"positive": i != 1, "negative": i == 1}))
    _, out = page(good(items))
    receipts, labels = found_features_rows("alice", out["items"])
    report = shadow_report("alice", receipts, labels)
    assert type(report) is dict and report["receipts"] == 3 and report["labelled"] == 3


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print("ok", name)
    print(n, "tests ok")
