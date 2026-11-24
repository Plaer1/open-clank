"""Pure protocol for the native control helper (`--control-v1`, JSON Lines) and the approval registry.

Nothing here starts a process or touches the desktop. Typed text never appears in a
receipt, a log line or an exception message; it is only returned by `approval_summary`.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Mapping, Optional

STREAM_MAX = 1 << 20
TEXT_MAX = 256
BUTTONS = ("left", "right", "middle")
INPUT_ACTIONS = ("begin", "click", "move", "key", "type", "hotkey", "scroll", "drag", "paste")  # pixel/portal path (control helper)
PIXEL_ACTIONS = tuple(a for a in INPUT_ACTIONS if a != "begin")     # each names the observation (capture) it was planned from
ELEMENT_ACTIONS = ("click_element", "secondary", "set_value", "type_element", "select_text")      # accessibility path (a11y helper)
FILE_ACTIONS = ("open_resource", "hand_over_file")                  # chosen Files resources only, never a raw path
ACTIONS = INPUT_ACTIONS + ELEMENT_ACTIONS + FILE_ACTIONS
INSPECT_ACTIONS = ("handshake", "list_apps", "list_windows", "app_state")
DIRECTIONS = ("up", "down", "left", "right")
SCROLL_MAX = 20
VALUE_MAX = 4096
_SNAP = re.compile(r"[0-9a-f]{16}")
_OBS = re.compile(r"[0-9a-f]{16}")
_REF = re.compile(r"[A-Za-z0-9_\-:=~.]{8,1024}")      # an opaque Files resource token; a path never matches (no slash)
_APP_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")
_ELEMENT = re.compile(r"root|[0-9]{1,6}(\.[0-9]{1,6}){0,63}")
BEGIN_DEADLINE_MS = 120000
APPROVAL_TTL_S = 30.0
APPROVAL_PER_SESSION = 8

MODIFIERS = {"Control": 0xffe3, "Shift": 0xffe1, "Alt": 0xffe9}
SPECIAL = {"Return": 0xff0d, "Tab": 0xff09, "Escape": 0xff1b, "BackSpace": 0xff08, "Delete": 0xffff,
           "Left": 0xff51, "Up": 0xff52, "Right": 0xff53, "Down": 0xff54, "Home": 0xff50, "End": 0xff57,
           "Page_Up": 0xff55, "Page_Down": 0xff56, **{f"F{i}": 0xffbd + i for i in range(1, 13)}}

# Closed set of helper error codes; anything else is an invalid reply, never a success.
_REFUSALS = {"frame_mismatch", "out_of_bounds", "invalid_text", "invalid_request", "unsupported_clipboard", "unsupported_action"}  # session continues
_ERRORS = _REFUSALS | {"not_begun", "no_wayland_session", "denied", "unavailable", "timeout",
                       "unsupported_target", "outcome_unknown", "expired", "taken_over", "clipboard_not_restored", "clipboard_changed"}
# Closed set of accessibility-helper codes (anything else is an invalid reply).
A11Y_ERRORS = {"invalid_request", "no_such_app", "app_excluded", "no_such_window", "snapshot_expired", "stale_element", "window_changed",
               "target_gone", "not_actionable", "unsupported_action", "no_such_element", "accessibility_disabled", "bus_unavailable", "timeout", "action_failed"}


class ProtocolError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _line(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _int(v: Any) -> bool:
    return type(v) is int


def build_begin(deadline_ms: int = BEGIN_DEADLINE_MS, stream_handoff: Optional[dict] = None, clipboard: Optional[str] = None) -> bytes:
    if not _int(deadline_ms) or not 1 <= deadline_ms <= 120000:
        raise ProtocolError("invalid_request")
    if stream_handoff is not None and (type(stream_handoff) is not dict or set(stream_handoff) != {"path", "secret", "host_pid", "host_start"}): raise ProtocolError("invalid_request")
    if clipboard not in (None, "replace"): raise ProtocolError("invalid_request")
    return _line({"v": 1, "op": "begin", "deadline_ms": deadline_ms, **({"stream_handoff": stream_handoff} if stream_handoff is not None else {}), **({"clipboard": clipboard} if clipboard else {})})


def build_click(x: Any, y: Any, button: Any, frame: Any) -> bytes:
    if not (_int(x) and _int(y)) or button not in BUTTONS:
        raise ProtocolError("invalid_request")
    w, h = _frame(frame)
    return _line({"v": 1, "op": "click", "x": x, "y": y, "button": button, "frame": {"width": w, "height": h}})


def build_key(names: Any) -> bytes:
    return _line({"v": 1, "op": "key", "keysyms": key_chord(names)})


def build_type(text: Any) -> bytes:
    return _line({"v": 1, "op": "type", "text": check_text(text)})


def build_stop() -> bytes:
    return _line({"v": 1, "op": "stop"})


def build_move(x: Any, y: Any, frame: Any) -> bytes:
    if not (_int(x) and _int(y)):
        raise ProtocolError("invalid_request")
    w, h = _frame(frame)
    return _line({"v": 1, "op": "move", "x": x, "y": y, "frame": {"width": w, "height": h}})


def build_frame_handoff(handoff: Any) -> bytes:
    if (type(handoff) is not dict or set(handoff)!={"path","secret","host_pid","host_start"}
        or type(handoff["path"]) is not str or not handoff["path"].startswith("/") or len(handoff["path"])>100
        or type(handoff["secret"]) is not str or re.fullmatch(r"[0-9a-f]{64}",handoff["secret"]) is None
        or not _int(handoff["host_pid"]) or handoff["host_pid"]<2 or not _int(handoff["host_start"]) or handoff["host_start"]<=0):
        raise ProtocolError("invalid_request")
    return _line({"v":1,"op":"framehandoff","handoff":handoff})


def build_probe() -> bytes:
    return _line({"v": 1, "op": "probe"})


def build_scroll(x: Any, y: Any, direction: Any, amount: Any, frame: Any) -> bytes:
    if not (_int(x) and _int(y) and _int(amount)) or direction not in DIRECTIONS or not 1 <= amount <= SCROLL_MAX:
        raise ProtocolError("invalid_request")
    w, h = _frame(frame)
    return _line({"v": 1, "op": "scroll", "x": x, "y": y, "direction": direction, "amount": amount, "frame": {"width": w, "height": h}})


def build_drag(start: Any, end: Any, frame: Any) -> bytes:
    pts = []
    for pt in (start, end):
        if type(pt) is not dict or set(pt) != {"x", "y"} or not (_int(pt["x"]) and _int(pt["y"])):
            raise ProtocolError("invalid_request")
        pts.append(pt)
    w, h = _frame(frame)
    return _line({"v": 1, "op": "drag", "x1": pts[0]["x"], "y1": pts[0]["y"], "x2": pts[1]["x"], "y2": pts[1]["y"], "frame": {"width": w, "height": h}})


def build_paste(text: Any, clipboard: Any = None) -> bytes:
    if clipboard != "replace": raise ProtocolError("unsupported_clipboard")
    return _line({"v": 1, "op": "paste", "text": check_text(text), "clipboard": "replace"})


def _frame(frame: Any) -> tuple[int, int]:
    if (type(frame) is not dict or set(frame) != {"width", "height"}
            or not (_int(frame["width"]) and _int(frame["height"]))
            or not (1 <= frame["width"] <= STREAM_MAX and 1 <= frame["height"] <= STREAM_MAX)):
        raise ProtocolError("invalid_request")
    return frame["width"], frame["height"]


def check_click(x: int, y: int, frame: Any, stream: Optional[tuple[int, int]]) -> None:
    """Host-side refusal before the helper (which checks again)."""
    if stream is None or _frame(frame) != tuple(stream):
        raise ProtocolError("frame_mismatch")
    if not (0 <= x < stream[0] and 0 <= y < stream[1]):
        raise ProtocolError("out_of_bounds")


def check_text(text: Any) -> str:
    if (type(text) is not str or not 1 <= len(text) <= TEXT_MAX
            or any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 or 0xd800 <= ord(c) <= 0xdfff for c in text)):
        raise ProtocolError("invalid_text")
    return text


def key_chord(names: Any) -> list[int]:
    """Key NAMES to keysyms: 0-3 distinct modifiers plus exactly one other key."""
    if type(names) is not list or not 1 <= len(names) <= 4 or any(type(n) is not str for n in names):
        raise ProtocolError("invalid_key")
    mods = [n for n in names if n in MODIFIERS]
    rest = [n for n in names if n not in MODIFIERS]
    if len(mods) != len(set(mods)) or len(rest) != 1 or len(mods) > 3:
        raise ProtocolError("invalid_key")
    key = rest[0]
    if key in SPECIAL:
        sym = SPECIAL[key]
    elif len(key) == 1 and 0x20 <= ord(key) <= 0x7e:
        sym = ord(key)
    else:
        raise ProtocolError("invalid_key")  # includes Super/Meta and every unknown name
    if "Control" in mods and "Alt" in mods:
        raise ProtocolError("refused_chord")
    if "Alt" in mods and key in ("F4", "Tab"):
        raise ProtocolError("refused_chord")
    # modifiers first (in the order given), then the key
    return [MODIFIERS[m] for m in names if m in MODIFIERS] + [sym]


def _target(t: Any) -> dict:
    """The model's claim about WHAT the element is; shown to the human and checked against the real snapshot before anything is sent."""
    if (type(t) is not dict or set(t) != {"app", "window", "role", "name"}
            or any(type(t[k]) is not str or len(t[k]) > 200 or any(ord(c) < 32 for c in t[k]) for k in t) or not t["app"] or not t["role"]):
        raise ProtocolError("invalid_request")
    return {k: t[k] for k in ("app", "window", "role", "name")}


def _element(args: Mapping[str, Any]) -> dict:
    if type(args.get("snapshot_id")) is not str or not _SNAP.fullmatch(args["snapshot_id"]) or type(args.get("element")) is not str or not _ELEMENT.fullmatch(args["element"]):
        raise ProtocolError("invalid_request")
    return {"snapshot_id": args["snapshot_id"], "element": args["element"], "target": _target(args.get("target"))}


def check_value(text: Any) -> str:
    if type(text) is not str or not 1 <= len(text) <= VALUE_MAX or "\x00" in text or any(0xd800 <= ord(c) <= 0xdfff for c in text):
        raise ProtocolError("invalid_text")
    return text


_PX = {"observation"}
_FIELDS = {"begin": {"action"}, "click": {"action", "x", "y", "frame"} | _PX, "move": {"action", "x", "y", "frame"} | _PX, "key": {"action", "keys"} | _PX,
           "hotkey": {"action", "keys"} | _PX, "type": {"action", "text"} | _PX, "scroll": {"action", "x", "y", "direction", "amount", "frame"} | _PX,
           "drag": {"action", "from", "to", "frame"} | _PX, "paste": {"action", "text"} | _PX,
           "click_element": {"action", "snapshot_id", "element", "target"}, "secondary": {"action", "snapshot_id", "element", "target", "action_name"},
           "set_value": {"action", "snapshot_id", "element", "target", "value"}, "type_element": {"action", "snapshot_id", "element", "target", "text"},
           "select_text": {"action", "snapshot_id", "element", "target", "start", "end"},
           "open_resource": {"action", "resource_ref", "app_id"}, "hand_over_file": {"action", "resource_ref", "direction"}}


def parse_call(args: Any) -> dict:
    """Strict model-argument validation for `control_desktop`; returns a normalized dict."""
    if type(args) is not dict or args.get("action") not in ACTIONS:
        raise ProtocolError("invalid_request")
    act = args["action"]
    req = _FIELDS[act]
    optional = ({"button"} if act == "click" else {"clipboard"} if act in ("begin", "paste") else {"expected_revision", "snapshot_id", "element", "target"} if act == "hand_over_file" else set())
    if req - set(args) or set(args) - req - optional:
        raise ProtocolError("invalid_request")
    if act in ("begin", "paste") and "clipboard" in args and args["clipboard"] != "replace":
        raise ProtocolError("invalid_request")
    if act in PIXEL_ACTIONS and (type(args.get("observation")) is not str or not _OBS.fullmatch(args["observation"])):
        raise ProtocolError("invalid_request")
    if act == "click":
        build_click(args["x"], args["y"], args.get("button", "left"), args["frame"])
        return {"action": act, "x": args["x"], "y": args["y"], "button": args.get("button", "left"), "observation": args["observation"],
                "frame": {"width": args["frame"]["width"], "height": args["frame"]["height"]}}
    if act == "move":
        build_move(args["x"], args["y"], args["frame"])
        return {"action": act, "x": args["x"], "y": args["y"], "observation": args["observation"], "frame": {"width": args["frame"]["width"], "height": args["frame"]["height"]}}
    if act in FILE_ACTIONS:
        if type(args["resource_ref"]) is not str or not _REF.fullmatch(args["resource_ref"]) or ".." in args["resource_ref"]:
            raise ProtocolError("invalid_request")               # a raw path (slashes) or anything that is not an opaque token
        if act == "open_resource":
            if type(args["app_id"]) is not str or not _APP_ID.fullmatch(args["app_id"]):
                raise ProtocolError("invalid_request")
        elif args["direction"] not in ("upload", "download"):
            raise ProtocolError("invalid_request")
        elif act == "hand_over_file":
            extra = {"expected_revision", "snapshot_id", "element", "target"}
            present = extra & set(args)
            if present and present != extra:
                raise ProtocolError("invalid_request")
            if present:
                revision = args["expected_revision"]
                if (args["direction"] != "upload" or type(revision) is not dict
                        or set(revision) != {"kind", "value"} or revision.get("kind") != "hostFingerprint"
                        or type(revision.get("value")) is not str
                        or re.fullmatch(r"sha256:[0-9a-f]{64}:[0-9]{1,8}", revision["value"]) is None):
                    raise ProtocolError("invalid_request")
                return {"action": act, "resource_ref": args["resource_ref"], "direction": "upload",
                        "expected_revision": dict(revision), **_element(args)}
        return dict(args)
    if act in ("key", "hotkey"):
        key_chord(args["keys"])
    elif act in ("type", "paste"):
        check_text(args["text"])
        return {k: args[k] for k in args}
    elif act == "scroll":
        build_scroll(args["x"], args["y"], args["direction"], args["amount"], args["frame"])
    elif act == "drag":
        build_drag(args["from"], args["to"], args["frame"])
        return {"action": act, "from": dict(args["from"]), "to": dict(args["to"]), "observation": args["observation"], "frame": {"width": args["frame"]["width"], "height": args["frame"]["height"]}}
    elif act in ELEMENT_ACTIONS:
        out = {"action": act, **_element(args)}
        if act == "secondary":
            if type(args["action_name"]) is not str or not 1 <= len(args["action_name"]) <= 64:
                raise ProtocolError("invalid_request")
            out["action_name"] = args["action_name"]
        elif act == "set_value":
            out["value"] = check_value(args["value"])
        elif act == "type_element":
            out["text"] = check_value(args["text"])
        elif act == "select_text":
            if not (_int(args["start"]) and _int(args["end"]) and 0 <= args["start"] <= args["end"] <= 1000000):
                raise ProtocolError("invalid_request")
            out.update(start=args["start"], end=args["end"])
        return out
    return dict(args)


def _app_sel(app: Any) -> dict:
    if type(app) is dict and set(app) == {"pid"} and _int(app["pid"]) and 1 <= app["pid"] <= 4194304:
        return {"pid": app["pid"]}
    if type(app) is dict and set(app) == {"name"} and type(app["name"]) is str and 1 <= len(app["name"]) <= 128 and all(ord(c) >= 32 for c in app["name"]):
        return {"name": app["name"]}
    raise ProtocolError("invalid_request")


def parse_inspect(args: Any) -> dict:
    """Strict model-argument validation for `inspect_desktop` (read-only observation of application windows)."""
    if type(args) is not dict or args.get("action") not in INSPECT_ACTIONS:
        raise ProtocolError("invalid_request")
    act = args["action"]
    req = {"handshake": {"action"}, "list_apps": {"action"}, "list_windows": {"action", "app"}, "app_state": {"action", "app"}}[act]
    if req - set(args) or set(args) - req - ({"window_index"} if act == "app_state" else set()):
        raise ProtocolError("invalid_request")
    out: dict[str, Any] = {"action": act}
    if "app" in req:
        out["app"] = _app_sel(args["app"])
    if "window_index" in args:
        if not _int(args["window_index"]) or not 0 <= args["window_index"] <= 63:
            raise ProtocolError("invalid_request")
        out["window_index"] = args["window_index"]
    return out


def a11y_request_line(call: Mapping[str, Any]) -> bytes:
    """One line for the `--a11y-v1` helper from a validated inspect or element call."""
    a = call["action"]
    if a in INSPECT_ACTIONS:
        op = {"handshake": "handshake", "list_apps": "list_apps", "list_windows": "list_windows", "app_state": "app_state"}[a]
        body: dict[str, Any] = {"v": 1, "op": op}
        if "app" in call:
            body["app"] = dict(call["app"])
        if "window_index" in call:
            body["window_index"] = call["window_index"]
        return _line(body)
    kind = {"click_element": "click", "secondary": "secondary", "set_value": "set_value", "type_element": "type", "select_text": "select_text"}[a]
    body = {"v": 1, "op": "act", "snapshot_id": call["snapshot_id"], "element": call["element"], "action": kind}
    if a == "secondary":
        body["name"] = call["action_name"]
    elif a in ("set_value", "type_element"):
        body["text"] = call["value"] if a == "set_value" else call["text"]
    elif a == "select_text":
        body["start"], body["end"] = call["start"], call["end"]
    return _line(body)


A11Y_STREAM_MAX = 4 << 20


def parse_a11y_reply(line: bytes, expect: str) -> dict:
    """expect: handshake | apps | windows | state | act. Garbage is `invalid_reply`, never success."""
    bad = {"ok": False, "code": "invalid_reply"}
    try:
        if not line.endswith(b"\n") or len(line) > A11Y_STREAM_MAX:
            return bad
        r = json.loads(line)
    except (ValueError, UnicodeError):
        return bad
    if type(r) is not dict or type(r.get("ok")) is not bool:
        return bad
    if r["ok"] is False:
        allowed = A11Y_ERRORS | ({"outcome_unknown"} if expect == "chooser" else set())
        return {"ok": False, "code": r["code"]} if set(r) == {"ok", "code"} and r["code"] in allowed else bad
    if expect == "chooser":
        return r if r == {"ok": True, "outcome": "entered"} else bad
    good = ((expect == "handshake" and r.get("accessibility") == "ready" and type(r.get("limits")) is dict)
            or (expect == "apps" and type(r.get("apps")) is list)
            or (expect == "windows" and type(r.get("windows")) is list)
            or (expect == "state" and type(r.get("snapshot_id")) is str and _SNAP.fullmatch(r["snapshot_id"]) and type(r.get("tree")) is dict and type(r.get("target")) is dict)
            or (expect == "act" and r.get("outcome") == "performed" and type(r.get("window_changed")) is bool and r.get("verified", "unknown") in (True, False, "unknown")))
    return r if good else bad


def chooser_request_line(snapshot_id: str, element: str, private_path: str) -> bytes:
    """Private helper transport; never accepts a model-provided path."""
    if (type(snapshot_id) is not str or not _SNAP.fullmatch(snapshot_id)
            or type(element) is not str or not _ELEMENT.fullmatch(element)
            or type(private_path) is not str
            or len(private_path) > 500 or re.fullmatch(r"/(?:[^/\x00-\x1f]+/)*openclank-selected-[A-Za-z0-9_-]+/[^/\x00-\x1f]+", private_path) is None):
        raise ProtocolError("invalid_request")
    return _line({"v": 1, "op": "chooser_enter", "snapshot_id": snapshot_id,
                  "element": element, "path": private_path})


def request_line(call: Mapping[str, Any]) -> bytes:
    a = call["action"]
    if a == "begin":
        return build_begin(clipboard=call.get("clipboard"))
    if a == "click":
        return build_click(call["x"], call["y"], call["button"], call["frame"])
    if a == "move":
        return build_move(call["x"], call["y"], call["frame"])
    if a == "scroll":
        return build_scroll(call["x"], call["y"], call["direction"], call["amount"], call["frame"])
    if a == "drag":
        return build_drag(call["from"], call["to"], call["frame"])
    if a == "paste":
        return build_paste(call["text"], call.get("clipboard"))
    return build_key(call["keys"]) if a in ("key", "hotkey") else build_type(call["text"])


def args_digest(args: Any) -> str:
    """sha256 of canonical JSON of the tool input; shared by the approval record and its consumer."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _what(t: Mapping[str, Any]) -> str:
    name = f" '{t['name']}'" if t.get("name") else ""
    return f"{t['role']}{name} in {t['app']}" + (f", window '{t['window']}'" if t.get("window") else "")


def approval_summary(call: Mapping[str, Any]) -> str:
    """Plain text shown to the human. Typed/set/pasted text and element names appear here, and nowhere else."""
    a = call["action"]
    if a == "begin":
        return "Start desktop control (the system will ask you to share the screen)" + ("; request clipboard replacement access" if call.get("clipboard") == "replace" else "")
    if a == "click":
        f = call["frame"]
        return f"Click {call['button']} at ({call['x']}, {call['y']}) in a {f['width']}x{f['height']} frame"
    if a in ("key", "hotkey"):
        return "Press " + "+".join(call["keys"])
    if a == "scroll":
        f = call["frame"]
        return f"Scroll {call['direction']} {call['amount']} at ({call['x']}, {call['y']}) in a {f['width']}x{f['height']} frame"
    if a == "drag":
        f, s0, s1 = call["frame"], call["from"], call["to"]
        return f"Drag from ({s0['x']}, {s0['y']}) to ({s1['x']}, {s1['y']}) in a {f['width']}x{f['height']} frame"
    if a == "paste":
        return ("Paste; your previous clipboard will be replaced and NOT restored: " if call.get("clipboard") == "replace" else "Paste unavailable without explicit clipboard replacement: ") + call["text"]
    if a == "move":
        f = call["frame"]
        return f"Move the pointer to ({call['x']}, {call['y']}) in a {f['width']}x{f['height']} frame (no click)"
    if a == "select_text":
        return f"Select characters {call['start']} to {call['end']} in " + _what(call["target"])
    if a == "open_resource":
        return f"Open the Files resource you chose ({call['resource_ref'][:12]}…) in the application '{call['app_id']}'"
    if a == "hand_over_file":
        if call.get("direction") == "upload" and "expected_revision" in call:
            return (f"Enter the selected Files resource ({call['resource_ref'][:12]}…) into the observed "
                    f"file chooser field in {call['target']['app']}; entry does not prove selection or opening")
        return f"Hand a file over ({call['direction']}) through a file dialog: not supported yet"
    if a == "click_element":
        return "Click " + _what(call["target"])
    if a == "secondary":
        return f"Run '{call['action_name']}' on " + _what(call["target"])
    if a == "set_value":
        return "Set " + _what(call["target"]) + " to: " + call["value"]
    if a == "type_element":
        return "Type into " + _what(call["target"]) + ": " + call["text"]
    return "Type: " + call["text"]


def inspect_summary(call: Mapping[str, Any]) -> str:
    """Plain text for the per-app read approval: what is read and what is hidden."""
    a = call["action"]
    if a == "handshake":
        return "Check whether reading application windows is available (reads no window content)"
    if a == "list_apps":
        return "List the applications that are open on your desktop (names only)"
    who = call["app"].get("name") or f"process {call['app']['pid']}"
    if a == "list_windows":
        return f"List the windows (titles) of {who}"
    return (f"Read the open window of {who}: element names, roles and values (secret-looking values are hidden, "
            f"but the filter is not a guarantee)")


def valid_control_target(value: Any, stream: Mapping[str, Any]) -> bool:
    if type(value) is not dict or set(value) != {"compositor_pid", "compositor_start", "output_id", "rect"}: return False
    if any(type(value[k]) is not int or value[k] <= 0 for k in ("compositor_pid", "compositor_start", "output_id")): return False
    rect = value["rect"]
    return (type(rect) is dict and set(rect) == {"x", "y", "width", "height"}
            and all(type(rect[k]) is int for k in rect) and -(2**31) <= rect["x"] < 2**31
            and -(2**31) <= rect["y"] < 2**31 and rect["width"] == stream.get("width")
            and rect["height"] == stream.get("height") and 0 < rect["width"] <= 65536 and 0 < rect["height"] <= 65536)


def parse_reply(line: bytes, expect: str) -> dict:
    """expect: begin | action | stop. Returns {"ok":True,...} or {"ok":False,"code":C}; garbage is invalid_reply."""
    bad = {"ok": False, "code": "invalid_reply"}
    try:
        if not line.endswith(b"\n") or len(line) > 4096:
            return bad
        r = json.loads(line)
    except (ValueError, UnicodeError):
        return bad
    if type(r) is not dict or type(r.get("ok")) is not bool:
        return bad
    if expect == "paste" and set(r) == {"ok", "clipboard", "input", "lifetime"}:
        if (r["clipboard"] in ("replaced", "not_issued", "unknown") and r["input"] in ("sent", "not_issued", "unknown")
                and r["lifetime"] in ("session", "platform") and r["ok"] == (r["clipboard"] == "replaced" and r["input"] == "sent")
                and (r["input"] != "sent" or r["clipboard"] == "replaced")
                and (r["clipboard"] != "not_issued" or r["input"] == "not_issued")):
            return r
        return bad
    if r["ok"] is False:
        return {"ok": False, "code": r["code"]} if set(r) == {"ok", "code"} and r["code"] in _ERRORS else bad
    if expect == "begin":
        s = r.get("stream")
        shortcut = r.get("takeover_shortcut")
        if ({"ok", "state", "stream"} <= set(r) <= {"ok", "state", "stream", "takeover_shortcut", "cursor_overlay_at_begin", "control_target", "clipboard_enabled"} and r["state"] == "active" and type(s) is dict and set(s) == {"width", "height"}
                and all(_int(s[k]) and 1 <= s[k] <= STREAM_MAX for k in s)
                and ("takeover_shortcut" not in r or (type(shortcut) is str and 0 < len(shortcut) <= 128))
                and ("cursor_overlay_at_begin" not in r or type(r["cursor_overlay_at_begin"]) is bool)
                and ("control_target" not in r or valid_control_target(r["control_target"], s))
                and ("clipboard_enabled" not in r or type(r["clipboard_enabled"]) is bool)):
            result = {"ok": True, "stream": (s["width"], s["height"])}
            if shortcut is not None: result["takeover_shortcut"] = shortcut
            if "cursor_overlay_at_begin" in r: result["cursor_overlay_at_begin"] = r["cursor_overlay_at_begin"]
            if "control_target" in r: result["control_target"] = r["control_target"]
            if "clipboard_enabled" in r: result["clipboard_enabled"] = r["clipboard_enabled"]
            return result
    elif expect == "action":
        if set(r) == {"ok", "outcome", "actions_left"} and r["outcome"] == "sent" and _int(r["actions_left"]) and r["actions_left"] >= 0:
            return {"ok": True, "outcome": "sent", "actions_left": r["actions_left"]}
    elif expect == "frame_handoff" and set(r) == {"ok", "state"} and r["state"] == "frame_handoff":
        return {"ok": True}
    elif expect == "stop" and set(r) == {"ok", "state"} and r["state"] == "stopped":
        return {"ok": True}
    elif expect == "probe":
        if (set(r) == {"ok", "state", "version", "keyboard", "pointer"} and r["state"] == "probe" and _int(r["version"]) and r["version"] >= 0
                and type(r["keyboard"]) is bool and type(r["pointer"]) is bool):
            return {"ok": True, "version": r["version"], "keyboard": r["keyboard"], "pointer": r["pointer"]}
    return bad


def keeps_session(code: str) -> bool:
    return code in _REFUSALS


def receipt(call: Optional[Mapping[str, Any]], outcome: str, approved: bool, now: Optional[float] = None) -> dict:
    """Small structured receipt. `type` records only the character count and sha256 of the text."""
    out: dict[str, Any] = {"action": (call or {}).get("action", "invalid"), "outcome": outcome,
                           "human_approved": bool(approved), "timestamp": int(time.time() if now is None else now)}
    if call:
        a = call["action"]
        if a in ("begin", "paste") and call.get("clipboard") == "replace": out["clipboard_mode"] = "replace"
        if a == "click":
            out.update(x=call["x"], y=call["y"], button=call["button"], frame=dict(call["frame"]))
        elif a == "key":
            out["keys"] = list(call["keys"])
        elif a in ("type", "paste", "type_element"):
            out.update(chars=len(call["text"]), text_sha256=hashlib.sha256(call["text"].encode()).hexdigest())
        elif a == "hotkey":
            out["keys"] = list(call["keys"])
        elif a == "move":
            out.update(x=call["x"], y=call["y"], frame=dict(call["frame"]))
        elif a == "select_text":
            out.update(start=call["start"], end=call["end"])
        elif a in FILE_ACTIONS:
            out["resource_ref"] = call["resource_ref"][:12]
            if a == "open_resource":
                out["app_id"] = call["app_id"]
        if a in PIXEL_ACTIONS:
            out["observation"] = call["observation"]
        elif a == "scroll":
            out.update(x=call["x"], y=call["y"], direction=call["direction"], amount=call["amount"], frame=dict(call["frame"]))
        elif a == "drag":
            out.update(start=dict(call["from"]), end=dict(call["to"]), frame=dict(call["frame"]))
        if a in ELEMENT_ACTIONS:
            t = call["target"]
            out.update(snapshot_id=call["snapshot_id"], element=call["element"], app=t["app"], role=t["role"],
                       name_sha256=hashlib.sha256(t["name"].encode()).hexdigest(), window_sha256=hashlib.sha256(t["window"].encode()).hexdigest())
            if a == "set_value":
                out.update(chars=len(call["value"]), text_sha256=hashlib.sha256(call["value"].encode()).hexdigest())
            elif a == "secondary":
                out["action_name"] = call["action_name"]
    return out


STAGES_NOT_MEASURED = ["model", "render"]


def stage_timings(measured: Optional[Mapping[str, int]]) -> dict:
    """Stage timings the Host measured, plus the stages it cannot (model and render happen outside the Host's view)."""
    return {**{k: int(v) for k, v in (measured or {}).items()}, "notMeasured": list(STAGES_NOT_MEASURED)}


def observation_receipt(call: Mapping[str, Any], outcome: str, reply: Optional[Mapping[str, Any]], operation_id: str, approved: bool,
                        timings_ms: Optional[Mapping[str, int]] = None, now: Optional[float] = None) -> dict:
    """`ObservationReceiptV1`: operation id, target identity before/after, timestamp and timebase, action, coordinate space, result. No private text."""
    base = receipt(call, outcome, approved, now)
    r = reply or {}
    return {"version": 1, "operationId": operation_id, "targetBefore": r.get("target_before"), "targetAfter": r.get("target_after"),
            "timestamp": base["timestamp"], "timebase": "unix_s", "action": base["action"], "coordinateSpace": "window", "transform": None,
            "resources": [], "result": outcome, "windowChanged": r.get("window_changed"), "verified": r.get("verified", "unknown"), "humanApproved": bool(approved),
            "stageTimingsMs": stage_timings(timings_ms), "detail": base}


class ApprovalRegistry:
    """In-memory, lock-protected, consume-once human approvals bound to one exact call."""

    def __init__(self, ttl: float = APPROVAL_TTL_S, per_session: int = APPROVAL_PER_SESSION):
        self._ttl, self._cap = ttl, per_session
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], list[tuple[str, float]]] = {}

    def grant(self, owner: str, session_id: str, digest: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            rows = [r for r in self._rows.get((owner, session_id), []) if r[1] > now]
            rows.append((digest, now + self._ttl))
            self._rows[(owner, session_id)] = rows[-self._cap:]

    def revoke(self, owner: Optional[str] = None, session_id: Optional[str] = None) -> int:
        """Drop pending approvals (a human takeover): for one (owner, session) or, with no arguments, all of them."""
        with self._lock:
            keys = [k for k in self._rows if (owner is None or k[0] == owner) and (session_id is None or k[1] == session_id)]
            n = sum(len(self._rows.pop(k)) for k in keys)
            return n

    def consume(self, owner: str, session_id: str, digest: str, *, enabled: bool = True, now: Optional[float] = None) -> bool:
        if not enabled:
            return False
        now = time.monotonic() if now is None else now
        with self._lock:
            rows = [r for r in self._rows.get((owner, session_id), []) if r[1] > now]
            hit = next((r for r in rows if r[0] == digest), None)
            if hit is not None:
                rows.remove(hit)
            if rows:
                self._rows[(owner, session_id)] = rows
            else:
                self._rows.pop((owner, session_id), None)
            return hit is not None


REGISTRY = ApprovalRegistry()
