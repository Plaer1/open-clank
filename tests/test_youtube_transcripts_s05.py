import asyncio
from types import SimpleNamespace
import pytest

from src import youtube_handler


def _fetched(snippets, language="French", language_code="fr", is_generated=False):
    return SimpleNamespace(
        snippets=[SimpleNamespace(**item) for item in snippets],
        language=language,
        language_code=language_code,
        is_generated=is_generated,
    )


def test_long_transcript_selects_late_query_match_and_timestamp_link(monkeypatch):
    snippets = [{"text": "the when and what of unrelated filler " * 30, "start": i * 10.0, "duration": 4.0} for i in range(300)]
    snippets.append({"text": "late orbital mechanics answer", "start": 3661.0, "duration": 6.0})

    class Track:
        language = "French"
        language_code = "fr"
        is_generated = False

        def fetch(self):
            return _fetched(snippets)

    class Api:
        def __init__(self, **_kwargs):
            pass

        def list(self, _video_id):
            return [Track()]

    monkeypatch.setattr(youtube_handler, "YOUTUBE_AVAILABLE", True)
    monkeypatch.setattr(youtube_handler, "YouTubeTranscriptApi", Api)
    data = asyncio.run(youtube_handler.extract_transcript_async(
        "https://youtu.be/video", "video", query="When are orbital mechanics discussed?"
    ))

    assert data["success"] is True
    assert data["language_code"] == "fr"
    assert data["caption_origin"] == "manual"
    assert "late orbital mechanics answer" in data["transcript"]
    assert data["source_status"] == "complete"
    out = youtube_handler.format_transcript_for_context(data, "https://youtu.be/video")
    assert "[61:01] late orbital mechanics answer (https://www.youtube.com/watch?v=video&t=3661s)" in out


def test_timestamp_replaces_existing_time_and_context_has_a_hard_bound():
    data = {
        "success": True, "transcript": "x", "video_id": "x", "language": "en",
        "segments": [{"timestamp": "00:01", "start": 61, "text": "a" * 30000}],
    }
    out = youtube_handler.format_transcript_for_context(
        data, "https://www.youtube.com/watch?v=x&t=3s#comments"
    )
    assert len(out) <= youtube_handler.TRANSCRIPT_CONTEXT_LIMIT
    assert "t=61s" in out and "t=3s" not in out
    assert "URL: https://www.youtube.com/watch?v=x" in out
    assert "caption excerpt truncated" in out


def test_huge_metadata_keeps_strong_late_answer_and_marks_omission():
    data = {
        "success": True, "video_id": "late_answer", "language": "en",
        "query": "orbital mechanics", "segments": [
            {"timestamp": "00:01", "start": 1, "text": "filler " * 10000},
            {"timestamp": "61:01", "start": 3661, "text": "strong orbital mechanics answer"},
        ],
    }
    out = youtube_handler.format_transcript_for_context(
        data, "https://youtu.be/late_answer", title="T" * 30000, channel="C" * 30000
    )
    assert len(out) <= youtube_handler.TRANSCRIPT_CONTEXT_LIMIT
    assert "strong orbital mechanics answer" in out
    assert "additional selected captions omitted" in out
    assert "T" * 257 not in out and "C" * 129 not in out

    huge = youtube_handler.format_transcript_for_context(
        {**data, "transcript": "z" * 50000}, "https://youtu.be/x?" + "q=" + "a" * 30000,
        title="T" * 30000, channel="C" * 30000,
    )
    assert len(huge) <= youtube_handler.TRANSCRIPT_CONTEXT_LIMIT
    assert "context bounded" in huge or "excerpt truncated" in huge


def test_translated_provenance_retains_original_track(monkeypatch):
    class Track:
        language = "Spanish"
        language_code = "es"
        is_generated = False
        is_translatable = True

        def translate(self, language):
            assert language == "en"
            return SimpleNamespace(language="English", language_code="en", is_generated=True,
                                   fetch=lambda: _fetched([{"text": "translated", "start": 0, "duration": 1}], "English", "en", True))

    class Api:
        def __init__(self, **_kwargs):
            pass

        def list(self, _video_id):
            return [Track()]

    monkeypatch.setattr(youtube_handler, "YOUTUBE_AVAILABLE", True)
    monkeypatch.setattr(youtube_handler, "YouTubeTranscriptApi", Api)
    data = asyncio.run(youtube_handler.extract_transcript_async("u", "v", preferred_languages=["en"]))
    assert data["caption_origin"] == "translated"
    assert data["source_language_code"] == "es"


def test_cancelled_worker_checks_before_api_stage(monkeypatch):
    cancelled = asyncio.Event()
    cancelled.set()
    called = {"value": False}

    class Api:
        def __init__(self, **kwargs):
            called["value"] = True

    monkeypatch.setattr(youtube_handler, "YouTubeTranscriptApi", Api)
    with pytest.raises(asyncio.CancelledError):
        youtube_handler._fetch_transcript_sync("v", None, 9999999999, cancelled)
    assert called["value"] is False


def test_missing_captions_and_unavailable_api_are_distinct(monkeypatch):
    class Api:
        def __init__(self, **_kwargs):
            pass

        def list(self, _video_id):
            raise LookupError("No captions are available")

    monkeypatch.setattr(youtube_handler, "YOUTUBE_AVAILABLE", True)
    monkeypatch.setattr(youtube_handler, "YouTubeTranscriptApi", Api)
    missing = asyncio.run(youtube_handler.extract_transcript_async("u", "v", max_retries=1))
    assert missing["status"] == "missing_captions"

    monkeypatch.setattr(youtube_handler, "YOUTUBE_AVAILABLE", False)
    unavailable = asyncio.run(youtube_handler.extract_transcript_async("u", "v"))
    assert unavailable["status"] == "unavailable"


def test_prompt_does_not_forbid_follow_up_search():
    assert "Do NOT web search" not in youtube_handler.YOUTUBE_INSTRUCTION_PROMPT
    assert "forbid further search" not in youtube_handler.YOUTUBE_INSTRUCTION_PROMPT
