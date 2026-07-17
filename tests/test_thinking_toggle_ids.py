"""Thinking-section toggle ids must be unique per page.

Live bug (2026-07-17): ids were `thinking-${Date.now()}-${index}` with
index almost always 0 (blocks are merged), so two messages rendered in
the same millisecond collided — clicking "View thinking process" on one
message opened ANOTHER message's block (getElementById returns the
first duplicate). A module-level sequence counter makes ids unique.
"""
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "markdown.js"


def test_thinking_section_ids_carry_a_sequence_counter():
    source = _SOURCE.read_text()
    assert "_thinkingSectionSeq" in source
    assert "${++_thinkingSectionSeq}" in source, (
        "thinking ids must include the per-page counter — Date.now() alone "
        "collides across messages rendered in the same millisecond"
    )
