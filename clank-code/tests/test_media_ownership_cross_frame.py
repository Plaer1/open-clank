"""Cross-frame move planning: a document moves with its media into another root.

The identity literals were captured from the unmodified planner before it gained
destination_frame; same-frame plans must stay equal to them field for field.
"""

from __future__ import annotations

import os

from src.openclank.media_ownership import (
    MovePhase, MovePlan, MoveFrame, MovePreflight, binary_digest, document_media_dir, plan_document_move,
)


def _asset(name, doc, media_dir=None, n=1):
    a = {"asset_id": f"a{n}", "name": name, "digest": binary_digest(name.encode()), "owner_document_id": doc}
    if media_dir is not None:
        a["media_dir"] = media_dir
    return a

def _pf(assets=()):
    return MovePreflight(expected_revision="r1", current_revision="r1", assets=tuple(assets))

def identity_cases():
    out = {}
    out["workspace_rename"] = dict(operation_id="op-1", document_id="a.md", source_document_path="a.md",
        destination_document_path="b.md", canonical_root="/r", origin="workspace", owner_subject_id="u",
        source_layout_version=2, preflight=_pf([_asset("i.png", "a.md", ".media/a")]),
        referencing_documents=[("a.md", "![i](.media/a/i.png) [self](a.md)", False, "a.md"), ("o.md", "[a](a.md)", False)])
    out["selected_subfolder"] = dict(operation_id="op-2", document_id="x/n.md", source_document_path="x/n.md",
        destination_document_path="x/y/n.md", canonical_root="/sel", origin="selected_root", owner_subject_id="u",
        source_layout_version=2, preflight=_pf([_asset("p.png", "x/n.md", ".media/x/n", 1), _asset("q.png", "x/n.md", ".media/x/n", 2)]),
        referencing_documents=[("x/n.md", "![p](../.media/x/n/p.png) ![q](<../.media/x/n/q.png>) [[../.media/x/n/p.png]] ![e](https://e.com/a.png) ![l](keep.png)", False, "x/n.md")])
    out["loose_move"] = dict(operation_id="op-3", document_id="n.md", source_document_path="n.md",
        destination_document_path="sub/m.md", canonical_root="/loose", origin="loose", owner_subject_id="u",
        source_layout_version=1, preflight=_pf([_asset("i.png", "n.md", "media/n")]),
        referencing_documents=[("n.md", "![i](media/n/i.png)", False, "n.md")])
    out["workspace_v1_to_v2"] = dict(operation_id="op-4", document_id="d/a.md", source_document_path="d/a.md",
        destination_document_path="d/a.md", canonical_root="/r", origin="workspace", owner_subject_id="u",
        source_layout_version=1, destination_layout_version=2, preflight=_pf([_asset("i.png", "d/a.md", "media/d/a")]),
        referencing_documents=[("d/a.md", "![i](../media/d/a/i.png)", False, "d/a.md")])
    out["workspace_preserve_media"] = dict(operation_id="op-5", document_id="a.md", source_document_path="a.md",
        destination_document_path="c.md", canonical_root="/r", origin="workspace", owner_subject_id="u",
        source_layout_version=2, preserve_media_location=True, preflight=_pf([_asset("i.png", "a.md", ".media/a")]),
        referencing_documents=[("a.md", "![i](.media/a/i.png)", False, "a.md")])
    out["protected_conflict"] = dict(operation_id="op-6", document_id="a.md", source_document_path="a.md",
        destination_document_path="s/a.md", canonical_root="/r", origin="workspace", owner_subject_id="u",
        source_layout_version=2, preflight=_pf([_asset("i.png", "a.md", ".media/a")]),
        referencing_documents=[("a.md", "![i](.media/a/i.png)", False, "a.md"), ("o.md", "![i](.media/a/i.png)", True)])
    return out



_FROZEN = {'loose_move': {'asset_moves': [{'asset_id': 'a1',
                                 'asset_name': 'i.png',
                                 'destination_media_dir': 'sub/media/m',
                                 'digest': 'sha256:0a7b7703c93599170ade5b1451d7d1e4836bc1e51ec55aa40e99b4488ed25018:5',
                                 'owner_document_id': 'n.md',
                                 'source_media_dir': 'media/n'}],
                'conflicts': [],
                'destination_document_path': 'sub/m.md',
                'destination_layout_version': 1,
                'destination_media_dir': 'sub/media/m',
                'document_id': 'n.md',
                'expected_revision': 'r1',
                'operation_id': 'op-3',
                'owner_subject_id': 'u',
                'phase': 'staged',
                'preimages': {'n.md': '![i](media/n/i.png)'},
                'reference_edits': [{'document_id': 'n.md',
                                     'end': 19,
                                     'new_target': 'media/m/i.png',
                                     'new_text': '![i](<media/m/i.png>)',
                                     'old_target': 'media/n/i.png',
                                     'start': 0}],
                'resource_receipt': None,
                'source_document_path': 'n.md',
                'source_layout_version': 1,
                'source_media_dir': 'media/n'},
 'protected_conflict': {'asset_moves': [{'asset_id': 'a1',
                                         'asset_name': 'i.png',
                                         'destination_media_dir': '.media/s/a',
                                         'digest': 'sha256:0a7b7703c93599170ade5b1451d7d1e4836bc1e51ec55aa40e99b4488ed25018:5',
                                         'owner_document_id': 'a.md',
                                         'source_media_dir': '.media/a'}],
                        'conflicts': [{'asset_name': '',
                                       'code': 'protected_reference',
                                       'document_id': 'o.md',
                                       'reason': 'protected reference cannot be updated; move left unapplied',
                                       'reference_target': '.media/a/i.png'}],
                        'destination_document_path': 's/a.md',
                        'destination_layout_version': 2,
                        'destination_media_dir': '.media/s/a',
                        'document_id': 'a.md',
                        'expected_revision': 'r1',
                        'operation_id': 'op-6',
                        'owner_subject_id': 'u',
                        'phase': 'aborted',
                        'preimages': {'a.md': '![i](.media/a/i.png)'},
                        'reference_edits': [{'document_id': 'a.md',
                                             'end': 20,
                                             'new_target': '../.media/s/a/i.png',
                                             'new_text': '![i](<../.media/s/a/i.png>)',
                                             'old_target': '.media/a/i.png',
                                             'start': 0}],
                        'resource_receipt': None,
                        'source_document_path': 'a.md',
                        'source_layout_version': 2,
                        'source_media_dir': '.media/a'},
 'selected_subfolder': {'asset_moves': [{'asset_id': 'a1',
                                         'asset_name': 'p.png',
                                         'destination_media_dir': '.media/x/y/n',
                                         'digest': 'sha256:5ff7447b29b2090f495f637430819a9c25a0a5e07411e99545c0a680adc8e101:5',
                                         'owner_document_id': 'x/n.md',
                                         'source_media_dir': '.media/x/n'},
                                        {'asset_id': 'a2',
                                         'asset_name': 'q.png',
                                         'destination_media_dir': '.media/x/y/n',
                                         'digest': 'sha256:62ca924812f8267bde71961834dfd2e76329e5d600c07c038c8f56aa55768e7c:5',
                                         'owner_document_id': 'x/n.md',
                                         'source_media_dir': '.media/x/n'}],
                        'conflicts': [],
                        'destination_document_path': 'x/y/n.md',
                        'destination_layout_version': 2,
                        'destination_media_dir': '.media/x/y/n',
                        'document_id': 'x/n.md',
                        'expected_revision': 'r1',
                        'operation_id': 'op-2',
                        'owner_subject_id': 'u',
                        'phase': 'staged',
                        'preimages': {'x/n.md': '![p](../.media/x/n/p.png) ![q](<../.media/x/n/q.png>) '
                                                '[[../.media/x/n/p.png]] ![e](https://e.com/a.png) '
                                                '![l](keep.png)'},
                        'reference_edits': [{'document_id': 'x/n.md',
                                             'end': 118,
                                             'new_target': '../keep.png',
                                             'new_text': '![l](<../keep.png>)',
                                             'old_target': 'keep.png',
                                             'start': 104},
                                            {'document_id': 'x/n.md',
                                             'end': 77,
                                             'new_target': '../../.media/x/y/n/p.png',
                                             'new_text': '[[../../.media/x/y/n/p.png]]',
                                             'old_target': '../.media/x/n/p.png',
                                             'start': 54},
                                            {'document_id': 'x/n.md',
                                             'end': 53,
                                             'new_target': '../../.media/x/y/n/q.png',
                                             'new_text': '![q](<../../.media/x/y/n/q.png>)',
                                             'old_target': '../.media/x/n/q.png',
                                             'start': 26},
                                            {'document_id': 'x/n.md',
                                             'end': 25,
                                             'new_target': '../../.media/x/y/n/p.png',
                                             'new_text': '![p](<../../.media/x/y/n/p.png>)',
                                             'old_target': '../.media/x/n/p.png',
                                             'start': 0}],
                        'resource_receipt': None,
                        'source_document_path': 'x/n.md',
                        'source_layout_version': 2,
                        'source_media_dir': '.media/x/n'},
 'workspace_preserve_media': {'asset_moves': [],
                              'conflicts': [],
                              'destination_document_path': 'c.md',
                              'destination_layout_version': 2,
                              'destination_media_dir': '.media/a',
                              'document_id': 'a.md',
                              'expected_revision': 'r1',
                              'operation_id': 'op-5',
                              'owner_subject_id': 'u',
                              'phase': 'staged',
                              'preimages': {'a.md': '![i](.media/a/i.png)'},
                              'reference_edits': [{'document_id': 'a.md',
                                                   'end': 20,
                                                   'new_target': '.media/a/i.png',
                                                   'new_text': '![i](<.media/a/i.png>)',
                                                   'old_target': '.media/a/i.png',
                                                   'start': 0}],
                              'resource_receipt': None,
                              'source_document_path': 'a.md',
                              'source_layout_version': 2,
                              'source_media_dir': '.media/a'},
 'workspace_rename': {'asset_moves': [{'asset_id': 'a1',
                                       'asset_name': 'i.png',
                                       'destination_media_dir': '.media/b',
                                       'digest': 'sha256:0a7b7703c93599170ade5b1451d7d1e4836bc1e51ec55aa40e99b4488ed25018:5',
                                       'owner_document_id': 'a.md',
                                       'source_media_dir': '.media/a'}],
                      'conflicts': [],
                      'destination_document_path': 'b.md',
                      'destination_layout_version': 2,
                      'destination_media_dir': '.media/b',
                      'document_id': 'a.md',
                      'expected_revision': 'r1',
                      'operation_id': 'op-1',
                      'owner_subject_id': 'u',
                      'phase': 'staged',
                      'preimages': {'a.md': '![i](.media/a/i.png) [self](a.md)', 'o.md': '[a](a.md)'},
                      'reference_edits': [{'document_id': 'a.md',
                                           'end': 33,
                                           'new_target': 'b.md',
                                           'new_text': '[self](<b.md>)',
                                           'old_target': 'a.md',
                                           'start': 21},
                                          {'document_id': 'a.md',
                                           'end': 20,
                                           'new_target': '.media/b/i.png',
                                           'new_text': '![i](<.media/b/i.png>)',
                                           'old_target': '.media/a/i.png',
                                           'start': 0},
                                          {'document_id': 'o.md',
                                           'end': 9,
                                           'new_target': 'b.md',
                                           'new_text': '[a](<b.md>)',
                                           'old_target': 'a.md',
                                           'start': 0}],
                      'resource_receipt': None,
                      'source_document_path': 'a.md',
                      'source_layout_version': 2,
                      'source_media_dir': '.media/a'},
 'workspace_v1_to_v2': {'asset_moves': [{'asset_id': 'a1',
                                         'asset_name': 'i.png',
                                         'destination_media_dir': '.media/d/a',
                                         'digest': 'sha256:0a7b7703c93599170ade5b1451d7d1e4836bc1e51ec55aa40e99b4488ed25018:5',
                                         'owner_document_id': 'd/a.md',
                                         'source_media_dir': 'media/d/a'}],
                        'conflicts': [],
                        'destination_document_path': 'd/a.md',
                        'destination_layout_version': 2,
                        'destination_media_dir': '.media/d/a',
                        'document_id': 'd/a.md',
                        'expected_revision': 'r1',
                        'operation_id': 'op-4',
                        'owner_subject_id': 'u',
                        'phase': 'staged',
                        'preimages': {'d/a.md': '![i](../media/d/a/i.png)'},
                        'reference_edits': [{'document_id': 'd/a.md',
                                             'end': 24,
                                             'new_target': '../.media/d/a/i.png',
                                             'new_text': '![i](<../.media/d/a/i.png>)',
                                             'old_target': '../media/d/a/i.png',
                                             'start': 0}],
                        'resource_receipt': None,
                        'source_document_path': 'd/a.md',
                        'source_layout_version': 1,
                        'source_media_dir': 'media/d/a'}}


def _plan(**kw):
    return plan_document_move(**kw)


def test_same_frame_plans_equal_frozen_unmodified_output():
    for name, kw in identity_cases().items():
        plan = _plan(**kw)
        assert plan.as_receipt() == _FROZEN[name], name
        assert plan.source_frame is None and plan.destination_frame is None
        assert "source_frame" not in plan.as_receipt()


def test_same_origin_frame_with_layout_change_is_the_same_frame_path():
    kw = identity_cases()["workspace_v1_to_v2"]
    kw = {**kw}
    kw.pop("destination_layout_version")
    frame = MoveFrame("workspace", "/r", 2)
    assert _plan(**kw, destination_frame=frame).as_receipt() == _FROZEN["workspace_v1_to_v2"]


_ROOTS = {"workspace": "/w", "selected_root": "/s", "loose": "/l/dir"}
_VERSION = {"workspace": 2, "selected_root": 2, "loose": 1}


def _cross(src, dst, *, src_root=None, dst_root=None, text="![i](%s) ![j](%s)", assets=("i.png", "j.png"),
           source_doc=None, dest_doc=None, other_docs=(), op="x"):
    src_root = src_root or _ROOTS[src]
    dst_root = dst_root or ("/other" + _ROOTS[dst] if dst_root is None and dst == src else _ROOTS[dst])
    source_doc = source_doc or ("note.md" if src == "loose" else "d/note.md")
    dest_doc = dest_doc or ("moved.md" if dst == "loose" else "e/moved.md")
    layout = document_media_dir(document_path=source_doc if src != "loose" else "note.md", canonical_root=src_root,
                                origin=src, layout_version=_VERSION[src])
    media = layout.media_dir if src != "loose" else "media/note"
    doc_dir = os.path.dirname(source_doc)
    rel = lambda name: os.path.relpath(media + "/" + name, doc_dir or ".")
    body = text
    for k in range(text.count("%s")):
        body = body.replace("%s", rel(assets[k % len(assets)]), 1)
    refs = [(source_doc, body, False, source_doc), *other_docs]
    kw = dict(operation_id=op, document_id=source_doc, source_document_path=source_doc,
              destination_document_path=dest_doc, canonical_root=src_root, origin=src, owner_subject_id="u",
              source_layout_version=_VERSION[src],
              preflight=_pf([_asset(n, source_doc, media, i) for i, n in enumerate(assets)]),
              referencing_documents=refs,
              destination_frame=MoveFrame(dst, dst_root, _VERSION[dst]))
    return kw, _plan(**kw), dst_root, src_root


def _resolve_from_destination(plan, dst_root, dest_doc):
    edited = [e for e in plan.reference_edits]
    out = []
    for e in edited:
        out.append(os.path.normpath(os.path.join(dst_root, os.path.dirname(dest_doc), e.new_target)))
    return out


PAIRS = [("workspace", "selected_root"), ("selected_root", "workspace"), ("selected_root", "selected_root"),
         ("loose", "workspace"), ("workspace", "loose"), ("loose", "selected_root"), ("workspace", "workspace")]


def test_every_origin_pair_plans_destination_layout_and_resolving_references():
    for src, dst in PAIRS:
        kw, plan, dst_root, src_root = _cross(src, dst)
        assert plan.conflicts == () and plan.phase == MovePhase.STAGED, (src, dst, plan.conflicts)
        assert plan.destination_layout_version == _VERSION[dst]
        assert plan.source_frame["origin"] == src and plan.destination_frame["origin"] == dst
        assert plan.destination_frame["layout_version"] == _VERSION[dst]
        dest_doc = kw["destination_document_path"]
        expected = document_media_dir(document_path=os.path.basename(dest_doc) if dst == "loose" else dest_doc,
                                      canonical_root=dst_root, origin=dst, layout_version=_VERSION[dst]).media_dir
        if dst == "loose":
            expected = "media/moved"
        assert plan.destination_media_dir == expected
        assert {m.destination_media_dir for m in plan.asset_moves} == {expected}
        assert len(plan.asset_moves) == 2
        want = {os.path.normpath(os.path.join(dst_root, expected, m.asset_name)) for m in plan.asset_moves}
        assert set(_resolve_from_destination(plan, dst_root, dest_doc)) == want
        for m in plan.asset_moves:
            absolute_dest = os.path.join(dst_root, m.destination_media_dir, m.asset_name)
            absolute_src = os.path.join(src_root, m.source_media_dir, m.asset_name)
            assert not absolute_dest.startswith(src_root.rstrip("/") + "/")
            assert not absolute_src.startswith(dst_root.rstrip("/") + "/")
        # The destination document location is in the destination frame only.
        assert plan.destination_document_path == dest_doc
        assert plan.as_receipt()["destination_frame"]["canonical_root"] == dst_root


def test_asset_referenced_twice_each_site_is_rewritten():
    kw, plan, dst_root, _ = _cross("workspace", "selected_root", text="![a](%s) ![b](%s)", assets=("i.png", "i.png"))
    assert plan.conflicts == ()
    assert len(plan.asset_moves) == 1 and len(plan.reference_edits) == 2
    assert len({e.new_target for e in plan.reference_edits}) == 1


def test_escaped_and_spaced_and_unicode_names_follow_the_asset():
    for name, escaped in (("my pic.png", "my%20pic.png"), ("café 图.png", "caf%C3%A9%20%E5%9B%BE.png")):
        for src, dst in (("workspace", "selected_root"), ("loose", "workspace")):
            kw, plan, dst_root, _ = _cross(src, dst, text="![raw](<%s>) ![esc](" + "__ESC__" + ")", assets=(name,))
            # Rebuild the text with both a raw and an escaped spelling.
            doc = kw["source_document_path"]
            media = kw["preflight"].assets[0]["media_dir"]
            doc_dir = os.path.dirname(doc)
            raw = os.path.relpath(media + "/" + name, doc_dir or ".")
            esc = os.path.relpath(media + "/" + escaped, doc_dir or ".")
            kw["referencing_documents"] = [(doc, f"![raw](<{raw}>) ![esc]({esc})", False, doc)]
            plan = _plan(**kw)
            assert plan.conflicts == (), (name, plan.conflicts)
            assert len(plan.reference_edits) == 2
            esc_edit = next(e for e in plan.reference_edits if e.site.target == esc)
            raw_edit = next(e for e in plan.reference_edits if e.site.target == raw)
            dest_dir = os.path.dirname(kw["destination_document_path"])
            from urllib.parse import unquote
            for edit in (raw_edit, esc_edit):
                target = os.path.normpath(os.path.join(dst_root, dest_dir, unquote(edit.new_target) if edit is esc_edit else edit.new_target))
                assert target == os.path.normpath(os.path.join(dst_root, plan.destination_media_dir, name))
            assert "%" in esc_edit.new_target and "%" not in raw_edit.new_target


def test_external_urls_and_fragments_are_untouched():
    kw, plan, _, _ = _cross("workspace", "loose", text="![e](https://e.com/a.png) [t](#top) ![i](%s)", assets=("i.png",))
    assert plan.conflicts == () and len(plan.reference_edits) == 1
    assert plan.reference_edits[0].site.target.endswith("i.png")


def test_same_frame_rule_for_non_asset_local_reference_is_unchanged():
    kw = identity_cases()["selected_subfolder"]
    plan = _plan(**kw)
    keep = [e for e in plan.reference_edits if e.site.target == "keep.png"]
    assert plan.conflicts == () and len(keep) == 1 and keep[0].new_target == "../keep.png"


def test_cross_frame_refuses_everything_that_cannot_be_exact_and_plans_nothing():
    def refused(code, **change):
        kw, plan, _, _ = _cross("workspace", "selected_root", **change)
        assert plan.phase == MovePhase.ABORTED, code
        assert [c.code for c in plan.conflicts] == [code], (code, plan.conflicts)
        assert plan.asset_moves == () and plan.reference_edits == () and plan.preimages == {}
        assert plan.source_frame and plan.destination_frame
        return kw

    refused("unmanaged_local_reference", text="![i](%s) ![x](other.png)", assets=("i.png",))
    refused("unresolvable_reference", text="![i](%s) ![x](/abs/other.png)", assets=("i.png",))
    refused("reference_outside_move", other_docs=[("p.md", "![i](" + document_media_dir(
        document_path="d/note.md", canonical_root="/w", origin="workspace", layout_version=2).media_dir + "/i.png)", False, "p.md")], assets=("i.png",))
    refused("reference_outside_move", other_docs=[("p.md", "[n](d/note.md)", False, "p.md")], assets=("i.png",))
    kw, _, _, _ = _cross("workspace", "selected_root")
    # media cannot stay behind
    plan = _plan(**{**kw, "preserve_media_location": True})
    assert [c.code for c in plan.conflicts] == ["media_location_not_preservable"] and plan.asset_moves == ()
    # a destination root that cannot hold the document (loose with v2)
    plan = _plan(**{**kw, "destination_frame": MoveFrame("loose", "/l", 2)})
    assert [c.code for c in plan.conflicts] == ["invalid_destination_frame"] and plan.asset_moves == ()
    # destination name collision between two assets of one document
    kw2, plan, _, _ = _cross("workspace", "selected_root")
    kw2["preflight"] = _pf([_asset("i.png", "d/note.md", ".media/d/note", 1), _asset("i.png", "d/note.md", ".media/d/other", 2)])
    plan = _plan(**kw2)
    assert "asset_collision" in [c.code for c in plan.conflicts] and plan.asset_moves == ()
    # foreign asset
    kw2["preflight"] = _pf([_asset("i.png", "z.md", ".media/d/note")])
    plan = _plan(**kw2)
    assert "foreign_asset" in [c.code for c in plan.conflicts] and plan.asset_moves == () and plan.reference_edits == ()
    # protected reference in the moved document
    kw3, _, _, _ = _cross("workspace", "selected_root")
    kw3["referencing_documents"] = [("d/note.md", kw3["referencing_documents"][0][1], True, "d/note.md")]
    plan = _plan(**kw3)
    assert {c.code for c in plan.conflicts} == {"protected_reference"} and plan.reference_edits == ()
    # stale revision
    plan = _plan(**{**kw, "preflight": MovePreflight(expected_revision="a", current_revision="b")})
    assert plan.phase == MovePhase.ABORTED and plan.conflicts[0].code == "stale_revision"


def test_cross_frame_plan_is_deterministic_and_round_trips_its_receipt():
    for src, dst in PAIRS:
        a = _cross(src, dst)[1]
        b = _cross(src, dst)[1]
        assert a.as_receipt() == b.as_receipt()
        assert MovePlan.from_receipt(a.as_receipt()).as_receipt() == a.as_receipt()
