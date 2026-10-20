"""
YouTube handling — transcript extraction, comment fetching (yt-dlp),
and context formatting for LLM injection. Used by chat_handler.py.
"""

import asyncio
import json
import logging
import time
import shutil
import sys
import urllib.parse
import re
import requests
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YOUTUBE_INSTRUCTION_PROMPT = """Use the supplied YouTube transcript and comments as evidence for the user's question. Cite the video and transcript timestamps when useful, and say when captions do not establish an answer. You may use other retrieval when the user asks for information outside the supplied video or when captions are missing; do not force a summary format."""

TRANSCRIPT_CONTEXT_LIMIT = 12000
TRANSCRIPT_TIMEOUT = 20
_QUERY_STOPWORDS = {"the", "and", "for", "when", "what", "with", "from", "that", "this", "are", "how", "why", "does", "did", "can", "you", "video", "tell", "about"}

# ---------------------------------------------------------------------------
# Init / helpers
# ---------------------------------------------------------------------------

# Will be set at startup by init_youtube()
YouTubeTranscriptApi = None
YOUTUBE_AVAILABLE = False


def _find_ytdlp() -> str:
    """Find the yt-dlp binary: venv bin first, then system PATH."""
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return str(venv_bin)
    found = shutil.which("yt-dlp")
    return found or "yt-dlp"


def init_youtube():
    """Import and cache the YouTube transcript API."""
    global YouTubeTranscriptApi, YOUTUBE_AVAILABLE
    try:
        from youtube_transcript_api import YouTubeTranscriptApi as _Api
        YouTubeTranscriptApi = _Api
        YOUTUBE_AVAILABLE = True
        logger.info("YouTube transcript API available")
    except ImportError as e:
        logger.warning(f"youtube-transcript-api not installed: {e}")
        YOUTUBE_AVAILABLE = False


def is_youtube_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    return "youtube.com" in url or "youtu.be" in url


# youtube.com-shaped hosts. music.youtube.com serves the same /watch and
# /shorts paths, so links shared from YouTube Music must resolve too.
_YT_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com")
# Path prefixes whose first following segment is the video id. Covers the
# /embed/ player, Shorts (/shorts/), live streams (/live/), and the legacy
# /v/ embed — all of which `is_youtube_url` already treats as YouTube, so
# they must be extractable or the link is silently dropped (neither web-fetched
# nor transcript-fetched) by the chat pipeline.
_YT_PATH_PREFIXES = ("/embed/", "/shorts/", "/live/", "/v/")


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract a YouTube video ID from the common URL shapes:
    watch?v=, youtu.be/<id>, /embed/<id>, /shorts/<id>, /live/<id>, /v/<id>,
    across youtube.com / m.youtube.com / music.youtube.com / youtu.be."""
    if not isinstance(url, str):
        return None
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _YT_HOSTS:
        if parsed.path == "/watch":
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("v"):
                return params["v"][0]
        else:
            for prefix in _YT_PATH_PREFIXES:
                if parsed.path.startswith(prefix):
                    vid = parsed.path[len(prefix):].split("/")[0]
                    if vid:
                        return vid
    elif host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        if vid:
            return vid
    return None


def _caption_error_kind(error: Exception) -> str:
    name = type(error).__name__.lower()
    if ("notranscript" in name or "disabled" in name or "caption" in name
            or "no captions" in str(error).lower()):
        return "missing_captions"
    return "retrieval_failed"


def _select_context_segments(segments: list[dict], query: str = "", limit: int = TRANSCRIPT_CONTEXT_LIMIT, url: str = "") -> list[dict]:
    """Select bounded windows after the complete timed transcript is fetched."""
    if not segments:
        return []
    terms = {term for term in re.findall(r"[\w']{3,}", (query or "").lower()) if term not in _QUERY_STOPWORDS}
    scored = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        overlap = terms.intersection(re.findall(r"[\w']{3,}", seg.get("text", "").lower()))
        if overlap:
            scored.append((len(overlap), i))
    matches = [i for _score, i in sorted(scored, key=lambda item: (-item[0], item[1]))]
    if not matches:
        # Cover the whole duration when there is no lexical answer match.
        count = min(12, len(segments))
        candidates = sorted({round(i * (len(segments) - 1) / max(1, count - 1)) for i in range(count)})
    else:
        candidates = []
        for index in matches:
            candidates.extend(range(max(0, index - 2), min(len(segments), index + 3)))
        # Rank windows by useful term overlap, then render chronologically.
        candidates = sorted(set(candidates), key=lambda i: (-len(terms.intersection(
            re.findall(r"[\w']{3,}", segments[i].get("text", "").lower())
        )), i))
    chosen = []
    chosen_lengths = []
    seen = set()
    for index in candidates:
        if index in seen:
            continue
        candidate = segments[index]
        if not isinstance(candidate, dict):
            continue
        seconds = int(float(candidate.get("start", 0)))
        line_length = len(f"[{candidate.get('timestamp', '00:00')}] {candidate.get('text', '')} ({_timestamp_url(url, seconds)})\n") if url else len(candidate.get("text", "")) + 28
        proposed = sum(chosen_lengths) + line_length
        if proposed > limit:
            # Keep the strongest candidate available for the formatter to
            # truncate deliberately. Never emit a segment when no body budget
            # remains; that used to make a zero-budget selection look full.
            if not chosen and limit > 0:
                chosen.append(candidate)
                chosen_lengths.append(proposed)
            continue
        chosen.append(candidate)
        chosen_lengths.append(line_length)
        seen.add(index)
    return sorted(chosen, key=lambda item: item.get("start", 0))


def _timestamp_url(url: str, seconds: int) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in {"t", "start"}]
    query.append(("t", f"{seconds}s"))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    urllib.parse.urlencode(query), parsed.fragment))


class _DeadlineSession(requests.Session):
    def __init__(self, deadline: float, cancelled: threading.Event):
        super().__init__()
        self.deadline = deadline
        self.cancelled = cancelled

    def request(self, method, url, **kwargs):
        if self.cancelled.is_set():
            raise asyncio.CancelledError()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Transcript retrieval deadline exceeded")
        requested_timeout = kwargs.get("timeout")
        kwargs["timeout"] = min(float(requested_timeout) if requested_timeout is not None else remaining, remaining)
        return super().request(method, url, **kwargs)


def _fetch_transcript_sync(video_id: str, preferred_languages: Optional[Iterable[str]], deadline: float, cancelled) -> dict:
    session = _DeadlineSession(deadline, cancelled)
    try:
        if cancelled.is_set():
            raise asyncio.CancelledError()
        api = YouTubeTranscriptApi(http_client=session)
        if cancelled.is_set():
            raise asyncio.CancelledError()
        transcript_list = api.list(video_id)
        available = list(transcript_list)
        if not available:
            raise LookupError("No captions are available for this video")
        preferred = list(preferred_languages or ())
        selected = None
        for language in preferred:
            for candidate in available:
                if candidate.language_code == language:
                    selected = candidate
                    break
            if selected:
                break
        caption_origin = "manual"
        source_language = None
        source_language_code = None
        if selected is None and preferred:
        # A requested language may only be available through YouTube's
        # translation track. Keep that provenance explicit in the result.
            for candidate in available:
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                if getattr(candidate, "is_translatable", False):
                    try:
                        if cancelled.is_set():
                            raise asyncio.CancelledError()
                        selected = candidate.translate(preferred[0])
                        caption_origin = "translated"
                        source_language = candidate.language
                        source_language_code = candidate.language_code
                        break
                    except Exception:
                        continue
        if selected is None:
        # TranscriptList iteration puts manual tracks before generated tracks.
            selected = available[0]
        if cancelled.is_set():
            raise asyncio.CancelledError()
        if cancelled.is_set():
            raise asyncio.CancelledError()
        fetched = selected.fetch()
        if caption_origin != "translated":
            caption_origin = "automatic" if selected.is_generated else "manual"
        return {"fetched": fetched, "caption_origin": caption_origin,
                "source_language": source_language or selected.language,
                "source_language_code": source_language_code or selected.language_code}
    finally:
        session.close()


async def extract_transcript_async(
    url: str, video_id: str, max_retries: int = 3, query: str = "",
    preferred_languages: Optional[Iterable[str]] = None,
    timeout: float = TRANSCRIPT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Async YouTube transcript extraction with retries.

    Args:
        url: Full YouTube URL
        video_id: Extracted video ID
        max_retries: Number of attempts

    Returns:
        Dict with success/error/transcript keys
    """
    if not YOUTUBE_AVAILABLE or YouTubeTranscriptApi is None:
        return {"success": False, "status": "unavailable", "error": "YouTube transcript API not available", "transcript": None}

    deadline = time.monotonic() + max(0.01, timeout)
    cancelled = threading.Event()
    for attempt in range(max_retries):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = await asyncio.wait_for(
                asyncio.to_thread(_fetch_transcript_sync, video_id, preferred_languages, deadline, cancelled),
                timeout=remaining,
            )
            transcript = result["fetched"]

            formatted = []
            for snippet in getattr(transcript, "snippets", transcript):
                text = (snippet.get("text", "") if isinstance(snippet, dict) else getattr(snippet, "text", "")).strip()
                if not text:
                    continue
                start = snippet.get("start", 0) if isinstance(snippet, dict) else getattr(snippet, "start", 0)
                duration = snippet.get("duration", 0) if isinstance(snippet, dict) else getattr(snippet, "duration", 0)
                formatted.append({
                    "text": text,
                    "start": start,
                    "duration": duration,
                    "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
                })

            full_text = " ".join(e["text"] for e in formatted)
            context_segments = _select_context_segments(formatted, query, TRANSCRIPT_CONTEXT_LIMIT, url)
            context_text = " ".join(e["text"] for e in context_segments)

            fetched_count = len(getattr(transcript, "snippets", transcript))
            return {
                "success": True,
                "status": "empty" if not formatted else "complete",
                "source_status": "complete",
                "transcript": context_text,
                "full_transcript": full_text,
                "video_id": video_id,
                "query": query,
                "language": transcript.language,
                "language_code": transcript.language_code,
                "caption_origin": result["caption_origin"],
                "source_language": result["source_language"],
                "source_language_code": result["source_language_code"],
                "is_generated": transcript.is_generated,
                "segments": formatted,
                "context_segments": context_segments,
            }
        except asyncio.TimeoutError:
            cancelled.set()
            return {"success": False, "status": "retrieval_failed", "error": "Transcript retrieval timed out", "transcript": None}
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception as e:
            logger.warning(f"Transcript attempt {attempt + 1} failed: {e}")
            error_kind = _caption_error_kind(e)
            if error_kind == "missing_captions":
                return {"success": False, "status": error_kind, "error": str(e), "transcript": None}
            if attempt < max_retries - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1 * (attempt + 1), remaining))

    return {"success": False, "status": "retrieval_failed", "error": f"Failed after {max_retries} attempts or deadline", "transcript": None}


def format_transcript_for_context(
    transcript_data: Dict[str, Any], url: str,
    title: str = "", channel: str = ""
) -> str:
    """Format transcript data for inclusion in LLM context."""
    title = str(title or "")[:256]
    channel = str(channel or "")[:128]
    raw_video_id = str(transcript_data.get("video_id") or "")
    video_id = raw_video_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw_video_id) else ""
    canonical_url = (f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id, safe='-_.~_')}"
                     if video_id else str(url or "")[:512])
    if not transcript_data.get("success"):
        header = ""
        if title:
            header = f" \"{title}\""
            if channel:
                header += f" by {channel}"
        status = transcript_data.get("status", "retrieval_failed")
        text = f"\n[YouTube Video{header}: Transcript unavailable ({status}: {transcript_data.get('error', 'Unknown error')}). Captions are not evidence for this question; use comments or other retrieval when appropriate.]"
        return text[:TRANSCRIPT_CONTEXT_LIMIT]

    transcript = str(transcript_data.get("transcript", "") or "")
    language = transcript_data.get("language", "unknown")
    is_generated = transcript_data.get("is_generated", False)
    segments = transcript_data.get("segments") or transcript_data.get("context_segments", [])
    caption_origin = transcript_data.get("caption_origin", "automatic" if is_generated else "manual")

    ctx = "\n[YOUTUBE VIDEO TRANSCRIPT]\n"
    if title:
        ctx += f"Title: {title}\n"
    if channel:
        ctx += f"Channel: {channel}\n"
    ctx += f"Video ID: {video_id}\n"
    source_language = transcript_data.get("source_language")
    source_suffix = f"; source: {source_language} ({transcript_data.get('source_language_code')})" if source_language and caption_origin == "translated" else ""
    ctx += f"Language: {language} ({transcript_data.get('language_code', 'unknown')}){source_suffix}\n"
    ctx += f"Caption source: {caption_origin}\n"
    ctx += f"URL: {canonical_url}\n\n"
    footer = "\n[END TRANSCRIPT]\n"
    omission = "[Transcript context bounded; additional selected captions omitted.]\n"
    # Select against the actual header/footer budget so strong late captions
    # survive final rendering instead of being lost to a blind tail slice.
    omitted = False
    if segments:
        original_segment_count = len(segments)
        available = max(0, TRANSCRIPT_CONTEXT_LIMIT - len(ctx) - len(footer) - len(omission))
        segments = _select_context_segments(segments, transcript_data.get("query", ""), available, canonical_url)
        omitted = len(segments) < original_segment_count
    # Include timestamped segments for the LLM to reference
    if segments:
        ctx += "Timestamped Transcript:\n"
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            timestamp = seg.get("timestamp", "00:00")
            seconds = int(float(seg.get("start", 0)))
            timestamp_url = _timestamp_url(canonical_url, seconds)
            line = f"[{timestamp}] {str(seg.get('text', '') or '')} ({timestamp_url})\n"
            if len(ctx) + len(line) + len(footer) <= TRANSCRIPT_CONTEXT_LIMIT:
                ctx += line
            elif ctx.endswith("Timestamped Transcript:\n"):
                tail_budget = TRANSCRIPT_CONTEXT_LIMIT - len(ctx) - len(footer)
                suffix = f" ({timestamp_url})\n"
                marker = " [caption excerpt truncated]"
                text_budget = max(0, tail_budget - len(f"[{timestamp}] ") - len(suffix) - len(marker) - len(omission))
                if text_budget > 0:
                    ctx += f"[{timestamp}] {str(seg.get('text', '') or '')[:text_budget]}{marker}{suffix}"
                omitted = True
            else:
                omitted = True
        if omitted and len(ctx) + len(omission) + len(footer) <= TRANSCRIPT_CONTEXT_LIMIT:
            ctx += omission
    else:
        ctx += "Transcript:\n"
        ctx += transcript
    if len(ctx) + len(footer) > TRANSCRIPT_CONTEXT_LIMIT:
        bounded_marker = "\n[Transcript context bounded; additional metadata omitted.]\n"
        budget = max(0, TRANSCRIPT_CONTEXT_LIMIT - len(footer) - len(bounded_marker))
        ctx = ctx[:budget] + bounded_marker
    return ctx + footer


async def fetch_youtube_comments(
    video_id: str, max_comments: int = 25, timeout: int = 30
) -> Dict[str, Any]:
    """Fetch top comments for a YouTube video using yt-dlp.

    Returns dict with 'success', 'comments' list, 'error'.
    """
    try:
        cmd = [
            _find_ytdlp(),
            "--skip-download",
            "--write-comments",
            "--extractor-args", f"youtube:max_comments={max_comments},all,100,0",
            "--dump-json",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            f"https://www.youtube.com/watch?v={video_id}",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Bound the wait on the process actually finishing, not on spawning it.
        # create_subprocess_exec returns as soon as the child starts, so wrapping
        # it in wait_for never enforces the timeout — proc.communicate() is the
        # blocking step. Kill and reap the child if it overruns so it does not
        # linger after we return.
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            return {"success": False, "error": f"yt-dlp failed: {stderr.decode()[:200]}", "comments": []}

        data = json.loads(stdout.decode())
        title = data.get("title", "")
        channel = data.get("channel", "") or data.get("uploader", "")
        raw_comments = data.get("comments", [])

        comments = []
        for c in raw_comments[:max_comments]:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            comments.append({
                "author": c.get("author", "Unknown"),
                "text": text,
                "likes": c.get("like_count", 0),
            })

        # Sort by likes descending — most popular comments first
        comments.sort(key=lambda x: x.get("likes", 0), reverse=True)

        return {"success": True, "comments": comments, "count": len(comments),
                "title": title, "channel": channel}

    except asyncio.TimeoutError:
        logger.warning(f"Comment fetch timed out for {video_id}")
        return {"success": False, "error": "Comment fetch timed out", "comments": []}
    except FileNotFoundError:
        logger.warning("yt-dlp not installed — cannot fetch comments")
        return {"success": False, "error": "yt-dlp not installed", "comments": []}
    except Exception as e:
        logger.warning(f"Failed to fetch comments for {video_id}: {e}")
        return {"success": False, "error": str(e), "comments": []}


def format_comments_for_context(comments_data: Dict[str, Any], url: str) -> str:
    """Format YouTube comments for inclusion in LLM context."""
    if not comments_data.get("success") or not comments_data.get("comments"):
        return ""

    comments = comments_data["comments"]
    ctx = f"\n[YOUTUBE VIDEO COMMENTS — Top {len(comments)} by popularity]\n"
    ctx += f"URL: {url}\n\n"

    for i, c in enumerate(comments, 1):
        if not isinstance(c, dict):
            continue
        likes = c.get("likes", 0)
        likes_str = f" [{likes} likes]" if likes else ""
        ctx += f"{i}. @{c['author']}{likes_str}: {c['text']}\n\n"

    if len(ctx) > 4000:
        ctx = ctx[:4000] + "\n[Comments truncated]\n"

    ctx += "[END COMMENTS]\n"
    return ctx
