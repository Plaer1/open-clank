"""Bounded, deterministic S07 selection over already-authorized recall hits.

This pure selector is not a source-liveness or privacy authority. The caller
must freshly authorize the scope and every exact revision before delivery, and
must bind the final delivered payload separately. No prompt or result is stored.
"""
from dataclasses import asdict, dataclass
import json
import re
import unicodedata
from typing import Literal

from src.memory_provider import MemoryRevisionRef, RecallInvocation

POLICY_VERSION = "s07.lexical.v1"
_STOP = frozenset("the and for are was were with that this from have has had into your you our not but can will would could should what when where which who how why about their they them its all any use using please".split())
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


ROUTE_KIND = "curated_fts"
FTS_ORDINAL_KEY = "_fts_ordinal"  # private carrier on a hit copy; popped before anything else sees the hit


def pop_fts_ordinal(hit):
    """Remove and return the pre-qualification FTS position carried on a hit copy, else None."""
    value = hit.pop(FTS_ORDINAL_KEY, None) if type(hit) is dict else None
    return value if type(value) is int and value >= 0 else None


def s07_features(ordinals, selected):
    """found.features v1 per delivered curated id; numbers and route label only, unknowns None."""
    return {cid: {"v": 1, "fts_ordinal": ordinals[cid], "route_kind": ROUTE_KIND, "directness": None,
                  "entity_match": None, "freshness": None}
            for cid, _ in selected if ordinals.get(cid) is not None}


_FEATURE_KEYS = frozenset(("v", "fts_ordinal", "route_kind", "directness", "entity_match", "freshness"))


def found_features_valid(f):
    """Closed found.features v1 check (mirrors the Native bounds): numbers, bools and a route label only."""
    unit = lambda x: x is None or (type(x) in (int, float) and 0 <= x <= 1)
    return (type(f) is dict and set(f) == _FEATURE_KEYS and type(f["v"]) is int and f["v"] == 1
            and type(f["fts_ordinal"]) is int and 0 <= f["fts_ordinal"] <= 19
            and type(f["route_kind"]) is str and re.fullmatch(r"[a-z0-9_]{1,32}", f["route_kind"]) is not None
            and unit(f["directness"]) and unit(f["freshness"])
            and (f["entity_match"] is None or type(f["entity_match"]) is bool))


def found_features_for(send, features, cid):
    """The features to attach to one delivered item, or None. `send` is the once-per-attempt decision."""
    return features.get(cid) if send and type(features) is dict else None


def found_request_body(request):
    """Wire body of a found write. An absent `features` is dropped: an older Native rejects even null."""
    body = asdict(request)
    if body.get("features") is None:
        body.pop("features", None)
    return body


def found_features_rows(owner, items):
    """Validated page items -> (receipts, labels) rows for memery_ranking_feedback.shadow_report."""
    receipts = [{**i["features"], "owner": owner, "found_event_id": i["found_event_id"],
                 "recall_attempt_id": i["recall_attempt_id"]} for i in items]
    labels = [{"owner": owner, "found_event_id": i["found_event_id"], "polarity": p}
              for i in items for p in ("positive", "negative") if i["recorded"][p]]
    return receipts, labels


@dataclass(frozen=True)
class Candidate:
    revision: MemoryRevisionRef
    text: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class S07ContextEvidence:
    """Host-held source and exact selected revisions; never model-visible."""
    owner: str
    account_id: str
    source: object
    target: object
    invocation: RecallInvocation
    selected: tuple[tuple[str, MemoryRevisionRef], ...]
    features: dict | None = None  # curated_id -> found.features v1, delivered ids only

@dataclass(frozen=True)
class Selection:
    policy: str
    candidates: tuple[Candidate, ...]


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _terms(value: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(_normalized(value)) if len(t) >= 3 and t not in _STOP)


def context_fragments(snapshot: str) -> tuple[str, ...]:
    """Decode the bounded Native text snapshot; malformed input is unavailable."""
    if type(snapshot) is not str or len(snapshot.encode("utf-8")) > 16000:
        raise ValueError("context snapshot unavailable")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("context snapshot unavailable")
            result[key] = value
        return result
    try:
        items = json.loads(snapshot, object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        raise ValueError("context snapshot unavailable") from exc
    if (type(items) is not list or not 1 <= len(items) <= 32
        or any(type(item) is not dict or set(item) != {"role", "text"}
            or item["role"] not in ("user", "assistant") or type(item["text"]) is not str for item in items)
        or sum(len(item["text"].encode("utf-8")) for item in items) > 16000):
        raise ValueError("context snapshot unavailable")
    return tuple(item["text"] for item in items)


def _word_character(char: str) -> bool:
    return unicodedata.category(char)[0] in ("L", "N", "M") or unicodedata.category(char) == "Pc"


def _contains_complete(needle: str, text: str) -> bool:
    if len(needle) < 24 or len(_terms(needle)) < 4:
        return False
    position = text.find(needle)
    while position >= 0:
        end = position + len(needle)
        if (position == 0 or not _word_character(text[position - 1])) and (end == len(text) or not _word_character(text[end])):
            return True
        position = text.find(needle, position + 1)
    return False

def _duplicate(left: str, right: str) -> bool:
    a, b = _normalized(left), _normalized(right)
    if a == b:
        return True
    if _contains_complete(a, b) or _contains_complete(b, a):
        return True
    ta, tb = _terms(a), _terms(b)
    return len(ta) >= 4 and len(tb) >= 4 and len(ta & tb) * 100 >= 85 * len(ta | tb)


def select_candidates(*, owner: str, workspace_id: str, project_id: str | None,
                      privacy: Literal["normal", "no_memory", "incognito"],
                      query: str, context: tuple[str, ...],
                      candidates: tuple[Candidate, ...], include_global: bool = False,
                      limit: int = 3, char_budget: int = 1200) -> Selection:
    """Select in existing rank order; malformed or over-budget inputs fail closed."""
    if (type(owner) is not str or not owner or owner != owner.strip()
        or type(workspace_id) is not str or not workspace_id or workspace_id != workspace_id.strip()
        or (project_id is not None and (type(project_id) is not str or not project_id or project_id != project_id.strip()))
        or privacy not in ("normal", "no_memory", "incognito") or type(include_global) is not bool
        or type(query) is not str or len(query) > 4096 or type(context) is not tuple or len(context) > 32
        or any(type(x) is not str for x in context) or sum(map(len, context)) > 16000
        or type(limit) is not int or not 1 <= limit <= 3 or type(char_budget) is not int or not 1 <= char_budget <= 1200
        or type(candidates) is not tuple or len(candidates) > 20):
        raise ValueError("candidate admission input unavailable")
    if privacy != "normal":
        return Selection(POLICY_VERSION, ())
    query_terms = _terms(query)
    if len(query_terms) < 2:
        return Selection(POLICY_VERSION, ())
    selected: list[Candidate] = []
    used = 0
    seen_refs = set()
    for candidate in candidates:
        if (type(candidate) is not Candidate or type(candidate.revision) is not MemoryRevisionRef
            or type(candidate.text) is not str or not candidate.text.strip() or len(candidate.text) > 4096
            or type(candidate.labels) is not tuple or len(candidate.labels) > 32
            or any(type(label) is not str or len(label) > 256 for label in candidate.labels)):
            raise ValueError("candidate admission input unavailable")
        ref = candidate.revision
        if (type(ref.version) is not int or ref.version != 1 or ref.provider != "fm-v2"
            or type(ref.owner) is not str or type(ref.workspace_id) is not str
            or (ref.project_id is not None and type(ref.project_id) is not str)
            or type(ref.block_id) is not str or not ref.block_id or len(ref.block_id) > 512
            or type(ref.revision) is not int or ref.revision < 1
            or type(ref.value_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", ref.value_sha256)):
            raise ValueError("candidate revision unavailable")
        scoped = ref.workspace_id == workspace_id and ref.project_id in (None, project_id)
        global_scope = include_global and ref.workspace_id in ("", "global") and ref.project_id is None
        if ref.owner != owner or not (scoped or global_scope):
            continue
        identity = (ref.owner, ref.workspace_id, ref.project_id, ref.block_id, ref.revision, ref.value_sha256)
        if identity in seen_refs:
            continue
        seen_refs.add(identity)
        terms = _terms(candidate.text + " " + " ".join(candidate.labels))
        if len(query_terms & terms) * 2 < len(query_terms):
            continue
        if any(_duplicate(candidate.text, item) for item in context) or any(_duplicate(candidate.text, item.text) for item in selected):
            continue
        if used + len(candidate.text) > char_budget:
            continue
        selected.append(candidate)
        used += len(candidate.text)
        if len(selected) == limit:
            break
    return Selection(POLICY_VERSION, tuple(selected))
