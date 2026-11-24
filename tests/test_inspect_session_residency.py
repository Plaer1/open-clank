"""Plain-assert tests for scripts/inspect_session_residency.py (no pytest needed)."""
import importlib.util
import io
import json
import os
import sqlite3
import tempfile

_spec = importlib.util.spec_from_file_location(
    "isr", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "inspect_session_residency.py"))
isr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(isr)

NOW = 1_000 * 86_400_000
H, D = isr.HOUR, isr.DAY
SECRET = "SECRET-PROMPT-TEXT"


def make_db(full=True):
    p = os.path.join(tempfile.mkdtemp(), "e.db")
    c = sqlite3.connect(p)
    # columns copied from actor/actor.sql.ts:6-48 (actor_registry) -- only those read
    c.execute("CREATE TABLE actor_registry(session_id TEXT, actor_id TEXT, status TEXT, last_outcome TEXT,"
              " last_turn_time INTEGER, instance_id TEXT, description TEXT)")
    if full:
        # session/session.sql.ts:15-42, message :64-73, part :82-91
        c.execute("CREATE TABLE session(id TEXT, time_updated INTEGER, time_archived INTEGER, title TEXT)")
        c.execute("CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)")
        c.execute("CREATE TABLE part(id TEXT, session_id TEXT, data TEXT)")
        c.executemany("INSERT INTO session VALUES(?,?,?,?)", [
            ("sess-aaaaaaaa-1", NOW - 30 * 60_000, None, SECRET),
            ("sess-bbbbbbbb-2", NOW - 2 * D, None, SECRET),
            ("sess-cccccccc-3", NOW - 9 * D, NOW - D, SECRET)])
        c.execute("INSERT INTO message VALUES('m1','sess-bbbbbbbb-2',?)", (SECRET * 10,))
        c.execute("INSERT INTO part VALUES('p1','sess-aaaaaaaa-1','xx')")
    c.executemany("INSERT INTO actor_registry VALUES(?,?,?,?,?,?,?)", [
        ("sess-aaaaaaaa-1", "act-1", "running", None, NOW - 10 * 60_000, "i1", SECRET),
        ("sess-bbbbbbbb-2", "act-2", "pending", None, NOW - 3 * H, "i1", SECRET),
        ("sess-cccccccc-3", "act-3", "running", None, NOW - 8 * D, "i0", SECRET),
        ("sess-bbbbbbbb-2", "act-4", "idle", "success", NOW - 2 * D, "i1", SECRET),
        ("gone-session-9", "act-5", "idle", None, NOW - 20 * H, "i1", SECRET)])
    c.commit(); c.close()
    return p


def run(p, *extra):
    buf = io.StringIO()
    rc = isr.main([p, *extra], now_ms=NOW, out=buf)
    return rc, buf.getvalue()


def test_counts_by_state():
    con = isr.open_ro(make_db())
    r = isr.measure(con, NOW)
    a = r["actors"]
    assert a["total"] == 5 and a["by_state"] == {"running": 2, "pending": 1, "idle/success": 1, "idle/no-outcome": 1}
    assert r["sessions"]["archived"] == 1 and r["sessions"]["unarchived"] == 2
    assert r["signals"]["non_terminal_actors_7d_or_older"]["count"] == 1
    assert r["signals"]["non_terminal_actors_in_archived_sessions"] == 1
    assert r["signals"]["actors_without_session_row"] == 1
    assert r["largest_sessions"][0]["bytes"] == len(SECRET) * 10


def test_age_bucketing_uses_injected_now():
    con = isr.open_ro(make_db())
    assert isr.measure(con, NOW)["actors"]["non_terminal_age"] == {"<1h": 1, "<1d": 1, "<7d": 0, ">=7d": 1}
    # a later "now" shifts every actor into older buckets
    later = isr.measure(con, NOW + 30 * D)["actors"]["non_terminal_age"]
    assert later == {"<1h": 0, "<1d": 0, "<7d": 0, ">=7d": 3}
    assert isr.bucket(H - 1) == "<1h" and isr.bucket(H) == "<1d" and isr.bucket(7 * D) == ">=7d"


def test_read_only_open():
    con = isr.open_ro(make_db())
    try:
        con.execute("DELETE FROM actor_registry")
        assert False, "write succeeded"
    except sqlite3.OperationalError:
        pass


def test_missing_table_is_unavailable():
    r = isr.measure(isr.open_ro(make_db(full=False)), NOW)
    assert r["actors"]["total"] == 5
    assert r["sessions"].startswith("unavailable (schema: session")
    assert r["largest_sessions"].startswith("unavailable")
    assert "render" and isr.render(r)  # renders without raising


def test_live_path_refused_without_flag():
    roots = isr.live_roots(home="/h", repo="/r")
    assert isr.is_live("/h/.open-clank/x.db", roots) and isr.is_live("/r/data/frankenmemory.db", roots)
    assert isr.is_live("/h/.local/opt/open-clank/e.db", roots) and not isr.is_live("/r/data2/e.db", roots)
    rc, out = run(os.path.join(isr.live_roots()[2], "nope.db"))
    assert rc == 3 and out == ""
    assert isr.main([], out=io.StringIO()) != 0


def test_json_parses_and_has_no_text():
    rc, out = run(make_db(), "--json")
    assert rc == 0
    assert SECRET not in out and json.loads(out)["actors"]["total"] == 5
    rc, txt = run(make_db())
    assert rc == 0 and SECRET not in txt


def test_ids_truncated_by_default():
    _, out = run(make_db(), "--json")
    d = json.loads(out)
    assert d["signals"]["non_terminal_actors_7d_or_older"]["oldest"][0]["session"] == "sess-ccc"
    _, full = run(make_db(), "--json", "--full-ids")
    assert json.loads(full)["signals"]["non_terminal_actors_7d_or_older"]["oldest"][0]["session"] == "sess-cccccccc-3"
