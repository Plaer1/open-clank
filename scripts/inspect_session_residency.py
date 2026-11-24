#!/usr/bin/env python3
"""Read-only O13 measurement: session/actor residency from an engine SQLite file.

Measures only what is on disk. It never writes, never imports app modules, and
never prints message/prompt text. In-memory residency (the SessionRunState
runners Map, Host session caches) is NOT observable here.

Schema facts relied on (packages/mimo-code/packages/cli/src/):
  actor/actor.sql.ts:6-48   actor_registry(session_id, actor_id, status
      pending|running|idle, last_outcome, last_turn_time ms, instance_id, ...)
  session/session.sql.ts:15-42  session(id, time_created, time_updated ms, time_archived)
  session/session.sql.ts:64-73,82-91  message/part(session_id, data)
"""
import json
import os
import sqlite3
import sys
import time
from urllib.parse import quote

HOUR, DAY = 3_600_000, 86_400_000
BUCKETS = (("<1h", HOUR), ("<1d", DAY), ("<7d", 7 * DAY), (">=7d", None))
USAGE = """usage: inspect_session_residency.py DB_PATH [--json] [--top N] [--full-ids] [--i-know-this-is-live]
Read-only. DB_PATH is an engine SQLite file you name explicitly; there is no default.
Paths under ~/.open-clank, ~/.local/opt/open-clank or <repo>/data/ are refused
unless --i-know-this-is-live is given."""


def live_roots(home=None, repo=None):
    home = home or os.path.expanduser("~")
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(home, ".open-clank"), os.path.join(home, ".local", "opt", "open-clank"),
            os.path.join(repo, "data")]


def is_live(path, roots=None):
    # shortcut: lexical check only (no symlink resolution, to avoid touching live paths); upgrade if symlinked installs matter.
    p = os.path.abspath(path)
    return any(p == r or p.startswith(r + os.sep) for r in (roots or live_roots()))


def open_ro(path):
    con = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def cols(con, table):
    return {r[1] for r in con.execute('PRAGMA table_info("%s")' % table)}


def bucket(age_ms):
    for name, lim in BUCKETS:
        if lim is None or age_ms < lim:
            return name


def buckets(ages):
    out = {n: 0 for n, _ in BUCKETS}
    for a in ages:
        out[bucket(max(a, 0))] += 1
    return out


def need(con, table, wanted):
    have = cols(con, table)
    miss = sorted(set(wanted) - have)
    if miss:
        return "unavailable (schema: %s %s)" % (table, "missing " + ", ".join(miss) if have else "table absent")


def tid(v, full):
    return v if full or v is None else v[:8]


def measure(con, now_ms, top=10, full_ids=False):
    r = {"now_ms": now_ms, "schema_facts": [
        "actor_registry.status in pending|running|idle; non-terminal = pending|running",
        "actor age = now - actor_registry.last_turn_time (ms)",
        "session age = now - session.time_updated (ms); unarchived = time_archived IS NULL",
        "stored size = sum of length(data) bytes in message and part rows"]}
    # actors
    err = need(con, "actor_registry", ["session_id", "actor_id", "status", "last_outcome", "last_turn_time", "instance_id"])
    if err:
        r["actors"] = err
    else:
        rows = con.execute("SELECT session_id, actor_id, status, last_outcome, last_turn_time, instance_id FROM actor_registry").fetchall()
        by_state, nt_ages, idle_ages, nt = {}, [], [], []
        for sid, aid, st, oc, lt, inst in rows:
            k = st if st != "idle" else "idle/%s" % (oc or "no-outcome")
            by_state[k] = by_state.get(k, 0) + 1
            age = now_ms - (lt or 0)
            if st in ("pending", "running"):
                nt_ages.append(age); nt.append((sid, aid, st, age, inst))
            elif st == "idle":
                idle_ages.append(age)
        r["actors"] = {"total": len(rows), "by_state": by_state,
                       "non_terminal_age": buckets(nt_ages), "idle_age": buckets(idle_ages),
                       "non_terminal_distinct_instance_ids": len({x[4] for x in nt})}
        r["_nt"] = nt
    # sessions
    err = need(con, "session", ["id", "time_updated", "time_archived"])
    if err:
        r["sessions"] = err
    else:
        rows = con.execute("SELECT id, time_updated, time_archived FROM session").fetchall()
        un = [now_ms - (u or 0) for _, u, a in rows if a is None]
        r["sessions"] = {"total": len(rows), "archived": len(rows) - len(un), "unarchived": len(un),
                         "unarchived_age": buckets(un)}
    # largest by stored size
    err = need(con, "message", ["session_id", "data"]) or need(con, "part", ["session_id", "data"])
    if err:
        r["largest_sessions"] = err
    else:
        sz = {}
        for t in ("message", "part"):
            for sid, n in con.execute("SELECT session_id, SUM(length(CAST(data AS BLOB))) FROM %s GROUP BY session_id" % t):
                sz[sid] = sz.get(sid, 0) + (n or 0)
        r["largest_sessions"] = [{"session": tid(s, full_ids), "bytes": n}
                                 for s, n in sorted(sz.items(), key=lambda x: -x[1])[:top]]
    # leak signals (facts only)
    sig = {}
    nt = r.pop("_nt", None)
    if nt is None:
        sig = "unavailable (needs actor_registry)"
    else:
        old = sorted((x for x in nt if x[3] >= 7 * DAY), key=lambda x: -x[3])
        sig["non_terminal_actors_7d_or_older"] = {"count": len(old), "oldest": [
            {"session": tid(x[0], full_ids), "actor": tid(x[1], full_ids), "status": x[2], "age_days": round(x[3] / DAY, 1)}
            for x in old[:top]]}
        if isinstance(r["sessions"], dict):
            arch = {s for (s,) in con.execute("SELECT id FROM session WHERE time_archived IS NOT NULL")}
            sig["non_terminal_actors_in_archived_sessions"] = sum(1 for x in nt if x[0] in arch)
            ids = {s for (s,) in con.execute("SELECT id FROM session")}
            sig["actors_without_session_row"] = sum(
                1 for (s,) in con.execute("SELECT session_id FROM actor_registry") if s not in ids)
        else:
            sig["archived_and_orphan_checks"] = r["sessions"]
    r["signals"] = sig
    return r


# What a Host process cannot see (read from the engine source; nothing here is measured):
MISSING_OBSERVABILITY = [
    "engine SessionRunState.runners: Map<sessionID:agentID, Runner> (session/run-state.ts:54; deleted by onIdle at :80-88, cleared by the scope finalizer at :56-62)",
    "engine ActorSpawn.forkContexts: Map<sessionID:actorID, {revision,hash,ForkContext}> (actor/spawn.ts:226; cleared on terminal status via clearForkContext :228-232)",
    "engine ActorSpawn.outerFibers: Map<key, {revision, Fiber, outcome Deferred}> (actor/spawn.ts:237; deleted at :405, :943, :965, :1036, :1643)",
    "engine ActorSpawn.pendingOutcomes: Map<key, Deferred<AgentOutcome>> (actor/spawn.ts:241; deleted at :1154, :1212, :1617)",
    "engine ActorSpawn.cancelling Set (actor/spawn.ts:254) and stalled-notification Set `notified` (actor/spawn.ts:1770)",
    "engine ExecutionLock lanes (actor/spawn.ts executionLocks) and fibers forked into the layer Scope (Effect.forkIn(scope) at actor/spawn.ts:950, :1191, :1534, :1662, :1862)",
    "engine SessionStatus.Service per-session idle/busy state (set at session/run-state.ts:79-88; storage not read)",
    "Host-side but not inventoried here: upload handler per-owner reference caches, OwnerSessionMap (file backed, not resident), prompt/vision caches under the upload directory",
]

_SESSION_STATES = ("metadata_only", "hydrated_active", "hydrated_idle")


def _chars(history):
    total = 0
    for m in history:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        total += len(c) if isinstance(c, str) else len(str(c))
    return total


def inventory_resident(manager, *, runs=None, max_ids=10, full_ids=False):
    """Read-only in-process inventory of the Host's resident per-session state.

    `manager` is a live core.session_manager.SessionManager: `manager.sessions` is the only resident
    collection it keeps (Dict[str, Session]); each Session holds its message list (`history`).
    `runs` is the src.agent_runs module (or any object with `_RUNS`/`_DRAIN_TASKS`), the Host-side
    per-session run registry that decides which sessions are active: manager state alone has no activity.
    Nothing is mutated, no lock is taken, nothing is evicted, no message text is read out (only lengths).
    Classification: metadata_only = not hydrated (empty history); hydrated_active = hydrated and a run is
    `running`; hydrated_idle = hydrated and no running run (never run, or its run is terminal).
    """
    items = list(getattr(manager, "sessions", {}).items())   # shallow copy of the mapping, objects untouched
    table = dict(getattr(runs, "_RUNS", {})) if runs is not None else {}
    running = {sid for sid, run in table.items() if getattr(run, "status", None) == "running"}
    groups = {name: [] for name in _SESSION_STATES}
    flags = {"archived": 0, "incognito": 0, "important": 0}
    for sid, sess in items:
        hist = getattr(sess, "history", None) or []
        state = "metadata_only" if not hist else "hydrated_active" if sid in running else "hydrated_idle"
        groups[state].append((sid, len(hist), _chars(hist) if hist else 0, getattr(sess, "message_count", 0) or 0))
        flags["archived"] += bool(getattr(sess, "archived", False))
        flags["incognito"] += bool(getattr(sess, "incognito", False))
        flags["important"] += bool(getattr(sess, "is_important", False))
    sessions = {"total": len(items), "flags": flags}
    for name, rows in groups.items():
        rows.sort(key=lambda x: (-x[2], x[0]))
        sessions[name] = {"count": len(rows), "history_messages": sum(x[1] for x in rows),
                          "history_chars": sum(x[2] for x in rows),
                          "persisted_message_count": sum(x[3] for x in rows),
                          "top": [{"session": tid(x[0], full_ids), "messages": x[1], "chars": x[2]} for x in rows[:max_ids]]}
    by_status, frames, subs, evict_pending, unresolved = {}, 0, 0, 0, 0
    rows = []
    for sid, run in table.items():
        st = getattr(run, "status", None) or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        n = len(getattr(run, "buffer", ()) or ())
        frames += n
        subs += len(getattr(run, "subscribers", ()) or ())
        ev = getattr(run, "evict_task", None)
        evict_pending += bool(ev is not None and not ev.done())
        unresolved += bool(getattr(run, "cleanup_unresolved", False))
        rows.append((sid, st, n))
    rows.sort(key=lambda x: (-x[2], x[0]))
    resident = {sid for sid, _ in items}
    run_report = {"total": len(table), "by_status": by_status, "buffer_frames": frames, "subscribers": subs,
                  "evict_timers_pending": evict_pending, "cleanup_unresolved": unresolved,
                  "drain_tasks": len(getattr(runs, "_DRAIN_TASKS", ()) or ()) if runs is not None else None,
                  "runs_without_resident_session": sum(1 for sid in table if sid not in resident),
                  "terminal_runs_retained": sum(1 for _, st, _ in rows if st != "running"),
                  "top": [{"session": tid(sid, full_ids), "status": st, "frames": n} for sid, st, n in rows[:max_ids]]}
    return {"version": 1, "scope": "host_process_in_memory", "max_ids": max_ids,
            "sessions": sessions, "agent_runs": run_report if runs is not None else "not supplied",
            "missing_observability": list(MISSING_OBSERVABILITY)}


def render(r):
    out = ["session residency (read-only; disk state only)", "schema facts relied on:"]
    out += ["  - " + f for f in r["schema_facts"]]
    for key in ("actors", "sessions", "largest_sessions", "signals"):
        out.append("[%s]" % key)
        out.append("  " + json.dumps(r[key], indent=2).replace("\n", "\n  "))
    out.append("not measured: in-memory runners/caches of a running process.")
    return "\n".join(out)


def main(argv, now_ms=None, out=sys.stdout):
    args = list(argv)
    flag = lambda f: (f in args) and (args.remove(f) or True)
    as_json, full, live = flag("--json"), flag("--full-ids"), flag("--i-know-this-is-live")
    top = 10
    if "--top" in args:
        i = args.index("--top")
        try:
            top = int(args[i + 1]); del args[i:i + 2]
        except (IndexError, ValueError):
            print(USAGE, file=sys.stderr); return 2
    if len(args) != 1 or args[0].startswith("-"):
        print(USAGE, file=sys.stderr); return 2
    if is_live(args[0]) and not live:
        print("refused: path looks like a live install; pass --i-know-this-is-live to override", file=sys.stderr)
        return 3
    if not os.path.isfile(args[0]):
        print("not a file: %s" % args[0], file=sys.stderr); return 2
    con = open_ro(args[0])
    try:
        res = measure(con, int(time.time() * 1000) if now_ms is None else now_ms, top, full)
    finally:
        con.close()
    print(json.dumps(res, indent=2) if as_json else render(res), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
