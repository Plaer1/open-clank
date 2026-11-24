import asyncio

from src.openclank.acp_bridge import PermissionHandler
from src.openclank.computer_control import protocol as P

TITLE = "lifetools_abc123_control_desktop"
ARGS = {"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 10, "y": 20, "frame": {"width": 100, "height": 50}}


def _handler(answer, ctx=None, prompts=None):
    h = PermissionHandler()
    context = {"owner": "alice", "is_admin": True, "odysseus_session_id": "chat1", "workspace": "/tmp"}
    context.update(ctx or {})
    h.set_context_resolver(lambda sid: context)

    async def on_request(req):
        if prompts is not None:
            prompts.append(req)
        req.resolve(answer)

    h.on_request(on_request)
    return h


ON = {"desktop_control_enabled": True, "desktop_accessibility_enabled": True}


def _ask(h, title=TITLE, args=ARGS, session="eng1", settings=None, precheck=False):
    """precheck=False stubs the Host lease/observation check (these tests are about the approval record); precheck=True uses the real one."""
    import src.openclank.transcript_projection as tp
    import src.settings as st
    from src.openclank.computer_control import hoststate
    real, real_get = tp.get_projection, st.get_user_setting
    real_pre = hoststate.ControlState.precheck
    if not precheck:
        hoststate.ControlState.precheck = lambda self, *a, **k: None
    st.get_user_setting = lambda key, owner, default=None: (ON if settings is None else settings).get(key, default)
    tp.get_projection = lambda *a, **k: {"mimo_session_id": session, "active_turn_id": "t1", "transcript_revision": 1}
    try:
        return asyncio.run(h.handle({"sessionId": session, "toolCall": {"title": title, "rawInput": {"args": args}}}))
    finally:
        tp.get_projection, st.get_user_setting = real, real_get
        hoststate.ControlState.precheck = real_pre


def _fresh():
    P.REGISTRY.__init__()


def _selected(r):
    return r["outcome"]["optionId"]


def test_human_allow_records_one_consumable_approval():
    _fresh()
    prompts = []
    assert _selected(_ask(_handler("once", prompts=prompts))) == "once"
    d = P.args_digest(ARGS)
    assert prompts[0].options == ["once", "reject"]
    assert prompts[0].raw_input["summary"] == "Click left at (10, 20) in a 100x50 frame"
    assert P.REGISTRY.consume("alice", "eng1", d) is True
    assert P.REGISTRY.consume("alice", "eng1", d) is False


def test_prompt_shows_exact_text_and_chord():
    _fresh()
    prompts = []
    _ask(_handler("once", prompts=prompts), args={"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "hello\nworld"})
    _ask(_handler("once", prompts=prompts), args={"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Control", "s"]})
    assert prompts[0].raw_input["summary"] == "Type: hello\nworld"
    assert prompts[1].raw_input["summary"] == "Press Control+s"


def test_expiry_digest_and_identity_mismatch():
    _fresh()
    d = P.args_digest(ARGS)
    P.REGISTRY.grant("alice", "eng1", d, now=100.0)
    assert P.REGISTRY.consume("alice", "eng1", d, now=131.0) is False  # expired (30 s)
    P.REGISTRY.grant("alice", "eng1", d, now=100.0)
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest({**ARGS, "x": 11}), now=101.0) is False  # one pixel
    t = {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "hello"}
    P.REGISTRY.grant("alice", "eng1", P.args_digest(t), now=100.0)
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest({**t, "text": "hellp"}), now=101.0) is False  # one character
    assert P.REGISTRY.consume("alice", "eng2", d, now=101.0) is False  # other session
    assert P.REGISTRY.consume("bob", "eng1", d, now=101.0) is False  # other owner
    assert P.REGISTRY.consume("alice", "eng1", d, now=101.0) is True


def test_reject_always_and_noninteractive_make_no_record():
    _fresh()
    d = P.args_digest(ARGS)
    for answer in ("reject", "always", "chat", "workspace", "bogus"):
        assert _selected(_ask(_handler(answer))) == "reject", answer
    assert _selected(_ask(_handler("once", ctx={"interaction_policy": "fail_on_interaction"}))) == "reject"
    assert _selected(_ask(_handler("once", ctx={"is_admin": False}))) == "reject"
    assert _selected(_ask(_handler("once", ctx={"incognito": True}))) == "reject"
    assert _selected(_ask(_handler("once", ctx={"owner": ""}))) == "reject"
    assert P.REGISTRY.consume("alice", "eng1", d) is False


def test_malformed_or_refused_args_are_rejected_without_prompt():
    _fresh()
    prompts = []
    for args in ({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Control", "Alt", "t"]}, {"action": "drag"}, None, {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": ""}):
        assert _selected(_ask(_handler("once", prompts=prompts), args=args)) == "reject"
    assert prompts == []


def test_stored_durable_grant_and_auto_mode_are_ignored():
    _fresh()
    import src.openclank.operation_approvals as oa
    import src.permission_mode as pm
    calls = []
    real_match, real_auto = oa.match_operation_approval, pm.auto_approves
    oa.match_operation_approval = lambda **k: calls.append(k) or True
    pm.auto_approves = lambda owner: True
    try:
        prompts = []
        assert _selected(_ask(_handler("reject", prompts=prompts))) == "reject"
        assert len(prompts) == 1 and calls == []  # the human was asked; the stored grant was never consulted
    finally:
        oa.match_operation_approval, pm.auto_approves = real_match, real_auto


def test_pref_off_is_not_approved_and_keeps_the_record():
    _fresh()
    d = P.args_digest(ARGS)
    P.REGISTRY.grant("alice", "eng1", d)
    assert P.REGISTRY.consume("alice", "eng1", d, enabled=False) is False
    assert P.REGISTRY.consume("alice", "eng1", d, enabled=True) is True


def test_cap_of_eight_per_session_drops_oldest():
    _fresh()
    ds = [P.args_digest({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": str(i)}) for i in range(10)]
    for i, d in enumerate(ds):
        P.REGISTRY.grant("alice", "eng1", d, now=float(i))
    assert P.REGISTRY.consume("alice", "eng1", ds[0], now=10.0) is False
    assert P.REGISTRY.consume("alice", "eng1", ds[1], now=10.0) is False
    assert P.REGISTRY.consume("alice", "eng1", ds[2], now=10.0) is True
    assert P.REGISTRY.consume("alice", "eng1", ds[9], now=10.0) is True


def test_other_tool_titles_keep_existing_behaviour():
    _fresh()
    prompts = []
    h = _handler("always", prompts=prompts)
    r = _ask(h, title="lifetools_abc123_control_desktop_status")
    assert prompts and prompts[0].options != ["once", "reject"]
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(ARGS)) is False


INSPECT = "lifetools_abc123_inspect_desktop"
STATE = {"action": "app_state", "app": {"name": "Demo"}}
SNAP = "a1b2c3d4e5f60718"
EL = {"action": "click_element", "snapshot_id": SNAP, "element": "0", "target": {"app": "Demo", "window": "Main", "role": "push button", "name": "Send"}}


def test_inspect_desktop_is_forced_ask_once_or_reject_with_the_exact_read():
    _fresh()
    prompts = []
    assert _selected(_ask(_handler("once", prompts=prompts), title=INSPECT, args=STATE)) == "once"
    assert prompts[0].options == ["once", "reject"] and "Read the open window of Demo" in prompts[0].raw_input["summary"]
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(STATE)) is True
    for answer in ("always", "chat", "workspace"):
        assert _selected(_ask(_handler(answer), title=INSPECT, args=STATE)) == "reject", answer
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(STATE)) is False


def test_element_action_approval_is_bound_to_snapshot_and_element():
    _fresh()
    prompts = []
    _ask(_handler("once", prompts=prompts), args=EL)
    assert prompts[0].raw_input["summary"] == "Click push button 'Send' in Demo, window 'Main'"
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest({**EL, "element": "1"})) is False       # another element
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest({**EL, "snapshot_id": "b" * 16})) is False  # another snapshot
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(EL)) is True
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(EL)) is False                            # replay


def test_handshake_needs_no_card_but_other_reads_ask():
    _fresh()
    prompts = []
    assert _selected(_ask(_handler("reject", prompts=prompts), title=INSPECT, args={"action": "handshake"})) == "once" and prompts == []
    assert _selected(_ask(_handler("reject", prompts=prompts), title=INSPECT, args={"action": "list_apps"})) == "reject" and len(prompts) == 1


def test_settings_off_means_no_card_and_no_record():
    _fresh()
    prompts = []
    off = {"desktop_control_enabled": True, "desktop_accessibility_enabled": False}
    assert _selected(_ask(_handler("once", prompts=prompts), title=INSPECT, args=STATE, settings=off)) == "reject"
    assert _selected(_ask(_handler("once", prompts=prompts), args=EL, settings=off)) == "reject"            # element action needs both
    assert _selected(_ask(_handler("once", prompts=prompts), args=ARGS, settings={"desktop_accessibility_enabled": True})) == "reject"
    assert prompts == [] and P.REGISTRY.consume("alice", "eng1", P.args_digest(STATE)) is False


def test_control_works_without_the_capture_setting():
    _fresh()
    only_control = {"desktop_control_enabled": True}          # desktop_capture_enabled is absent (Off)
    assert _selected(_ask(_handler("once"), args=ARGS, settings=only_control)) == "once"
    assert P.REGISTRY.consume("alice", "eng1", P.args_digest(ARGS)) is True
