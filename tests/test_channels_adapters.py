"""Telegram (long polling) and Matrix (/sync, unencrypted) adapters against fake services (httpx.MockTransport)."""
import asyncio
import json
from urllib.parse import parse_qsl

import httpx

from src.openclank.channels.matrix import MatrixAdapter
from src.openclank.channels.telegram import TelegramAdapter

TOKEN = "123:SECRET"


def run(coro): return asyncio.run(coro)


async def collect(adapter, want):
    got, done = [], asyncio.Event()
    async def on(inbound):
        got.append(inbound)
        if len(got) >= want: done.set()
    task = asyncio.create_task(adapter.run(on))
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    return got


def recorder():
    slept = []
    async def sleep(s): slept.append(s)
    sleep.slept = slept
    return sleep


# ---------------------------------------------------------------- Telegram
def tg_update(uid, text, *, chat=5, ctype="private", sender=5, bot=False, thread=None, reply_to_bot=False):
    m = {"message_id": uid * 10, "text": text, "chat": {"id": chat, "type": ctype}, "from": {"id": sender, "is_bot": bot, "username": "ann"}}
    if thread: m.update(is_topic_message=True, message_thread_id=thread)
    if reply_to_bot: m["reply_to_message"] = {"from": {"id": 99}}
    return {"update_id": uid, "message": m}


class TgService:
    def __init__(self, batches): self.batches, self.polls, self.sent = list(batches), [], []
    async def handler(self, request):
        if request.url.path == f"/file/bot{TOKEN}/photos/p1.jpg": return httpx.Response(200, content=b"JPEGDATA")
        method = request.url.path.rsplit("/", 1)[1]
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        if method == "getFile": return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/p1.jpg"}})
        form = dict(parse_qsl(request.content.decode())) if b"multipart" not in request.headers.get("content-type", "").encode() else {}
        if method == "getMe": return httpx.Response(200, json={"ok": True, "result": {"id": 99, "username": "ocbot"}})
        if method == "getUpdates":
            self.polls.append(form)
            if not self.batches: await asyncio.sleep(0.02)      # a real long poll waits; never spin
            item = self.batches.pop(0) if self.batches else []
            if isinstance(item, int): return httpx.Response(item, json={"ok": False, "parameters": {"retry_after": 7} if item == 429 else {}})
            return httpx.Response(200, json={"ok": True, "result": item})
        self.sent.append((method, form, request.content)); return httpx.Response(200, json={"ok": True, "result": {}})


def tg_adapter(service, sleep=None):
    return TelegramAdapter(lambda: TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(service.handler)), poll_timeout=0, sleep=sleep or recorder())


def test_telegram_parse_group_mention_bot_and_acknowledge_after_handover():
    svc = TgService([[tg_update(1, "hi"), tg_update(2, "hey @ocbot", chat=-7, ctype="supergroup", sender=6, thread=3),
                      tg_update(3, "other bot", sender=8, bot=True), tg_update(4, "reply", chat=-7, ctype="group", reply_to_bot=True)], []])
    got = run(collect(tg_adapter(svc), 4))
    assert [(g.conv_id, g.sender_id, g.is_direct, g.mentioned, g.thread_id, g.sender_is_bot) for g in got] == [
        ("5", "5", True, False, None, False), ("-7", "6", False, True, "3", False), ("5", "8", True, False, None, True), ("-7", "5", False, True, None, False)]
    assert svc.polls[1]["offset"] == "5"                          # the offset moved past 4 only after the hand-over


def test_telegram_offset_is_not_advanced_when_the_handover_fails():
    svc = TgService([[tg_update(1, "hi")]]); adapter = tg_adapter(svc)
    async def boom(inbound): raise RuntimeError("bridge down")
    try: run(adapter.run(boom))
    except RuntimeError: pass
    assert adapter._offset is None                                # the update will be replayed, not lost


def test_telegram_backoff_retry_after_conflict_and_reset():
    sleep = recorder(); svc = TgService([500, 500, 429, 409, [tg_update(1, "ok")]])
    run(collect(tg_adapter(svc, sleep), 1))
    assert sleep.slept == [1.0, 2.0, 7.0, 60.0]                   # doubling, the service's retry_after, then the max for a second poller


def test_telegram_sends_text_and_file_and_never_shows_the_token():
    svc = TgService([]); adapter = tg_adapter(svc)
    run(adapter.send_text("5", "3", "hello")); run(adapter.send_file("5", None, "a.txt", b"data", "text/plain"))
    assert svc.sent[0][0] == "sendMessage" and svc.sent[0][1] == {"chat_id": "5", "text": "hello", "message_thread_id": "3"}
    assert svc.sent[1][0] == "sendDocument" and b'filename="a.txt"' in svc.sent[1][2] and b"data" in svc.sent[1][2]
    assert TOKEN not in repr(adapter) and adapter.limits.max_text == 4096


# ---------------------------------------------------------------- Matrix
def mx_event(eid, sender, body="hi", etype="m.room.message", msgtype="m.text", mentions=None):
    content = {"msgtype": msgtype, "body": body}
    if mentions: content["m.mentions"] = {"user_ids": mentions}
    return {"event_id": eid, "sender": sender, "type": etype, "content": content}


def mx_sync(batch, rooms):
    return {"next_batch": batch, "rooms": {"join": rooms}}


class MxService:
    def __init__(self, syncs): self.syncs, self.requests = list(syncs), []
    async def handler(self, request):
        self.requests.append((request.method, request.url.path, dict(request.url.params), request.content))
        assert request.headers["authorization"] == "Bearer mxtok"
        if request.url.path.endswith("/whoami"): return httpx.Response(200, json={"user_id": "@bot:hs"})
        if request.url.path.endswith("/sync"):
            if not self.syncs: await asyncio.sleep(0.02)
            item = self.syncs.pop(0) if self.syncs else {"next_batch": "idle"}
            return httpx.Response(item, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 3000} if item == 429 else {}) if isinstance(item, int) else httpx.Response(200, json=item)
        if "/upload" in request.url.path: return httpx.Response(200, json={"content_uri": "mxc://hs/abc"})
        return httpx.Response(200, json={"event_id": "$e"})


def mx_adapter(service, sleep=None):
    return MatrixAdapter(lambda: "https://hs.example", lambda: "mxtok", httpx.AsyncClient(transport=httpx.MockTransport(service.handler)), sync_timeout_ms=0, sleep=sleep or recorder())


def test_matrix_first_sync_sets_position_without_history_then_parses_events():
    old = mx_sync("s1", {"!r:hs": {"timeline": {"events": [mx_event("$old", "@u:hs", "ancient")]}}})
    new = mx_sync("s2", {"!r:hs": {"summary": {"m.joined_member_count": 2}, "timeline": {"events": [
        mx_event("$1", "@u:hs", "hello"), mx_event("$2", "@bot:hs", "my own"), mx_event("$3", "@u:hs", "note", msgtype="m.notice"),
        mx_event("$4", "@u:hs", "", etype="m.room.encrypted"), mx_event("$5", "@u:hs", "hey @bot:hs")]}},
        "!g:hs": {"summary": {"m.joined_member_count": 5}, "timeline": {"events": [mx_event("$6", "@v:hs", "group msg", mentions=["@bot:hs"])]}}})
    svc = MxService([old, new, mx_sync("s3", {})])
    got = run(collect(mx_adapter(svc), 4))
    assert [(g.message_id, g.is_direct, g.encrypted, g.mentioned) for g in got] == [("$1", True, False, False), ("$4", True, True, False), ("$5", True, False, True), ("$6", False, False, True)]
    sync_params = [p for m, path, p, c in svc.requests if path.endswith("/sync")]
    assert "since" not in sync_params[0] and sync_params[1]["since"] == "s1" and sync_params[2]["since"] == "s2"


def test_matrix_backoff_and_retry_after_ms():
    sleep = recorder(); svc = MxService([500, 429, mx_sync("s1", {}), mx_sync("s2", {"!r:hs": {"timeline": {"events": [mx_event("$1", "@u:hs")]}}})])
    run(collect(mx_adapter(svc, sleep), 1))
    assert sleep.slept == [1.0, 3.0]


def test_matrix_send_text_and_file_and_no_invites_accepted():
    svc = MxService([]); adapter = mx_adapter(svc)
    run(adapter.send_text("!r:hs", None, "hello")); run(adapter.send_file("!r:hs", None, "p.png", b"PNG", "image/png"))
    (m1, p1, _q, c1), (m2, p2, q2, _c2), (m3, p3, _q3, c3) = svc.requests[0], svc.requests[1], svc.requests[2]
    assert m1 == "PUT" and "/rooms/!r:hs/send/m.room.message/" in p1 and json.loads(c1) == {"msgtype": "m.text", "body": "hello"}
    assert p2.endswith("/_matrix/media/v3/upload") and q2["filename"] == "p.png"
    assert json.loads(c3)["msgtype"] == "m.image" and json.loads(c3)["url"] == "mxc://hs/abc"
    assert not any("/join" in path for _m, path, _q, _c in svc.requests)       # the adapter never joins a room by itself


def test_telegram_photo_and_document_become_lazy_attachments_and_download_through_getfile():
    photo = {"update_id": 1, "message": {"message_id": 1, "caption": "see this", "chat": {"id": 5, "type": "private"}, "from": {"id": 5, "is_bot": False},
             "photo": [{"file_id": "small", "file_unique_id": "u", "file_size": 10}, {"file_id": "big", "file_unique_id": "u2", "file_size": 99}]}}
    doc = {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 5, "type": "private"}, "from": {"id": 5, "is_bot": False},
           "document": {"file_id": "d1", "file_name": "notes.txt", "mime_type": "text/plain", "file_size": 4}}}
    svc = TgService([[photo, doc]])
    got = run(collect(tg_adapter(svc), 2))
    assert got[0].text == "see this" and got[0].attachments[0].mime == "image/jpeg" and got[0].attachments[0].size == 99
    assert got[1].text == "" and got[1].attachments[0].name == "notes.txt" and got[1].attachments[0].mime == "text/plain"
    assert run(got[0].attachments[0].fetch()) == b"JPEGDATA"                  # nothing is downloaded until the bridge asks


def test_matrix_image_event_becomes_an_attachment_downloaded_from_the_media_repo():
    event = {"event_id": "$i", "sender": "@u:hs", "type": "m.room.message", "content": {"msgtype": "m.image", "body": "cat.png", "url": "mxc://hs/abc", "info": {"mimetype": "image/png", "size": 3}}}
    svc = MxService([mx_sync("s1", {}), mx_sync("s2", {"!r:hs": {"timeline": {"events": [event]}}})])
    adapter = mx_adapter(svc); got = run(collect(adapter, 1))
    assert got[0].attachments[0].name == "cat.png" and got[0].attachments[0].mime == "image/png" and got[0].attachments[0].size == 3
    orig = svc.handler
    async def handler(request):
        if "/download/" in request.url.path:
            assert request.url.path.endswith("/_matrix/media/v3/download/hs/abc"); return httpx.Response(200, content=b"PNG")
        return await orig(request)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run(got[0].attachments[0].fetch()) == b"PNG"
