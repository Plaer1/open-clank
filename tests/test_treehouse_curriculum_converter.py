"""Conversion risk checks: bounded paths, complete work and semantic revisions."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("curriculum_converter", ROOT / "scripts/build_treehouse_curriculum.py")
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def test_embedded_fence_is_not_a_manifest_boundary(tmp_path):
    path = tmp_path / "manifest.md"
    path.write_text('```json\n' + json.dumps({"schemaVersion": 1, "body": "```python\n1\n```"}) + '\n```')
    assert converter.author_manifest(path)["body"] == "```python\n1\n```"


def test_containment_rejects_external_bodies(tmp_path):
    with pytest.raises(ValueError, match="contained"):
        converter.safe_body(tmp_path, "../external.md")


def test_requirement_identity_changes_only_by_reviewed_semantics():
    assignment = {"id": "cs:values:assessment:x", "unitId": "cs:values:x", "completionEvidence": "response", "requirementRevision": 1, "prompt": "First wording"}
    initial = converter.requirement_digest(assignment)
    assignment["prompt"] = "Typographic correction"
    assert converter.requirement_digest(assignment) == initial
    assignment["requirementRevision"] = 2
    assert converter.requirement_digest(assignment) != initial
    assignment["requirementRevision"] = 1
    assignment["completionEvidence"] = "observed-success"
    assert converter.requirement_digest(assignment) != initial


def test_published_work_is_complete_and_has_no_click_credit():
    from src.openclank.treehouse_global_catalogue import GlobalCatalogue
    package = ROOT / "src/openclank/treehouse_curriculum"
    manifest = json.loads((package / "manifest.json").read_text())
    catalogue = GlobalCatalogue(package)
    roots = set(manifest['mainQuests'])
    expected = {3: (228, 231), 4: (291, 297)}
    assert roots == ({'treehouse:programming','treehouse:games','treehouse:ai'} if len(roots) == 3 else {'treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'})
    assert len(manifest['subjects']) == 49
    assert len(manifest['units']) == expected[len(roots)][0]
    work = {}
    for meta in manifest["subjects"].values():
        state = catalogue.subject_state(meta['id'])
        for unit in state["activities"].values():
            assert unit["points"] == 0 and not unit["requiredWork"]
            assert unit["content"].strip()
            assert unit['assignmentIds'] or (unit.get('readingOnly') is True and unit['id'] not in meta['completionCriteria']['activityIds'])
            assert all(key in state["assignments"] for key in unit["assignmentIds"])
        assignments = state["assignments"]
        assert set(meta["completionCriteria"]["assignmentIds"]) <= set(assignments)
        assert meta["requirementsDigest"] == converter.requirements_digest(meta["completionCriteria"], assignments)
        for item in assignments.values():
            assert item["requirementDigest"] == converter.requirement_digest(item)
            assert item["assessmentType"] == "text" and not item["graded"]
            assert item["prompt"].strip() and item["successConditions"]
            assert "tool-recipes.md" not in item["prompt"]
            work[item["id"]] = item
        raw = (package / meta["definitionPath"]).read_text()
        assert ".clanker/" not in raw and ".references/" not in raw
    assert len(work) == sum(len(catalogue.subject_state(meta['id'])['assignments']) for meta in manifest['subjects'].values())

    assert len(work) == expected[len(roots)][1]
