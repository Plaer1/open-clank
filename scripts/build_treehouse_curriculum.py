#!/usr/bin/env python3
"""Manually import reviewed author manifests into self-contained native data.

Inputs are explicit CLI paths. This is never a runtime importer or startup hook.
Output is deterministic; semantics change only with reviewed requirement revisions.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from urllib.parse import quote
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.openclank.treehouse_curriculum_validation import (
    ROOT_IDS, validate_fragments, validate_public_strings, validate_resources, validate_sources,
)
from src.openclank.treehouse_course_files import split_course_definition
SOURCE_ID = "openclank:treehouse-core"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def author_manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    start = text.find("```json")
    if start < 0:
        raise ValueError(f"Missing manifest JSON fence: {path.name}")
    value, _ = json.JSONDecoder().raw_decode(text[start + 7:].lstrip())
    if not isinstance(value, dict) or value.get("schemaVersion") not in {1, "treehouse-author-handoff-v1"}:
        raise ValueError("Unsupported author manifest")
    return value


def safe_body(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix != ".md" or path.parent.name != "units":
        raise ValueError("Body must be a contained learner unit Markdown file")
    return path.read_text(encoding="utf-8")


def source_records(root: Path, lane: str, records: list) -> dict:
    # CS has key/registerPath rows; Games/AI have source id/title/url rows.
    table = {}
    register = root / "sources.md"
    if register.is_file():
        for line in register.read_text(encoding="utf-8").splitlines():
            columns = [x.strip() for x in line.split("|")[1:-1]]
            if len(columns) >= 4:
                link = re.search(r"\[([^]]+)\]\((https?://[^)]+)\)", columns[1])
                if link:
                    table[columns[0]] = {"title": link[1], "url": link[2], "supportedClaim": columns[2], "version": columns[3]}
    result = {}
    for record in records:
        key = record.get("id") or record.get("key")
        if not key:
            raise ValueError("Source lacks ID")
        normalized = {k: copy.deepcopy(v) for k, v in record.items() if k not in {"key", "registerPath", "usedBy"}}
        normalized.update(table.get(key, {}))
        normalized["id"] = key if ':' in key else f"{lane}:{key}"
        if key.rsplit(':', 1)[-1] == "LOCAL" or record.get('kind') == 'first-party-inspection':
            # Precise private audit pointers stay in author notes, never publication.
            normalized.pop("path", None)
            normalized.pop("companionPath", None)
            code_paths = ["src/openclank/copal_treehouse.py", "src/openclank/treehouse_global_catalogue.py",
                          "routes/copal_treehouse_routes.py", "static/js/copal/treehouse.js",
                          "static/js/copal/treehouseTopicWeb.js"]
            normalized["codePaths"] = code_paths
            normalized["candidateIdentity"] = {"baseCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "sourceDigests": {name: "sha256:" + hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in code_paths}}
            normalized["provenanceLimit"] = "First-party source inspection of this candidate; capabilities still depend on configuration and actual runtime availability. These code references are provenance, not runtime imports or a deployment claim."
        for name, value in normalized.items():
            if name.lower().endswith("path") and isinstance(value, str) and (Path(value).is_absolute() or ".." in Path(value).parts):
                raise ValueError("Source provenance cannot retain external or parent-relative author paths")
        if any(token in json.dumps(normalized) for token in [".clanker/", ".references/"]):
            raise ValueError("Source provenance cannot retain private paths")
        validate_public_strings(normalized)
        if normalized['id'] in result and result[normalized['id']] != normalized:
            raise ValueError('Conflicting source identity: ' + normalized['id'])
        result[normalized["id"]] = normalized
    return result


def requirement_digest(assignment: dict) -> str:
    return digest({"assignmentId": assignment["id"], "unitId": assignment["unitId"],
                   "completionEvidence": assignment["completionEvidence"],
                   "requirementRevision": assignment["requirementRevision"]})


def requirements_digest(criteria: dict, assignments: dict) -> str:
    canonical_criteria = {"activityIds": sorted(criteria.get("activityIds", [])),
                          "assignmentIds": sorted(criteria.get("assignmentIds", [])),
                          "assignmentMode": criteria["assignmentMode"]}
    revisions = [{"id": key, "requirementRevision": assignments[key]["requirementRevision"],
                  "completionEvidence": assignments[key]["completionEvidence"]}
                 for key in canonical_criteria["assignmentIds"]]
    return digest({"completionCriteria": canonical_criteria, "requirementRevisions": revisions})


def convert(inputs: list[Path], revision: str, assets: list | None = None, resources: list | None = None, *, course_files: bool = False) -> tuple[dict, dict[str, bytes]]:
    manifest = {"format": "openclank-treehouse-global-v1", "schemaVersion": 1,
                "sourceId": SOURCE_ID, "packageRevision": revision,
                "mainQuests": {}, "subjects": {}, "units": {}, "topicLinks": [], "sources": {}, "assets": assets or [], "resources": resources or []}
    files = {}
    seen = set()
    for path in inputs:
        author = author_manifest(path)
        main = author["mainQuest"]
        root_id = main["id"]
        lane = {"treehouse:programming": "cs", "treehouse:games": "games", "treehouse:ai": "ai", "treehouse:openclank": "openclank"}[root_id]
        if root_id in manifest["mainQuests"]:
            raise ValueError("Duplicate root")
        manifest["mainQuests"][root_id] = copy.deepcopy(main)
        for key, record in source_records(path.parent, lane, author.get("sources", [])).items():
            if key in manifest['sources'] and manifest['sources'][key] != record:
                raise ValueError('Conflicting source identity: ' + key)
            manifest['sources'][key] = record
        manifest["topicLinks"].extend(copy.deepcopy(author["topicLinks"]))
        units = {unit["id"]: unit for unit in author["units"]}
        if len(units) != len(author['units']):
            raise ValueError('Duplicate author unit')
        body_targets = {(path.parent / unit["bodyPath"]).resolve(): unit["id"] for unit in author["units"]}
        def published_markdown(text: str, body_path: Path) -> str:
            def link(match):
                target = match[2]
                if target.startswith(("https:", "http:", "#", "mailto:")):
                    return match[0]
                resolved = (body_path.parent / target.split("#")[0]).resolve()
                if resolved not in body_targets:
                    raise ValueError(f"Unresolved learner link: {target}")
                return f"[{match[1]}](#treehouse-unit={quote(body_targets[resolved], safe='')})"
            return re.sub(r"\[([^]]+)\]\(([^)]+)\)", link, text)
        ownership = {unit["id"]: unit["subjectId"] for unit in author["units"]}
        author_assignments = {}
        for item in author["assignments"]:
            if item['id'] in author_assignments:
                raise ValueError('Duplicate author assignment')
            assignment = copy.deepcopy(item)
            assignment["subjectId"] = ownership[assignment["unitId"]]
            author_assignments[assignment["id"]] = assignment
        for subject in author["subjects"]:
            sid = subject["id"]
            module_id = sid + ":group:units"
            source_ref = {"kind": "system", "sourceId": SOURCE_ID, "courseId": sid}
            state = {name: {} for name in ["courses", "modules", "activities", "assignments", "skills", "badges", "quests"]}
            course = {k: copy.deepcopy(subject[k]) for k in ["id", "title", "objective", "courseType", "learningContractVersion", "curriculumRevision", "completionCriteria", "freeExploration"]}
            course.update({"description": subject["objective"], "status": "published", "learningWebVersion": 1,
                           "moduleIds": [module_id], "prerequisites": [], "tags": [lane], "sourceRef": source_ref})
            state["courses"][sid] = course
            ordered_units = subject["units"]
            assignment_ids = []
            for unit_id in ordered_units:
                unit = units[unit_id]
                aids = unit.get("assignmentIds") or ([unit["assignmentId"]] if unit.get("assignmentId") else [])
                if not aids and (unit.get('readingOnly') is not True or unit_id in course['completionCriteria'].get('activityIds', [])):
                    raise ValueError('Assignment-free unit must be explicitly readingOnly, without completion credit')
                assignment_ids.extend(aids)
                source_keys = [key if ':' in key else f"{lane}:{key}" for key in unit.get("sourceKeys", unit.get("sourceIds", []))]
                activity = {"id": unit_id, "courseId": sid, "moduleId": module_id, "title": unit["title"],
                            "activityType": "lesson", "status": "published", "contentFormat": "markdown",
                            "content": published_markdown(safe_body(path.parent, unit["bodyPath"]), path.parent / unit["bodyPath"]), "outcome": unit["outcome"],
                            "optionalSections": copy.deepcopy(unit.get("optionalSections", [])),
                            "assignmentIds": aids, "sourceKeys": source_keys, "requiredWork": False,
                            "points": 0, "skillIds": [], "freeExploration": True,
                            "assetIds": [asset["id"] for asset in manifest["assets"] if unit_id in asset.get("unitIds", [])],
                            "resourceIds": [record['id'] for record in manifest['resources'] if unit_id in record.get('unitIds', [])],
                            "readingOnly": not aids}
                state["activities"][unit_id] = activity
                manifest["units"][unit_id] = {k: copy.deepcopy(activity[k]) for k in ["id", "courseId", "title", "outcome", "assignmentIds", "sourceKeys", "assetIds", "resourceIds", "readingOnly"]}
                for aid in aids:
                    item = author_assignments[aid]
                    conditions = item["successConditions"]
                    if isinstance(conditions, str): conditions = [conditions]
                    if not isinstance(conditions, list) or not conditions or not all(isinstance(s, str) and s.strip() for s in conditions):
                        raise ValueError("Invalid success conditions")
                    prompt = item["prompt"]
                    # Approved publication correction: recipe is embedded in the unit.
                    if aid in {"cs:systems:assessment:compile-inspect-run", "cs:rust:assessment:run-rust-tests"}:
                        prompt = prompt.replace("`tool-recipes.md`", "the recipe in this unit").replace("tool-recipes.md", "the recipe in this unit")
                    native = {"id": aid, "unitId": unit_id, "courseId": sid, "moduleId": module_id,
                              "title": item.get("title", unit["title"] + " — response"), "prompt": prompt,
                              "description": prompt, "successConditions": conditions, "rubric": [],
                              "action": item.get("action", "Submit a response with reasoning or the requested evidence."),
                              "failureFeedback": item["failureFeedback"], "hint": item.get("hint", ""),
                              "evidenceType": item["evidenceType"], "verificationLimit": item["verificationLimit"],
                              "completionEvidence": item.get("completionEvidence", "response"),
                              "promptInBody": item.get('promptInBody', lane in {"cs", "games"}),
                              "requirementRevision": item.get("requirementRevision", 1),
                              "status": "published", "assessmentType": "text", "graded": False,
                              "allowRetries": True, "maxAttempts": 0, "maxPoints": 0, "skillIds": [],
                              "submissionContract": copy.deepcopy(item.get("submissionContract", {"responseRequired": True, "semanticValidation": "none", "selfReport": True}))}
                    if type(native["requirementRevision"]) is not int or native["requirementRevision"] < 1:
                        raise ValueError("Invalid requirement revision")
                    native["requirementDigest"] = requirement_digest(native)
                    if type(native['promptInBody']) is not bool:
                        raise ValueError('promptInBody must be explicit boolean')
                    state["assignments"][aid] = native
            state["modules"][module_id] = {"id": module_id, "courseId": sid, "title": "Units", "description": "",
                                           "activityIds": ordered_units, "assignmentIds": assignment_ids, "order": 0}
            course["requirementsDigest"] = requirements_digest(course["completionCriteria"], state["assignments"])
            definition_path = "subjects/" + sid.replace(":", "-") + ".json"
            raw = canonical_bytes(state)
            files[definition_path] = raw
            metadata = {k: copy.deepcopy(course[k]) for k in ["id", "title", "objective", "curriculumRevision", "completionCriteria", "requirementsDigest"]}
            metadata.update({"mainQuestId": root_id, "definitionPath": definition_path,
                             "definitionDigest": "sha256:" + hashlib.sha256(raw).hexdigest(), "unitIds": ordered_units})
            manifest["subjects"][sid] = metadata
            for collection in state.values():
                for identity in collection:
                    if identity in seen: raise ValueError("Duplicate native identity")
                    seen.add(identity)
    if set(manifest["mainQuests"]) != ROOT_IDS:
        raise ValueError("Exactly four curriculum roots required")
    edges = {}
    for edge in manifest['topicLinks']:
        key = (edge['fromId'], edge['toId'], edge['type'])
        if key in edges and edges[key] != edge:
            raise ValueError('Conflicting topic edge: ' + repr(key))
        edges[key] = edge
    manifest['topicLinks'] = list(edges.values())
    validate_sources(manifest['sources'], manifest['units'])
    validate_public_strings(manifest)
    for raw in files.values():
        definition = json.loads(raw)
        validate_public_strings(definition)
        validate_fragments(definition, manifest['units'])
    validate_resources(manifest, ROOT)
    if course_files:
        externalize_course_files(manifest, files)
    manifest["packageDigest"] = digest(manifest)
    files["manifest.json"] = canonical_bytes(manifest)
    return manifest, files


def externalize_course_files(manifest: dict, files: dict[str, bytes]) -> None:
    """Change representation only; hydrate back to the exact native definitions."""
    manifest['contentFormat'] = 'course-files-v1'
    for metadata in manifest['subjects'].values():
        path = metadata['definitionPath']
        definition, text_files = split_course_definition(json.loads(files[path]))
        if set(text_files) & set(files):
            raise ValueError('Duplicate course source path')
        files.update(text_files)
        files[path] = canonical_bytes(definition)
        metadata['definitionDigest'] = 'sha256:' + hashlib.sha256(files[path]).hexdigest()


def write_reviewed(path: Path, raw: bytes) -> None:
    if path.is_file() and path.read_bytes() == raw:
        return
    subprocess.run([str(ROOT / "scripts/openclank"), "hex", "explain", str(path)], cwd=ROOT, check=True, capture_output=True)
    if path.exists():
        blob = subprocess.check_output(["git", "hash-object", "-w", str(path)], cwd=ROOT, text=True).strip()
        key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        subprocess.run(["git", "update-ref", f"refs/private/treehouse-content-preimages/{key}/{blob}", blob], cwd=ROOT, check=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-revision", required=True)
    parser.add_argument("--asset-register", type=Path)
    parser.add_argument("--resource-register", type=Path)
    parser.add_argument("--apply", action="store_true", help="Explicitly write reviewed output, preserving preimages")
    parser.add_argument('--course-files', action='store_true', help='Publish metadata plus ordinary Markdown/work source files')
    args = parser.parse_args()
    assets = json.loads(args.asset_register.read_text()) if args.asset_register else []
    resources = json.loads(args.resource_register.read_text()) if args.resource_register else []
    manifest, files = convert(args.input, args.package_revision, assets, resources, course_files=args.course_files)
    if args.apply:
        for relative, raw in files.items(): write_reviewed(args.output / relative, raw)
    print(json.dumps({"written": args.apply, "roots": len(manifest["mainQuests"]), "subjects": len(manifest["subjects"]),
                      "units": len(manifest["units"]), "links": len(manifest["topicLinks"]), "packageDigest": manifest["packageDigest"]}))


if __name__ == "__main__":
    main()
