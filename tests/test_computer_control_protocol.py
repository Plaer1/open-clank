import hashlib
import json

from src.openclank.computer_control import protocol as P


def refused(fn, *a, code=None):
    try:
        fn(*a)
    except P.ProtocolError as exc:
        assert code is None or exc.code == code, exc.code
        return
    raise AssertionError("expected refusal")


def test_request_lines_match_helper_contract_byte_for_byte():
    assert P.build_begin(120000) == b'{"v":1,"op":"begin","deadline_ms":120000}\n'
    assert P.build_click(10, 20, "left", {"width": 1920, "height": 1080}) == \
        b'{"v":1,"op":"click","x":10,"y":20,"button":"left","frame":{"width":1920,"height":1080}}\n'
    assert P.build_key(["Control", "s"]) == b'{"v":1,"op":"key","keysyms":[65507,115]}\n'
    assert P.build_type("a\tb\n") == b'{"v":1,"op":"type","text":"a\\tb\\n"}\n'
    assert P.build_stop() == b'{"v":1,"op":"stop"}\n'
    refused(P.build_begin, 0)
    refused(P.build_begin, 120001)
    refused(P.build_click, True, 1, "left", {"width": 1, "height": 1})
    refused(P.build_click, 1, 1, "double", {"width": 1, "height": 1})
    refused(P.build_click, 1, 1, "left", {"width": 1, "height": 1, "x": 2})


def test_key_allowlist_table():
    ok = {("a",): [97], ("~",): [126], (" ",): [32], ("Return",): [0xff0d], ("F12",): [0xffc9], ("F1",): [0xffbe],
          ("Page_Down",): [0xff56], ("Control", "s"): [0xffe3, 115], ("Shift", "Tab"): [0xffe1, 0xff09],
          ("Control", "Shift", "Left"): [0xffe3, 0xffe1, 0xff51], ("Alt", "Left"): [0xffe9, 0xff51]}
    for names, syms in ok.items():
        assert P.key_chord(list(names)) == syms, names
    for names in [["Super", "a"], ["Meta", "a"], ["Control", "Alt", "Delete"], ["Control", "Alt", "t"], ["Alt", "F4"],
                  ["Alt", "Tab"], ["Shift", "Alt", "Tab"], ["Control"], [], ["Control", "Control", "a"], ["ctrl", "a"],
                  ["a", "b"], ["Escape", "Return"], ["F13"], ["é"], ["\x01"], ["Super"], ["Control", "Shift", "Alt", "x"],
                  "Control+s", ["Control", 5]]:
        refused(P.key_chord, names)
    refused(P.key_chord, ["Control", "Alt", "t"], code="refused_chord")


def test_text_rules():
    assert P.check_text("hello\n\tworld") == "hello\n\tworld"
    assert P.check_text("é€") == "é€"
    assert P.check_text("x" * 256)
    for bad in ["", "x" * 257, "a\x1bb", "a\x00", "a\x7f", 5, None, "a\r"]:
        refused(P.check_text, bad, code="invalid_text")


def test_frame_and_bounds_refusal():
    s = (1920, 1080)
    P.check_click(0, 0, {"width": 1920, "height": 1080}, s)
    P.check_click(1919, 1079, {"width": 1920, "height": 1080}, s)
    refused(P.check_click, 1920, 5, {"width": 1920, "height": 1080}, s, code="out_of_bounds")
    refused(P.check_click, -1, 5, {"width": 1920, "height": 1080}, s, code="out_of_bounds")
    refused(P.check_click, 5, 5, {"width": 1280, "height": 720}, s, code="frame_mismatch")
    refused(P.check_click, 5, 5, {"width": 1920, "height": 1080}, None, code="frame_mismatch")


def test_parse_call_is_strict():
    assert P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 1, "y": 2, "frame": {"width": 3, "height": 4}})["button"] == "left"
    assert P.parse_call({"action": "begin"}) == {"action": "begin"}
    for bad in [{"action": "begin", "x": 1}, {"action": "drag"}, {"action": "key"}, {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "a", "keys": []},
                {"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 1, "y": 2}, [], None, {"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 1.5, "y": 2, "frame": {"width": 3, "height": 4}}]:
        refused(P.parse_call, bad)


def test_reply_parsing_never_treats_garbage_as_success():
    good = P.parse_reply(b'{"ok":true,"state":"active","stream":{"width":1920,"height":1080}}\n', "begin")
    assert good == {"ok": True, "stream": (1920, 1080)}
    assert P.parse_reply(b'{"ok":true,"outcome":"sent","actions_left":19}\n', "action") == {"ok": True, "outcome": "sent", "actions_left": 19}
    assert P.parse_reply(b'{"ok":true,"state":"stopped"}\n', "stop") == {"ok": True}
    assert P.parse_reply(b'{"ok":false,"code":"outcome_unknown"}\n', "action") == {"ok": False, "code": "outcome_unknown"}
    bad = {"ok": False, "code": "invalid_reply"}
    for line, expect in [(b'{"ok":false,"code":"brand_new_code"}\n', "action"), (b'not json\n', "action"), (b'{"ok":true,"outcome":"sent","actions_left":1}', "action"),
                         (b'{"ok":"yes"}\n', "action"), (b'[]\n', "action"), (b'{"ok":true,"outcome":"done","actions_left":1}\n', "action"),
                         (b'{"ok":true,"outcome":"sent","actions_left":1}\n', "begin"), (b'{"ok":true,"state":"active","stream":{"width":0,"height":5}}\n', "begin"),
                         (b'{"ok":true,"outcome":"sent","actions_left":1,"extra":1}\n', "action"), (b'\xff\n', "action"), (b'', "action"),
                         (b'{"ok":true,"state":"active"}\n', "stop")]:
        assert P.parse_reply(line, expect) == bad, line
    assert P.keeps_session("frame_mismatch") and not P.keeps_session("outcome_unknown") and not P.keeps_session("expired")


def test_receipt_never_contains_typed_text():
    secret = "correct horse battery staple"
    call = P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": secret})
    r = P.receipt(call, "sent", True, now=5)
    assert r == {"observation": "aaaaaaaaaaaaaaaa", "action": "type", "outcome": "sent", "human_approved": True, "timestamp": 5, "chars": len(secret),
                 "text_sha256": hashlib.sha256(secret.encode()).hexdigest()}
    assert secret not in json.dumps(r)
    c = P.receipt(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 1, "y": 2, "button": "right", "frame": {"width": 3, "height": 4}}), "sent", False, now=1)
    assert c["x"] == 1 and c["button"] == "right" and c["frame"] == {"width": 3, "height": 4} and c["human_approved"] is False
    assert P.receipt(None, "invalid_key", False)["action"] == "invalid"


def test_approval_summary_shows_exact_action():
    assert P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "type", "text": "hi there"})) == "Type: hi there"
    assert P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "key", "keys": ["Control", "s"]})) == "Press Control+s"
    assert P.approval_summary(P.parse_call({"observation": "aaaaaaaaaaaaaaaa", "action": "click", "x": 5, "y": 6, "frame": {"width": 7, "height": 8}})) == "Click left at (5, 6) in a 7x8 frame"
    assert "share the screen" in P.approval_summary({"action": "begin"})


def test_args_digest_is_canonical():
    assert P.args_digest({"b": 1, "a": "é"}) == P.args_digest({"a": "é", "b": 1})
    assert P.args_digest({"a": 1}) != P.args_digest({"a": 2})


def test_portal_shortcut_description_is_optional_and_bounded():
    base = {"ok": True, "state": "active", "stream": {"width": 100, "height": 50}}
    base["takeover_shortcut"] = "Ctrl+Alt+Escape"
    base["cursor_overlay_at_begin"] = True
    reply = P.parse_reply((json.dumps(base) + "\n").encode(), "begin")
    assert reply["takeover_shortcut"] == "Ctrl+Alt+Escape" and reply["cursor_overlay_at_begin"] is True
    base["takeover_shortcut"] = "x" * 129
    assert P.parse_reply((json.dumps(base) + "\n").encode(), "begin") == {"ok": False, "code": "invalid_reply"}
