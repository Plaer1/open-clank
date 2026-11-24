import asyncio
import inspect
import json
import re
import time
from secrets import token_urlsafe
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from typing import Dict, Any

from src.constants import MAX_OUTPUT_CHARS

_WEB_PAGE_CHARS = 7000
_WEB_CAPTURE_LIMIT = 2_000_000
_WEB_CAPTURE_COUNT = 8
_WEB_CAPTURE_TTL = 300
_WEB_CAPTURE_LOCK = Lock()
_WEB_CAPTURE = {}


def _public_source_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def _web_scope(ctx):
    owner = ctx.get("owner")
    session = ctx.get("session_id")
    if not isinstance(owner, str) or not owner or not isinstance(session, str) or not session:
        return None
    return (owner, session, ctx.get("actor_id") or "main")


def _web_page(capture, offset):
    text = capture["text"]
    end = min(len(text), offset + _WEB_PAGE_CHARS, offset + max(1, MAX_OUTPUT_CHARS - 1800))
    next_page = f"{capture['id']}.{end}" if capture["retained"] and end < len(text) else None
    complete = end == len(text) and not capture["truncated"]
    evidence = {
        **capture["provenance"], "response_id": capture["id"], "offset": offset,
        "next": next_page, "complete": complete, "source_truncated": capture["truncated"],
    }
    notices = []
    if capture["truncated"]:
        notices.append("The original response reached its download budget.")
    if next_page:
        notices.append(f"Continue this response with web_fetch continuation {next_page}.")
    elif end < len(text):
        notices.append("The remaining text is unavailable for paging.")
    title = capture["title"]
    final_url = evidence.get("final_url") or capture["display_url"]
    heading = "\n".join(part for part in (
        *notices, f"# {title}" if title else "", f"Source: {final_url}" if final_url else "",
    ) if part)
    return {"output": heading + "\n\n" + text[offset:end], "exit_code": 0,
            "metadata": {"webfetch": evidence}}


class WebSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search import SearchService
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        query = raw
        time_filter = None
        max_pages = 5
        provider = None
        mode = "results"
        intent = "web"
        operation_id = None
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "query" in parsed:
                    query = str(parsed.get("query", "")).strip()
                    if parsed.get("provider") is not None:
                        provider = str(parsed.get("provider")).strip() or None
                    if parsed.get("operation_id") is not None:
                        operation_id = str(parsed.get("operation_id")).strip() or None
                    if parsed.get("mode") in ("results", "read", "answer"):
                        mode = parsed["mode"]
                    if parsed.get("intent") in ("web", "image"):
                        intent = parsed["intent"]
                    tf = parsed.get("time_filter") or parsed.get("freshness")
                    if isinstance(tf, str) and tf.lower() in ("day", "week", "month", "year"):
                        time_filter = tf.lower()
                    mp = parsed.get("max_pages")
                    if isinstance(mp, int) and 1 <= mp <= 10:
                        max_pages = mp
            except json.JSONDecodeError:
                pass
        if not query:
            query = raw.split("\n")[0].strip()
        if time_filter is None:
            q_lc = query.lower()
            if any(kw in q_lc for kw in ("today", "latest", "breaking", "this morning", "right now", "currently")):
                time_filter = "day"
            elif any(kw in q_lc for kw in ("this week", "past week", "recent news", "last few days")):
                time_filter = "week"
            elif any(kw in q_lc for kw in ("this month", "past month")):
                time_filter = "month"
            elif " news" in q_lc or q_lc.startswith("news ") or q_lc.endswith(" news"):
                time_filter = "week"
        loop = asyncio.get_running_loop()
        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Searching web for: {query[:160]}",
            })
        try:
            response = await SearchService().search(
                query, caller="code", mode=mode, provider=provider, count=max_pages,
                freshness=time_filter, owner=(ctx.get("owner") or "") if isinstance(ctx, dict) else "",
                deadline=30, intent=intent,
                operation_id=operation_id,
            )
        except asyncio.TimeoutError:
            return {
                "error": f"web_search timed out after 30s: {query[:200]}",
                "exit_code": 1,
            }
        except Exception as e:
            return {
                "error": f"web_search failed: {type(e).__name__}: {str(e) or 'no details'}",
                "exit_code": 1,
            }
        if response.error:
            return {"error": f"web_search: {response.error.get('message', response.status)}", "exit_code": 1,
                    "status": response.status, "result": response.as_dict()}
        text = "\n\n".join(f"{r.title}\n{r.snippet}" for r in response.results)
        sources = [r.as_dict() for r in response.results]
        if progress_cb:
            await progress_cb({
                "elapsed_s": 30,
                "tail": "Search completed; preparing sources.",
            })
        output = text[:MAX_OUTPUT_CHARS] if len(text) > MAX_OUTPUT_CHARS else text
        if sources:
            output += "\n\n<!-- SOURCES:" + json.dumps(sources) + " -->"
        evidence = {
            "kind": "image_search" if intent == "image" else "web_search",
            "selected_provider": response.selected_provider,
            "actual_provider": response.actual_provider,
            "state": response.status, "mode": response.mode,
            "cached": response.cached, "truncated": len(text) > MAX_OUTPUT_CHARS,
            "sources": [{"url": url, "title": result.title[:300], "kind": "image" if result.image_url else "web",
                         "fetched_state": result.fetched_status}
                        for result in response.results if (url := _public_source_url(result.url))],
        }
        return {"output": output, "exit_code": 0, "metadata": {"search": evidence}}

class WebFetchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search.content import fetch_webpage_content
        from src.constants import WEB_FETCH_HARD_MAX_BYTES
        raw = content.strip()
        url = ""
        max_bytes = None
        continuation = None
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    url = str(parsed.get("url") or "").strip()
                    continuation = parsed.get("continuation")
                    # Download-budget override (#3812): "full": true raises the
                    # budget to the hard cap; an explicit max_bytes is clamped
                    # to the hard cap downstream. Default stays the soft cap.
                    if parsed.get("full") is True:
                        max_bytes = WEB_FETCH_HARD_MAX_BYTES
                    mb = parsed.get("max_bytes")
                    if isinstance(mb, int) and mb > 0:
                        max_bytes = mb
            except json.JSONDecodeError:
                url = ""
        if continuation is not None:
            matched = re.fullmatch(r"([A-Za-z0-9_-]{32})\.([1-9][0-9]{0,6})", continuation) if isinstance(continuation, str) else None
            scope = _web_scope(ctx)
            if matched is None or scope is None:
                return {"error": "Web response continuation is unavailable.", "exit_code": 1}
            response_id, offset_text = matched.groups()
            with _WEB_CAPTURE_LOCK:
                capture = _WEB_CAPTURE.get(response_id)
                if capture is not None and capture["expires"] <= time.monotonic():
                    _WEB_CAPTURE.pop(response_id, None)
                    capture = None
            offset = int(offset_text)
            if capture is None or capture["scope"] != scope or not 0 < offset < len(capture["text"]):
                return {"error": "Web response continuation expired or is unavailable.", "exit_code": 1}
            return _web_page(capture, offset)

        if not url:
            url = raw.split("\n")[0].strip()
        if not url or url.startswith("{") or any(c in url for c in (" ", "\t", "\n")):
            return {"error": "web_fetch: provide a single URL or domain, e.g. example.com", "exit_code": 1}
        low = url.lower()
        if "://" in low and not low.startswith(("http://", "https://")):
            return {"error": f"web_fetch: unsupported URL scheme (only http/https): {url[:80]}", "exit_code": 1}
        if not low.startswith(("http://", "https://")):
            url = "https://" + url
        loop = asyncio.get_running_loop()
        try:
            def _fetch():
                kwargs = {"timeout": 10}
                try:
                    sig = inspect.signature(fetch_webpage_content)
                    if "max_bytes" in sig.parameters:
                        kwargs["max_bytes"] = max_bytes
                except (TypeError, ValueError):
                    # Some deployed/test shims may not expose a signature.
                    # Prefer compatibility over failing the whole fetch.
                    pass
                return fetch_webpage_content(url, **kwargs)

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {"error": f"web_fetch: timed out fetching {url}", "exit_code": 1}
        except Exception as e:
            return {"error": f"web_fetch: {url}: {e}", "exit_code": 1}
        err = result.get("error")
        text = (result.get("content") or "").strip()
        title = result.get("title") or ""

        if not text:
            if err:
                return {"error": f"web_fetch: {url}: {err}", "exit_code": 1}
            return {"error": f"web_fetch: {url}: no readable text content (not HTML, or the page needs JS/login)", "exit_code": 1}

        scope = _web_scope(ctx)
        observed = result.get("provenance")
        observed = observed if isinstance(observed, dict) else {}
        keep = bool(scope and _WEB_PAGE_CHARS < len(text) and len(text.encode("utf-8")) <= _WEB_CAPTURE_LIMIT)
        response_id = token_urlsafe(24)
        capture = {
            "id": response_id, "scope": scope, "text": text, "title": title[:300],
            "display_url": _public_source_url(url),
            "expires": time.monotonic() + _WEB_CAPTURE_TTL,
            "retained": keep, "truncated": result.get("truncated") is True,
            "provenance": {
                "final_url": _public_source_url(observed.get("final_url")),
                "fetched_at": observed.get("fetched_at"),
                "result_kind": observed.get("result_kind") or "unavailable",
                "content_type": observed.get("content_type"),
                "url_parameters_omitted": observed.get("url_parameters_omitted") is True,
            },
        }
        if keep:
            with _WEB_CAPTURE_LOCK:
                for expired in [key for key, entry in _WEB_CAPTURE.items() if entry["expires"] <= time.monotonic()]:
                    _WEB_CAPTURE.pop(expired, None)
                if len(_WEB_CAPTURE) >= _WEB_CAPTURE_COUNT:
                    _WEB_CAPTURE.pop(next(iter(_WEB_CAPTURE)))
                _WEB_CAPTURE[response_id] = capture
        return _web_page(capture, 0)
