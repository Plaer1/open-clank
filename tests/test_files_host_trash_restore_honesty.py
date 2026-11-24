"""BQ-16: honest managed-media refusal, restore never overwrites, lost-reply restore reconciles by original ids."""
import asyncio
from pathlib import Path

from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import _origin
from tests.test_files_host_trash import CONTEXT, MediaProvenance, provenance, refused, run


def _trash(provider, path):
    return provider.action(CONTEXT, origin_id=_origin(str(path)), action="trash", args={})


def test_managed_media_refusal_is_plain_and_actionable():
    def rows(work):
        return [{"receipt": {"provenance": provenance(work)}}]

    async def body(provider, recorder, work):
        media = work / MediaProvenance.from_receipt(provenance(work)).resolved_media_dir()
        media.mkdir(parents=True)
        (work / "Note.md").write_text("![](x)")
        (media / "image.png").write_bytes(b"png")
        for target, hint in ((work / "Note.md", "use Move"), (media / "image.png", "editing the document")):
            try:
                await provider.action(CONTEXT, origin_id=_origin(str(target)), action="trash", args={})
            except FilesFacadeError as exc:
                assert exc.code == "managed_media_move_unsupported"
                assert "Nothing was changed" in str(exc) and hint in str(exc) and "managed media" in str(exc)
            else:
                raise AssertionError("not refused")
        assert (work / "Note.md").exists() and (media / "image.png").exists()
    run(body, rows_for=rows)


def test_restore_refuses_to_overwrite_a_newer_file_at_the_original_path():
    async def body(provider, recorder, work):
        (work / "a.txt").write_bytes(b"v1")
        entry = (await _trash(provider, work / "a.txt")).action_receipt["receipt"]["trash_entry"]
        (work / "a.txt").write_bytes(b"NEWER bytes")
        try:
            await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="restore", args={"entry": entry})
        except FilesFacadeError as exc:
            assert exc.code == "resource_changed"
        else:
            raise AssertionError("overwrote a newer file")
        assert (work / "a.txt").read_bytes() == b"NEWER bytes" and Path(entry["trashed_path"]).read_bytes() == b"v1"
    run(body)


def test_lost_restore_reply_reconciles_by_original_ids_without_second_restore():
    async def body(provider, recorder, work):
        (work / "a.txt").write_bytes(b"v1")
        entry = (await _trash(provider, work / "a.txt")).action_receipt["receipt"]["trash_entry"]
        arg = {"entry": entry}
        first = await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="restore", args=arg)
        assert first.action_receipt["receipt"].get("reconciled") is None and recorder.operations.count("restore") == 1
        # The first reply was "lost": the caller repeats the same original entry.
        again = await provider.action(CONTEXT, origin_id=_origin(str(work / "a.txt")), action="restore", args=arg)
        assert recorder.operations.count("restore") == 1, "reconciliation must not issue a second restore"
        assert again.action_receipt["receipt"]["reconciled"] is True and again.action_receipt["status"] == "complete"
        assert (work / "a.txt").read_bytes() == b"v1"
        # A different file at the original path is not mistaken for the restored one.
        (work / "a.txt").write_bytes(b"other")
        recorder.operations.clear()
        await refused(provider, recorder, work, work / "a.txt", action="restore", args=arg, code="resource_unavailable")
    run(body)
