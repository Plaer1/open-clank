# routes/skills_routes.py
"""REST API for the Skills system.

The on-disk format is SKILL.md (frontmatter + structured body) under
`data/skills/<category>/<name>/`. Old shape (`title`, `problem`, `solution`,
`steps`) still accepted on input — they're translated to the new fields
(`description`, `when_to_use`, `body_extra`, `procedure`).
"""

import asyncio
import logging
import json
import hashlib
import re
import uuid
from pathlib import Path
from typing import Callable, List, Literal, Optional

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.memory.skills import SkillsManager
from src.auth_helpers import get_current_user
from src.constants import DATA_DIR
from core.atomic_io import atomic_write_json
from core.middleware import require_admin

logger = logging.getLogger(__name__)
_SKILL_JOB_STATE_PATH = Path(DATA_DIR) / "skill-audit-jobs.json"


def _load_skill_job_state() -> dict:
    try:
        raw = json.loads(_SKILL_JOB_STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


_PERSISTED_SKILL_JOB_STATE = _load_skill_job_state()


def _restore_job_map(kind: str, persisted: Optional[dict] = None) -> dict:
    restored = {}
    source = persisted if isinstance(persisted, dict) else _PERSISTED_SKILL_JOB_STATE
    for row in source.get(kind, []):
        if not isinstance(row, dict) or not isinstance(row.get("key"), list):
            continue
        key = tuple(str(part) for part in row["key"])
        job = row.get("job") if isinstance(row.get("job"), dict) else {}
        if job.get("status") == "running":
            job["status"] = "manual_verification_required"
            job["verdict"] = job.get("verdict") or {
                "verdict": "manual_verification_required",
                "confidence": 0,
                "summary": "The server restarted before this job completed.",
                "issues": ["Restart the audit manually."],
            }
        restored[key] = job
    return restored


def _persist_skill_job_state() -> None:
    suspended = set(globals().get("_skill_job_persistence_suspended", set()))

    def _rows(store: dict, kind: str) -> list[dict]:
        rows = []
        for key, job in store.items():
            if _normalized_job_owner(key) in suspended:
                continue
            clean = {name: value for name, value in job.items() if name != "task"}
            try:
                clean = json.loads(json.dumps(clean))
            except (TypeError, ValueError):
                continue
            rows.append({"key": list(key), "job": clean})
        return rows

    # A lifecycle nuke may have cancelled an owner in memory while its
    # confirmed preview still fingerprints the durable rows. Preserve those
    # rows even when another owner's completion triggers this process-wide
    # writer; the nuke's subsequent purge or post-failure synchronization is
    # the authority that removes or refreshes them.
    existing = _load_skill_job_state()
    preserved: dict[str, list[dict]] = {}
    for kind in ("test", "audit"):
        preserved[kind] = [
            row for row in existing.get(kind, [])
            if isinstance(row, dict)
            and _normalized_job_owner(tuple(row.get("key", []))) in suspended
        ]

    atomic_write_json(str(_SKILL_JOB_STATE_PATH), {
        "test": preserved["test"] + _rows(globals().get("_skill_test_jobs", {}), "test"),
        "audit": preserved["audit"] + _rows(globals().get("_skill_audit_jobs", {}), "audit"),
    }, indent=2)


def _bound_skill_route(owner: Optional[str], purpose: str = "utility"):
    """Resolve the first live normalized binding for a skill workload."""
    from src.openclank.chat_routing import list_chat_routes, normalized_provider_owner
    from src.openclank.provider_store import ProviderStore

    normalized_owner = normalized_provider_owner(owner)
    store = ProviderStore()
    own, shared = list_chat_routes(normalized_owner, provider_store=store)
    visible = {route.model_route_id: route for route in (*own, *shared)}
    for binding in store.list_route_bindings(owner=normalized_owner):
        if (
            binding.purpose == purpose
            and binding.enabled
            and binding.model_route_id in visible
        ):
            return visible[binding.model_route_id]
    raise ValueError(
        f"No normalized {purpose} model is configured; set its provider route in Settings."
    )


def _selected_skill_route(
    *,
    owner: Optional[str],
    model_spec: Optional[str] = None,
    endpoint_id: Optional[str] = None,
    model_route_id: Optional[str] = None,
):
    """Resolve an explicit public selector, otherwise use the utility binding."""
    from src.openclank.chat_routing import resolve_chat_model_spec, resolve_chat_route

    route_id = str(model_route_id or "").strip()
    endpoint = str(endpoint_id or "").strip()
    spec = str(model_spec or "").strip()
    if route_id or endpoint:
        return resolve_chat_route(
            owner=owner,
            endpoint_id=endpoint or None,
            model_id=spec or None,
            model_route_id=route_id or None,
        )
    if spec and spec.casefold() != "auto":
        return resolve_chat_model_spec(owner=owner, model_spec=spec)
    return _bound_skill_route(owner, "utility")


def _skill_agent_target(route):
    """Build the strict ACP target from nonsecret normalized route identity."""
    from src.endpoint_resolver import ResolvedModelTarget

    route_capabilities = dict(route.capabilities or {})
    return ResolvedModelTarget(
        transport="acp",
        endpoint_url="mimo://acp",
        model_id=route.runtime_model,
        endpoint_id=route.connection_id,
        provider_id=route.connection_id,
        headers={},
        capabilities={
            "chat": True,
            "tools": route_capabilities.get("tools", True),
            "stream": True,
            "auxiliary": True,
            "vision": route_capabilities.get("vision"),
        },
        lifecycle="ephemeral",
    )


def _skill_test_unsafe_tools(skill_md: str) -> list[str]:
    """Tools without a hermetic read-only adapter require manual verification."""
    from src.tool_policy import known_tool_names
    from src.tool_security import COMPARE_READONLY_TOOLS

    text = str(skill_md or "").lower()
    referenced = {
        name for name in known_tool_names()
        if re.search(rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", text)
    }
    if "mcp__" in text:
        referenced.add("mcp__*")
    return sorted(referenced - set(COMPARE_READONLY_TOOLS))


async def _skill_auxiliary_call(
    purpose: str,
    messages: list[dict],
    *,
    owner: Optional[str],
    timeout: int,
    route=None,
    root_operation_id: Optional[str] = None,
    **options,
) -> str:
    import asyncio as _asyncio
    from src.openclank.modality_facade import complete_text

    operation_root = root_operation_id or f"skill-call-{uuid.uuid4().hex}"
    payload = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(
        f"{operation_root}\0{purpose}\0{getattr(route, 'model_route_id', '')}\0{payload}".encode("utf-8")
    ).hexdigest()[:32]
    return await _asyncio.wait_for(
        complete_text(
            owner=owner or "local-installation",
            purpose="utility",
            messages=messages,
            model_route_id=getattr(route, "model_route_id", None),
            grant_id=getattr(route, "provider_grant_id", None),
            root_operation_id=operation_root,
            idempotency_key=f"skills-{purpose}-{digest}"[:128],
            temperature=options.get("temperature"),
            max_output_tokens=options.get("max_tokens"),
        ),
        timeout=timeout,
    )

# Last-resort verdict extraction from a teacher/verifier model's prose (run when
# JSON parsing fails). `["\'\s:]*` already consumes whitespace, so the original
# trailing `\s*` made two adjacent \s-matching quantifiers that backtrack O(n^2)
# on a `verdict` + whitespace flood in untrusted model output (CodeQL
# py/polynomial-redos). Without it a single unbounded quantifier remains — the
# matched text is identical, and the scan is linear.
_VERDICT_PROSE_RE = re.compile(
    r'verdict["\'\s:]*["\']?(pass|needs_work|fail|inconclusive)', re.I
)


class SkillAddRequest(BaseModel):
    # New schema (preferred)
    name: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=200)
    category: str = Field("general", max_length=40)
    tags: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    requires_toolsets: List[str] = Field(default_factory=list)
    fallback_for_toolsets: List[str] = Field(default_factory=list)
    when_to_use: Optional[str] = Field(None, max_length=2000)
    procedure: List[str] = Field(default_factory=list)
    pitfalls: List[str] = Field(default_factory=list)
    verification: List[str] = Field(default_factory=list)
    status: Literal["draft", "published"] = "draft"
    version: str = "1.0.0"
    confidence: float = 0.8
    # Manual adds via this endpoint are human-authored → "user", which exempts
    # them from auto-dedup and cap-eviction in add_skill. (The agent's own
    # skill writes go through do_manage_skills with source="learned".)
    source: str = "user"
    teacher_model: Optional[str] = None
    session_id: Optional[str] = None

    # Old schema (back-compat)
    title: Optional[str] = Field(None, max_length=200)
    problem: Optional[str] = Field(None, max_length=2000)
    solution: Optional[str] = Field(None, max_length=5000)
    steps: List[str] = Field(default_factory=list)


class SkillImportUrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)


class SkillUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    platforms: Optional[List[str]] = None
    requires_toolsets: Optional[List[str]] = None
    fallback_for_toolsets: Optional[List[str]] = None
    when_to_use: Optional[str] = None
    procedure: Optional[List[str]] = None
    pitfalls: Optional[List[str]] = None
    verification: Optional[List[str]] = None
    status: Optional[Literal["draft", "published"]] = None
    version: Optional[str] = None
    confidence: Optional[float] = None
    body_extra: Optional[str] = None
    # Old shape
    title: Optional[str] = None
    problem: Optional[str] = None
    solution: Optional[str] = None
    steps: Optional[List[str]] = None
    expected_revision: Optional[int] = None
    expected_hash: Optional[str] = None
    waiver_reason: Optional[str] = None


class SkillPromotionNominateRequest(BaseModel):
    memory_ids: List[str] = Field(..., min_length=2, max_length=50)
    name: str = Field(..., min_length=1, max_length=80)
    scope: str = Field("owner", max_length=80)
    counterexamples: List[str] = Field(default_factory=list)


class SkillPromotionDraftRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    description: str = Field("", max_length=200)
    category: str = Field("memory", max_length=40)
    tags: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    requires_toolsets: List[str] = Field(default_factory=list)
    when_to_use: str = Field("", max_length=2000)
    procedure: List[str] = Field(default_factory=list)
    pitfalls: List[str] = Field(default_factory=list)
    verification: List[str] = Field(default_factory=list)


class SkillPromotionFeedbackRequest(BaseModel):
    reason: str = Field("", max_length=2000)


class SkillPromotionRollbackRequest(BaseModel):
    target_revision: int = Field(..., ge=1)
    reason: str = Field("", max_length=2000)


class SkillOutcomeRequest(BaseModel):
    signal: Literal["success", "failure", "correction", "contradiction", "mismatch"]
    replacement_skill_id: str = Field("", max_length=80)


def _skill_test_task(skill: dict) -> str:
    """Build a self-contained test task. Many skills act ON something (a doc,
    an email); if we just hand over the 'when to use' text the agent has nothing
    to work on and stalls asking for input. So we tell it to create its own
    realistic fixture first, then apply the skill end-to-end."""
    if not isinstance(skill, dict):
        skill = {}
    ctx = (skill.get("when_to_use") or skill.get("description") or skill.get("name") or "").strip()
    return (
        "Test this skill against the provided disposable fixture using only the "
        "read-only tools available. Do not create, edit, send, publish, or mutate "
        "anything. If the procedure needs a side effect, say manual verification is "
        "required. Apply every safe step you can and show the "
        "result. Context for when this skill is used: " + (ctx or "(general)")
    )


async def _eval_skill_run(skill_md: str, task: str, transcript: str,
                          route, owner: Optional[str] = None,
                          root_operation_id: Optional[str] = None) -> dict:
    """LLM-as-judge: grade a skill test run from its transcript. Advisory only.

    Robust against local reasoning models (strips <think>, lenient JSON,
    generous token budget) — same defensive parsing used elsewhere.
    """
    import json as _json
    import re as _re

    sys_prompt = (
        "You are a strict QA reviewer judging whether an AI 'skill' (a reusable "
        "procedure) actually works. You are given the SKILL, the TASK it was tested "
        "on, and the TRANSCRIPT of the agent's run.\n\n"
        "Judge honestly:\n"
        "- Did following the skill accomplish the task?\n"
        "- Are the steps clear, correct, and reproducible?\n"
        "- Did it reference tools/commands that don't exist or that errored?\n"
        "- Is it too vague or generic to be a useful, reusable skill?\n"
        "- METADATA: do the frontmatter fields match what the skill actually does? "
        "Flag wrong/misleading/missing tags, a wrong category, a when_to_use that "
        "doesn't describe the real trigger, or a description that oversells or "
        "mismatches the body. List each metadata problem in 'issues' (prefix it "
        "with 'metadata:'). Metadata problems alone do NOT make the verdict 'fail' "
        "if the procedure works — note them as issues on an otherwise-passing run.\n\n"
        "IMPORTANT — fairness rule: if the run could NOT proceed because it lacked "
        "an input or target the test never provided (e.g. there was no document/"
        "email/data to act on, so the agent reasonably asked for it), that is NOT "
        "the skill's fault. Return verdict \"inconclusive\" — do NOT mark it fail "
        "or needs_work. Only judge the skill's PROCEDURE; reserve fail/needs_work "
        "for when the steps themselves are wrong, vague, or reference missing tools.\n\n"
        "If you need to reason, do it inside <think></think> FIRST. Then output "
        "ONLY this JSON (no fences):\n"
        '{"verdict": "pass" | "needs_work" | "fail" | "inconclusive", '
        '"confidence": 0.0-1.0, "summary": "one short sentence", '
        '"issues": ["short issue", ...]}'
    )
    # Give the judge plenty of transcript, and when it must trim, keep the TAIL
    # (the final result lives at the end) plus a bit of the head — truncating to
    # a short prefix made the judge wrongly call complete runs "incomplete /
    # missing sections" because it never saw the end.
    def _clip(t: str, limit: int = 24000) -> str:
        t = (t or "").strip() or "(no output produced)"
        if len(t) <= limit:
            return t
        head = limit // 4
        return t[:head] + "\n\n…[transcript trimmed for length]…\n\n" + t[-(limit - head):]
    user_msg = (
        f"=== SKILL ===\n{(skill_md or '')[:4000]}\n\n"
        f"=== TASK ===\n{task}\n\n"
        f"=== TRANSCRIPT ===\n{_clip(transcript)}"
    )
    _VERDICTS = ("pass", "needs_work", "fail", "inconclusive")

    def _parse(raw: str):
        """Return a final result dict on success, or None if unparseable."""
        text = (raw or '')
        # Strip closed think blocks. If a <think> was opened but never closed
        # (the model ran out of budget mid-reasoning), drop everything from it
        # onward so its stray braces don't poison JSON extraction.
        text = _re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', text, flags=_re.I)
        text = _re.sub(r'<think(?:ing)?>[\s\S]*$', '', text, flags=_re.I).strip()

        def _coerce(d):
            return d if (isinstance(d, dict) and "verdict" in d) else None

        data = None
        # Scan every balanced {...} candidate and keep the LAST one that parses
        # and carries a "verdict" — the transcript is full of JSON API bodies,
        # so a naive first-brace/last-brace span almost never parses.
        for m in _re.finditer(r'\{[\s\S]*?\}', text):
            frag = m.group(0)
            for cand in (frag, _re.sub(r',(\s*[}\]])', r'\1', frag)):
                try:
                    d = _coerce(_json.loads(cand))
                except Exception:
                    d = None
                if d is not None:
                    data = d
        # Fallback to the greedy outermost span (handles nested objects the
        # non-greedy scan above splits apart).
        if data is None:
            a, b = text.find('{'), text.rfind('}')
            if a >= 0 and b > a:
                frag = text[a:b + 1]
                for cand in (frag, _re.sub(r',(\s*[}\]])', r'\1', frag)):
                    try:
                        d = _coerce(_json.loads(cand))
                    except Exception:
                        d = None
                    if d is not None:
                        data = d
                        break

        v = str(data.get("verdict", "")).lower().strip() if isinstance(data, dict) else None
        # Last resort: pull the verdict keyword straight out of the prose so a
        # clearly-decided run isn't thrown away as "unparseable".
        if v not in _VERDICTS:
            km = _VERDICT_PROSE_RE.search(text)
            if km:
                v = km.group(1).lower()
                if data is None:
                    data = {}
        if not isinstance(data, dict) or v not in _VERDICTS:
            return None
        try:
            conf = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        return {
            "verdict": v,
            "confidence": max(0.0, min(1.0, conf)),
            "summary": str(data.get("summary", ""))[:400],
            "issues": [str(x)[:200] for x in (data.get("issues") or []) if str(x).strip()][:8],
        }

    # Two attempts: the first lets the judge reason; if a heavy reasoning model
    # burns its budget inside <think> and never emits the JSON, the second
    # forbids thinking and demands the JSON immediately.
    last_text = ""
    last_err = None
    for attempt in range(2):
        msgs = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg}]
        if attempt == 1:
            msgs[0]["content"] = (
                sys_prompt + "\n\nDO NOT use <think> or any reasoning. Your reply "
                "must START with '{' and be ONLY the JSON object, nothing else."
            )
        try:
            raw = await _skill_auxiliary_call(
                "skill_run_judge",
                # Generous budget so a heavy reasoner can think AND still have
                # room to emit the JSON afterwards (reasoning tokens come out of
                # this same cap; the server clamps to its own max).
                msgs,
                temperature=0.1, max_tokens=32768, timeout=180,
                owner=owner, route=route,
                root_operation_id=root_operation_id,
            )
        except Exception as e:
            # Don't give up on a transient first-attempt error — let the second
            # (no-think) attempt run before reporting failure.
            last_err = e
            continue
        last_text = (raw or '')
        parsed = _parse(raw)
        if parsed is not None:
            return parsed

    if last_err is not None and not last_text:
        return {"verdict": "unknown", "confidence": 0, "summary": f"Evaluator call failed: {last_err}", "issues": []}
    return {"verdict": "unknown", "confidence": 0,
            "summary": "Evaluator returned unparseable output.", "issues": [], "raw": last_text[:300]}


async def _eval_skill_necessity(skill_md: str, others: list, route,
                                owner: Optional[str] = None,
                                root_operation_id: Optional[str] = None) -> Optional[dict]:
    """Advisory judge: is this skill worth keeping, or is it redundant / trivially
    unnecessary? Sees the OTHER skills' names+descriptions so it can spot
    duplicates. Returns {necessary, redundant_with, reason} or None. Never acts —
    purely a flag the UI surfaces."""
    import json as _json
    import re as _re

    catalog = "\n".join(f"- {o.get('name')}: {o.get('description', '')}" for o in others) or "(no other skills)"
    sys_prompt = (
        "You assess whether a reusable AI 'skill' (a saved procedure) is worth keeping. "
        "A skill is UNNECESSARY if it essentially duplicates another skill in the library, "
        "OR if it's so trivial/generic that a capable assistant would do it correctly with no "
        "saved procedure at all. A skill IS necessary if it captures a specific, non-obvious "
        "procedure, tool sequence, or hard-won detail.\n\n"
        "Be conservative: only call it unnecessary when you're confident. Reason in "
        "<think></think> first if needed, then output ONLY this JSON:\n"
        '{"necessary": true|false, "redundant_with": ["skill-name", ...], '
        '"reason": "one short sentence"}'
    )
    user_msg = (
        f"=== SKILL UNDER REVIEW ===\n{(skill_md or '')[:3000]}\n\n"
        f"=== OTHER SKILLS IN THE LIBRARY ===\n{catalog[:4000]}"
    )
    try:
        raw = await _skill_auxiliary_call(
            "skill_necessity_judge",
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
            temperature=0.1, max_tokens=8192, timeout=120,
            owner=owner, route=route,
            root_operation_id=root_operation_id,
        )
    except Exception as e:
        logger.warning(f"Necessity check failed: {e}")
        return None
    text = _re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', (raw or ''), flags=_re.I)
    text = _re.sub(r'<think(?:ing)?>[\s\S]*$', '', text, flags=_re.I).strip()
    data = None
    a, b = text.find('{'), text.rfind('}')
    if a >= 0 and b > a:
        frag = text[a:b + 1]
        for cand in (frag, _re.sub(r',(\s*[}\]])', r'\1', frag)):
            try:
                data = _json.loads(cand)
                break
            except Exception:
                continue
    if not isinstance(data, dict) or "necessary" not in data:
        return None
    return {
        "necessary": bool(data.get("necessary", True)),
        "redundant_with": [str(x)[:80] for x in (data.get("redundant_with") or []) if str(x).strip()][:6],
        "reason": str(data.get("reason", ""))[:200],
    }


def _should_check_retrieval_precision(skill: dict) -> bool:
    """Cheap prefilter for the expensive retrieval-precision judge.

    Skills with broad tags like "network" or "document" are the ones most likely
    to over-inject. Narrow command/vendor tags alone are fine.
    """
    broad = {
        "arch", "arch linux", "linux", "network", "networking", "wifi",
        "installation", "install", "system", "ssh", "document", "documents",
        "search", "email", "calendar", "gpu", "server", "python",
    }
    if not isinstance(skill, dict):
        return False
    tags = {str(t or "").strip().lower() for t in (skill.get("tags") or [])}
    if tags & broad:
        return True
    text = " ".join([
        str(skill.get("name") or ""),
        str(skill.get("description") or ""),
        str(skill.get("when_to_use") or ""),
    ]).lower()
    return sum(1 for t in broad if t in text) >= 2


async def _eval_skill_retrieval_precision(skill_md: str, others: list,
                                          route, owner: Optional[str] = None,
                                          root_operation_id: Optional[str] = None) -> Optional[dict]:
    """Advisory judge: would this skill's metadata make retrieval over-select it?

    This is distinct from "does the procedure work?". It asks whether tags,
    description, and when_to_use are specific enough that the skill won't be
    injected into adjacent but wrong tasks.
    """
    import json as _json
    import re as _re

    catalog = "\n".join(f"- {o.get('name')}: {o.get('description', '')}" for o in others[:80]) or "(no other skills)"
    sys_prompt = (
        "You are auditing retrieval metadata for a reusable AI skill. The app selects "
        "skills by matching user requests against the skill name, description, tags, "
        "when_to_use, and procedure text. Judge whether this skill is likely to be "
        "over-selected for nearby but wrong requests.\n\n"
        "Focus ONLY on metadata/retrieval precision, not whether the procedure works. "
        "Flag broad tags such as network, installation, system, document, search, ssh, "
        "python, gpu, or server when they would cause this narrow skill to match too "
        "many adjacent tasks. Recommend narrower tags/when_to_use wording. Compare "
        "against the other skills to spot boundaries.\n\n"
        "Return ok=true only when the trigger metadata is narrow enough. If not ok, "
        "issues MUST start with 'metadata: retrieval:' and be actionable. Output ONLY JSON:\n"
        '{"ok": true|false, "summary": "one short sentence", "issues": ["metadata: retrieval: ..."]}'
    )
    user_msg = (
        f"=== SKILL UNDER REVIEW ===\n{(skill_md or '')[:5000]}\n\n"
        f"=== OTHER SKILLS IN LIBRARY ===\n{catalog[:5000]}\n\n"
        "Decide if this skill's retrieval metadata should be narrowed so it only "
        "fires for its intended scenario and not for adjacent skills above."
    )
    try:
        raw = await _skill_auxiliary_call(
            "skill_retrieval_judge",
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
            temperature=0.1, max_tokens=4096, timeout=90,
            owner=owner, route=route,
            root_operation_id=root_operation_id,
        )
    except Exception as e:
        logger.warning(f"Retrieval precision check failed: {e}")
        return None
    text = _re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', (raw or ''), flags=_re.I)
    text = _re.sub(r'<think(?:ing)?>[\s\S]*$', '', text, flags=_re.I).strip()
    data = None
    a, b = text.find('{'), text.rfind('}')
    if a >= 0 and b > a:
        frag = text[a:b + 1]
        for cand in (frag, _re.sub(r',(\s*[}\]])', r'\1', frag)):
            try:
                data = _json.loads(cand)
                break
            except Exception:
                continue
    if not isinstance(data, dict) or "ok" not in data:
        return None
    return {
        "ok": bool(data.get("ok")),
        "summary": str(data.get("summary", ""))[:300],
        "issues": [str(x)[:220] for x in (data.get("issues") or []) if str(x).strip()][:6],
    }


# Durable skill-test checkpoints, keyed by (owner, skill_name). Live asyncio
# handles stay process-local; restart marks unfinished work for manual rerun.
_skill_test_jobs: dict = _restore_job_map("test")
_skill_test_handles: dict = {}


async def _run_skill_test_job(
    key, name, md, task, route, owner,
    skills_manager=None,
):
    """Background coroutine: run the skill in an agent loop, capture a condensed
    log + transcript, then have the judge grade it. Writes into _skill_test_jobs."""
    import json as _json
    import uuid as _uuid
    from src.model_dispatch import stream_agent_target

    job = _skill_test_jobs.get(key)
    if job is None:
        return
    log = job["log"]
    unsafe_tools = _skill_test_unsafe_tools(md)
    if unsafe_tools:
        job["verdict"] = {
            "verdict": "manual_verification_required",
            "confidence": 0,
            "summary": "This skill requires tools without a hermetic read-only test adapter.",
            "issues": [f"manual verification: {', '.join(unsafe_tools)}"],
        }
        log.append({"type": "manual_verification_required", "tools": unsafe_tools})
        job["status"] = "done"
        _persist_skill_job_state()
        return
    transcript = []
    say_buf = []

    def _flush_say():
        if say_buf:
            log.append({"type": "say", "text": "".join(say_buf)})
            say_buf.clear()

    messages = [
        {"role": "system", "content":
            "You are TESTING a skill. Below is a reusable skill (a procedure). Follow it "
            "to complete the user's task for real, using your available tools, step by "
            "step. If the skill is wrong, unclear, or references tools that don't exist, "
            "do your best — the problems will be reviewed afterward.\n\n=== SKILL ===\n" + md},
        {"role": "user", "content": task},
    ]
    infrastructure_error = None
    root_operation_id = f"skill-test-{_uuid.uuid4().hex}"
    try:
        import tempfile
        from pathlib import Path
        from src.tool_security import COMPARE_READONLY_TOOLS

        target = _skill_agent_target(route)
        with tempfile.TemporaryDirectory(prefix="openclank-skill-test-") as workspace:
            Path(workspace, "fixture.txt").write_text(
                "Disposable read-only skill-test fixture.\n", encoding="utf-8",
            )
            async for chunk in stream_agent_target(
                target,
                messages,
                session_id=f"skill-test-{_uuid.uuid4().hex}",
                owner=owner,
                cwd=workspace,
                relevant_tools=COMPARE_READONLY_TOOLS,
                max_tool_calls=12,
                max_rounds=8,
                workload="background",
                turn_envelope={
                    "allowed_tools": sorted(COMPARE_READONLY_TOOLS),
                    "interaction_policy": "fail_on_interaction",
                    "workspace": workspace,
                    "durable_id": f"skill-test:{name}",
                    "provider_grant_id": route.provider_grant_id,
                    "root_operation_id": root_operation_id,
                },
            ):
                if chunk.startswith("event: error"):
                    infrastructure_error = chunk
                    break
                if not chunk.startswith("data: ") or chunk.strip() == "data: [DONE]":
                    continue
                try:
                    d = _json.loads(chunk[6:])
                except Exception:
                    continue
                if d.get("delta"):
                    say_buf.append(d["delta"]); transcript.append(d["delta"])
                elif d.get("type") == "tool_start":
                    _flush_say()
                    cmd = str(d.get("command") or d.get("args") or "")[:300]
                    log.append({"type": "tool_start", "tool": d.get("tool"), "command": cmd})
                    transcript.append(f"\n[tool {d.get('tool')}] {cmd}\n")
                elif d.get("type") == "tool_output":
                    _flush_say()
                    out = str(d.get("output") or "")[:600]
                    log.append({"type": "tool_output", "output": out})
                    transcript.append(f"[output] {out}\n")
                elif d.get("type") == "agent_step":
                    _flush_say()
                    log.append({"type": "agent_step", "round": d.get("round")})
                    transcript.append(f"\n--- round {d.get('round')} ---\n")
                if len(log) > 600:
                    del log[0:len(log) - 600]
                if len(log) % 5 == 0:
                    _persist_skill_job_state()
        _flush_say()
    except Exception as e:
        _flush_say()
        log.append({"type": "error", "error": str(e)})
        infrastructure_error = str(e)

    if infrastructure_error:
        job["verdict"] = {
            "verdict": "manual_verification_required",
            "confidence": 0,
            "summary": "Skill test infrastructure did not complete.",
            "issues": [str(infrastructure_error)[:500]],
        }
        job["status"] = "done"
        _persist_skill_job_state()
        return

    log.append({"type": "evaluating"})
    try:
        job["verdict"] = await _eval_skill_run(
            md, task, "".join(transcript), route, owner=owner,
            root_operation_id=root_operation_id,
        )
    except Exception as e:
        job["verdict"] = {"verdict": "unknown", "confidence": 0, "summary": f"Eval failed: {e}", "issues": []}
    # Record the result so the card shows a 'verified' check (a manual test
    # never involves the teacher) and nudge the confidence score to match the
    # verdict — same scale as Audit-all's pass=0.95. inconclusive/unknown leave
    # the score alone (missing-fixture or parse failures shouldn't punish it).
    if skills_manager is not None:
        v = (job["verdict"] or {}).get("verdict") or "unknown"
        try:
            skills_manager.set_audit(
                name, v, by_teacher=False,
                worker_model=route.provider_model_id, owner=owner,
                results=job["verdict"],
            )
        except Exception:
            pass
        conf = {"pass": 0.95, "needs_work": 0.6, "fail": 0.4}.get(v)
        if conf is not None:
            try:
                skills_manager.update_skill(name, {"confidence": conf}, owner=owner)
            except Exception:
                pass
    job["status"] = "done"
    _persist_skill_job_state()


# ── Autonomous skill audit: test → judge → self-edit → retry → teacher → flag ──
_skill_audit_jobs: dict = _restore_job_map("audit")
_skill_audit_handles: dict = {}
_skill_job_persistence_suspended: set[str] = set()


def _normalized_job_owner(key: object) -> str:
    if not isinstance(key, tuple) or not key:
        return ""
    return str(key[0] or "").strip().lower()


def _track_skill_job_handle(handles: dict, key: tuple, handle: asyncio.Task) -> None:
    """Register one writer without letting an old callback evict its successor."""

    _skill_job_persistence_suspended.discard(_normalized_job_owner(key))
    handles[key] = handle

    def _finished(done: asyncio.Task, *, job_key=key, store=handles) -> None:
        if store.get(job_key) is done:
            store.pop(job_key, None)
        owner_key = _normalized_job_owner(job_key)
        if owner_key not in _skill_job_persistence_suspended:
            _persist_skill_job_state()

    handle.add_done_callback(_finished)


def track_skill_test_handle(key: tuple, handle: asyncio.Task) -> None:
    _track_skill_job_handle(_skill_test_handles, key, handle)


def track_skill_audit_handle(key: tuple, handle: asyncio.Task) -> None:
    _track_skill_job_handle(_skill_audit_handles, key, handle)


def synchronize_owner_skill_job_runtime(*owners: str) -> dict[str, int]:
    """Refresh only quiesced owners from the exact durable job-state file.

    Account lifecycle operations rename or purge ``skill-audit-jobs.json`` via
    ``SkillsManager``.  These module maps otherwise retain the old keys and a
    later unrelated job completion can serialize them back to disk.  Other
    owners' live dictionaries are deliberately left in place.
    """

    selected = {
        str(owner or "").strip().lower()
        for owner in owners
        if str(owner or "").strip()
    }
    if not selected:
        return {"test": 0, "audit": 0}

    persisted = _load_skill_job_state()
    _skill_job_persistence_suspended.difference_update(selected)
    counts: dict[str, int] = {}
    for kind, jobs, handles in (
        ("test", _skill_test_jobs, _skill_test_handles),
        ("audit", _skill_audit_jobs, _skill_audit_handles),
    ):
        for key, handle in list(handles.items()):
            if _normalized_job_owner(key) not in selected:
                continue
            if not handle.done():
                raise RuntimeError(
                    f"cannot synchronize active {kind} skill job runtime"
                )
            handles.pop(key, None)

        for key in list(jobs):
            if _normalized_job_owner(key) in selected:
                jobs.pop(key, None)
        restored = _restore_job_map(kind, persisted)
        selected_rows = 0
        for key, job in restored.items():
            if _normalized_job_owner(key) in selected:
                jobs[key] = job
                selected_rows += 1
        counts[kind] = selected_rows
    return counts


def cancel_owner_skill_job_handles(owner: Optional[str]) -> int:
    """Fence one owner's live skill jobs without rewriting their state file.

    The Brain reset's SkillsManager transaction removes the persisted rows
    immediately afterward. Keeping this helper persistence-free preserves the
    preview fingerprint while preventing a live task from recreating a skill
    after its owner-scoped tree has been staged for deletion.
    """
    owner_key = str(owner or "").strip().lower()
    cancelled = 0
    for jobs, handles in (
        (_skill_test_jobs, _skill_test_handles),
        (_skill_audit_jobs, _skill_audit_handles),
    ):
        selected = [
            key for key in list(jobs)
            if isinstance(key, tuple)
            and key
            and _normalized_job_owner(key) == owner_key
        ]
        for key in selected:
            handle = handles.get(key)
            if handle is not None and not handle.done():
                handle.cancel()
                cancelled += 1
            job = jobs.get(key)
            if isinstance(job, dict):
                job["cancel"] = True
    return cancelled


async def quiesce_owner_skill_job_handles(
    owner: Optional[str], *, persist: bool = True
) -> dict[str, object]:
    """Cancel and join every skill writer that can touch one owner's tree.

    The ownerless scheduled audit enumerates all owners, so its ``('',)``
    handle is intentionally joined too.  Job rows stay present until the exact
    Skills lifecycle transaction moves or purges them from the durable state.
    """

    owner_key = str(owner or "").strip().lower()
    if not owner_key:
        raise ValueError("skill lifecycle owner is required")
    if not persist:
        _skill_job_persistence_suspended.add(owner_key)
    joined: set[asyncio.Task] = set()
    while True:
        selected: set[asyncio.Task] = set()
        for jobs, handles, include_global in (
            (_skill_test_jobs, _skill_test_handles, False),
            (_skill_audit_jobs, _skill_audit_handles, True),
        ):
            for key, job in list(jobs.items()):
                if not isinstance(key, tuple) or not key:
                    continue
                key_owner = _normalized_job_owner(key)
                if key_owner != owner_key and not (include_global and not key_owner):
                    continue
                if isinstance(job, dict):
                    job["cancel"] = True
                handle = handles.get(key)
                if handle is not None and not handle.done():
                    selected.add(handle)
        if not selected:
            break
        for handle in selected:
            if not handle.cancelling():
                handle.cancel()
        joined.update(selected)
        await asyncio.shield(asyncio.gather(*selected, return_exceptions=True))

    for jobs, include_global in (
        (_skill_test_jobs, False),
        (_skill_audit_jobs, True),
    ):
        for key, job in list(jobs.items()):
            if not isinstance(key, tuple) or not key or not isinstance(job, dict):
                continue
            key_owner = _normalized_job_owner(key)
            if key_owner == owner_key or (include_global and not key_owner):
                if job.get("status") == "running":
                    job["status"] = "cancelled"
    # A nuke commit supplies a preview fingerprint that includes this file.
    # Keep cancellation status process-local until SkillsManager atomically
    # moves/purges the owner rows, otherwise quiescing would invalidate the
    # user's confirmed preview before the destructive step can CAS it.
    if persist:
        _persist_skill_job_state()
    return {"owner": owner_key, "joined": len(joined)}


def _audit_auto_publish_policy(owner) -> tuple[bool, float]:
    """Return (auto_publish_enabled, minimum_confidence) for audit finalization."""
    try:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner) or {}
    except Exception:
        prefs = {}
    try:
        from src.settings import get_setting
        default_min = get_setting("skill_autosave_min_confidence", 0.85)
    except Exception:
        default_min = 0.85
    enabled = bool(prefs.get("auto_approve_skills", False))
    try:
        min_conf = float(prefs.get("skill_min_confidence", default_min))
    except (TypeError, ValueError):
        min_conf = 0.85
    return enabled, max(0.0, min(1.0, min_conf))


def _skill_duplicate_blocker(skills_manager, name: str, owner) -> Optional[str]:
    """Cheap duplicate guard matching the UI's duplicate grouping.

    The LLM necessity check catches semantic redundancy, but the UI also has a
    cheap similarity pass. Use the same broad signal before auto-publishing so
    a high-scoring lower-priority duplicate stays draft.
    """
    import re as _re

    def _tokens(sk: dict) -> set[str]:
        text = " ".join([
            str(sk.get("name") or ""),
            str(sk.get("description") or ""),
            str(sk.get("when_to_use") or ""),
            " ".join(sk.get("procedure") or []),
            " ".join(sk.get("tags") or []),
        ]).lower()
        text = _re.sub(r"-\d+\b", "", text)
        return {
            t for t in _re.split(r"[^a-z0-9]+", text)
            if len(t) > 2 and t not in {"the", "and", "with", "for", "from", "using"}
        }

    def _sim(a: dict, b: dict) -> float:
        A, B = _tokens(a), _tokens(b)
        if not A or not B:
            return 0.0
        return len(A & B) / max(1, len(A | B))

    def _base(n: str) -> str:
        return _re.sub(r"-\d+$", "", str(n or ""))

    def _score(sk: dict) -> float:
        return (
            (100000 if (sk.get("status") == "published") else 0)
            + int(sk.get("uses") or 0) * 100
            + round(float(sk.get("confidence") or 0) * 100)
            + (-5 if sk.get("audit_by_teacher") else 0)
            - (len(str(sk.get("name") or "")) / 1000)
        )

    skills = skills_manager.load(owner=owner)
    current = next((s for s in skills if (s.get("name") or s.get("id")) == name), None)
    if not current:
        return None
    duplicates = []
    cur_name = current.get("name") or current.get("id") or name
    for other in skills:
        other_name = other.get("name") or other.get("id")
        if not other_name or other_name == cur_name:
            continue
        if _base(cur_name) == _base(other_name) or _sim(current, other) >= 0.38:
            duplicates.append(other)
    if not duplicates:
        return None
    keeper = sorted([current, *duplicates], key=_score, reverse=True)[0]
    keeper_name = keeper.get("name") or keeper.get("id") or ""
    if keeper_name and keeper_name != cur_name:
        try:
            skills_manager.set_necessity(
                cur_name,
                False,
                [keeper_name],
                f"Lower-priority duplicate of {keeper_name}",
                owner=owner,
            )
        except Exception:
            pass
        return keeper_name
    return None


def _audit_flag_text(*parts) -> str:
    text_parts = []
    for part in parts:
        if isinstance(part, dict):
            text_parts.extend(str(v or "") for v in part.values())
        elif isinstance(part, (list, tuple, set)):
            text_parts.extend(str(v or "") for v in part)
        else:
            text_parts.append(str(part or ""))
    return " ".join(text_parts).lower()


def _audit_generic_blocker(skill: Optional[dict], necessity: Optional[dict],
                           verdict_data: Optional[dict]) -> Optional[str]:
    """Return a short reason when a generic/trivial skill must stay draft."""
    generic_re = re.compile(
        r"\b(too[-\s]?generic|generic|trivial|capable assistant|without a saved|"
        r"not need|unnecessary|irrelevant)\b",
        re.I,
    )
    if isinstance(necessity, dict):
        reason = str(necessity.get("reason") or "")
        if necessity.get("necessary") is False and generic_re.search(reason):
            return reason or "Generic or unnecessary skill"

    if isinstance(skill, dict):
        tag_text = _audit_flag_text(skill.get("tags") or [])
        if generic_re.search(tag_text):
            return "Skill is tagged generic"

    if isinstance(verdict_data, dict):
        verdict_text = _audit_flag_text(
            verdict_data.get("summary"),
            verdict_data.get("issues") or [],
        )
        if generic_re.search(verdict_text):
            return "Audit flagged the skill as generic or unnecessary"
    return None


def _audit_finalize_status(skills_manager, name: str, owner, verdict: str,
                           confidence: Optional[float], necessity: Optional[dict] = None,
                           verdict_data: Optional[dict] = None) -> str:
    """Record evaluator output without borrowing publisher authority."""
    _auto_publish, min_conf = _audit_auto_publish_policy(owner)
    necessary = True
    current = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
    generic_reason = _audit_generic_blocker(current, necessity, verdict_data)
    if isinstance(necessity, dict) and necessity.get("necessary") is False:
        necessary = False
    if generic_reason:
        necessary = False
        try:
            skills_manager.set_necessity(name, False, [], generic_reason, owner=owner)
        except Exception:
            pass
    duplicate_of = _skill_duplicate_blocker(skills_manager, name, owner) if verdict == "pass" else None
    if duplicate_of:
        necessary = False
    c = float(confidence or 0.0)
    passed = necessary and verdict == "pass" and c >= min_conf
    try:
        if not passed:
            skills_manager.update_skill(name, {"status": "draft"}, owner=owner)
    except Exception:
        pass
    # Passing evaluation makes a draft eligible; a human/admin still performs
    # the explicit, revision-CAS publication step.
    return (current or {}).get("status", "draft") if passed else "draft"


def _apply_skill_md(skills_manager, name: str, md: str, owner) -> bool:
    """Parse + persist an edited SKILL.md. Returns True on success."""
    try:
        from services.memory.skill_format import Skill, slugify
        sk = Skill.from_markdown(md)
        # Pin the identity: the audit's fixer is now allowed to edit frontmatter
        # (tags/category/when_to_use/description), but it must NEVER rename the
        # skill — a changed `name` would move the dir and orphan the usage/audit
        # sidecar entries that the caller keeps writing under the original name.
        sk.name = name
        return bool(skills_manager.update_skill(name, {
            "name": sk.name, "description": sk.description, "version": sk.version,
            "category": sk.category, "tags": sk.tags, "platforms": sk.platforms,
            "requires_toolsets": sk.requires_toolsets, "fallback_for_toolsets": sk.fallback_for_toolsets,
            "status": "draft", "confidence": sk.confidence, "source": sk.source,
            "teacher_model": sk.teacher_model, "owner": sk.owner or owner,
            "when_to_use": sk.when_to_use, "procedure": sk.procedure,
            "pitfalls": sk.pitfalls, "verification": sk.verification, "body_extra": sk.body_extra,
        }, owner=owner))
    except Exception as e:
        logger.warning(f"Audit: could not save edited skill {name}: {e}")
        return False


async def _run_skill_test_once(
    md: str,
    task: str,
    route,
    owner,
    *,
    root_operation_id: Optional[str] = None,
) -> tuple:
    """Run the skill once in the agent loop; return (transcript, verdict)."""
    import json as _json
    import uuid as _uuid
    from src.model_dispatch import stream_agent_target

    unsafe_tools = _skill_test_unsafe_tools(md)
    if unsafe_tools:
        return "", {
            "verdict": "manual_verification_required",
            "confidence": 0,
            "summary": "No hermetic read-only adapter exists for this skill's tools.",
            "issues": [f"manual verification: {', '.join(unsafe_tools)}"],
        }

    transcript = []
    messages = [
        {"role": "system", "content":
            "You are TESTING a skill. Follow this skill's procedure to complete the task "
            "for real, using your tools, step by step.\n\n=== SKILL ===\n" + md},
        {"role": "user", "content": task},
    ]
    infrastructure_error = None
    terminal = False
    operation_root = root_operation_id or f"skill-audit-{_uuid.uuid4().hex}"
    try:
        import tempfile
        from pathlib import Path
        from src.tool_security import COMPARE_READONLY_TOOLS

        target = _skill_agent_target(route)
        with tempfile.TemporaryDirectory(prefix="openclank-skill-audit-") as workspace:
            Path(workspace, "fixture.txt").write_text(
                "Disposable read-only skill-audit fixture.\n", encoding="utf-8",
            )
            async for chunk in stream_agent_target(
                target,
                messages,
                session_id=f"skill-audit-{_uuid.uuid4().hex}",
                max_rounds=8,
                max_tool_calls=12,
                owner=owner,
                cwd=workspace,
                relevant_tools=COMPARE_READONLY_TOOLS,
                workload="background",
                turn_envelope={
                    "allowed_tools": sorted(COMPARE_READONLY_TOOLS),
                    "interaction_policy": "fail_on_interaction",
                    "workspace": workspace,
                    "provider_grant_id": route.provider_grant_id,
                    "root_operation_id": operation_root,
                },
            ):
                if chunk.startswith("event: error"):
                    infrastructure_error = chunk
                    break
                if chunk.strip() == "data: [DONE]":
                    terminal = True
                    continue
                if not chunk.startswith("data: "):
                    continue
                try:
                    d = _json.loads(chunk[6:])
                except Exception:
                    continue
                if d.get("delta") and not d.get("thinking"):
                    transcript.append(d["delta"])
                elif d.get("type") == "tool_start":
                    transcript.append(f"\n[tool {d.get('tool')}] {str(d.get('command') or d.get('args') or '')[:300]}\n")
                elif d.get("type") == "tool_output":
                    transcript.append(f"[output] {str(d.get('output') or '')[:600]}\n")
                elif d.get("type") == "agent_step":
                    transcript.append(f"\n--- round {d.get('round')} ---\n")
    except Exception as e:
        infrastructure_error = str(e)
    text = "".join(transcript)
    if infrastructure_error or not terminal or not text.strip():
        return text, {
            "verdict": "manual_verification_required",
            "confidence": 0,
            "summary": "Hermetic skill test infrastructure did not complete.",
            "issues": [str(infrastructure_error or "empty or interrupted Agent result")[:500]],
        }
    verdict = await _eval_skill_run(
        md, task, text, route, owner=owner,
        root_operation_id=operation_root,
    )
    return text, verdict


async def _improve_skill_md(
    skill_md: str,
    verdict: dict,
    transcript: str,
    route,
    owner=None,
    *,
    root_operation_id: Optional[str] = None,
):
    """Have a model rewrite SKILL.md to fix the reviewer's issues. Returns the
    corrected markdown, or None if it couldn't produce a usable change."""
    import re as _re
    issues = "\n".join("- " + str(i) for i in (verdict.get("issues") or []))
    sys_prompt = (
        "You are improving a reusable AI SKILL written in Markdown (frontmatter + body). "
        "A QA reviewer found problems after a test run. Rewrite the SKILL.md to fix them: "
        "make vague steps concrete, correct or remove references to tools that don't exist, "
        "ensure the procedure is reproducible. "
        "Keep the `name` field EXACTLY as-is (it is the skill's identity / filename). You MAY "
        "correct the OTHER frontmatter — tags, category, when_to_use, description — when the "
        "reviewer flagged them (issues prefixed 'metadata:') or they don't match the body; keep "
        "retrieval metadata narrow: remove broad tags that would over-select the skill, and make "
        "`when_to_use` say when NOT to use the skill if adjacent tasks are easy to confuse. Keep "
        "valid frontmatter structure. Do NOT invent capabilities the agent lacks. Reason in "
        "<think></think> first if needed, then output ONLY the full corrected SKILL.md (no "
        "fences, no commentary)."
    )
    user_msg = (
        f"=== CURRENT SKILL.md ===\n{skill_md}\n\n"
        f"=== REVIEWER VERDICT ===\n{verdict.get('summary', '')}\nIssues:\n{issues}\n\n"
        f"=== TEST TRANSCRIPT ===\n{(transcript or '')[:6000]}"
    )
    try:
        raw = await _skill_auxiliary_call(
            "skill_rewrite",
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": user_msg}],
            temperature=0.2, max_tokens=16384, timeout=180,
            owner=owner, route=route,
            root_operation_id=root_operation_id,
        )
    except Exception as e:
        logger.warning(f"Audit: improve call failed: {e}")
        return None
    text = _re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', (raw or ''), flags=_re.I)
    text = _re.sub(r'<think(?:ing)?>[\s\S]*$', '', text, flags=_re.I)
    text = _re.sub(r'</think(?:ing)?>', '', text, flags=_re.I).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # Some reasoning models still prepend analysis or echo the old skill before
    # the final corrected document. Keep only the last complete-looking
    # frontmatter document so reviewer prose never gets persisted into SKILL.md.
    starts = list(_re.finditer(r'(?m)^---\s*\n(?=[\s\S]*?^name\s*:)', text))
    if starts:
        text = text[starts[-1].start():].strip()
    return text or None


async def _audit_one_skill(skills_manager, skill, worker_route,
                           teacher_route, owner, log, *,
                           root_operation_id: Optional[str] = None) -> dict:
    """Test → judge → self-edit+retry → (teacher edit+retry) → flag. Never deletes;
    a skill the teacher still can't fix is demoted to draft for manual review.
    ``teacher_route`` is a normalized route or None. ``log(msg)`` records progress."""
    name = skill.get("name")
    operation_root = root_operation_id or f"skill-audit-{uuid.uuid4().hex}"
    worker_model = worker_route.provider_model_id

    # Reflect the audit outcome in the skill's confidence so the main list
    # updates: a clean pass earns high confidence; a pass that needed fixing
    # earns a bit less; a skill that still fails is marked low.
    def _set_conf(c):
        try:
            skills_manager.update_skill(name, {"confidence": c}, owner=owner)
        except Exception:
            pass

    md = skills_manager.read_skill_md(name, owner=owner)
    if not md:
        log(f"{name}: no source — skipped")
        return {"skill": name, "result": "skipped"}

    # Advisory necessity/redundancy check — runs once, independent of the test
    # outcome, and only records a flag the UI surfaces (never deletes/demotes).
    others = []
    nec = None
    try:
        # Only compare against skills the SAME owner can see, so the necessity
        # judge never sees (or flags "redundant_with") another user's skills.
        sk_owner = skill.get("owner")
        others = [
            {"name": s.get("name"), "description": s.get("description", "")}
            for s in skills_manager.load(owner=owner)
            if s.get("name") and s.get("name") != name
            and (not sk_owner or not s.get("owner") or s.get("owner") == sk_owner)
        ]
        nec = await _eval_skill_necessity(
            md, others, worker_route, owner=owner,
            root_operation_id=operation_root,
        )
        if nec is not None:
            skills_manager.set_necessity(name, nec.get("necessary", True),
                                         nec.get("redundant_with"), nec.get("reason"),
                                         owner=owner)
            if not nec.get("necessary", True):
                log(f"{name}: possibly unnecessary — {nec.get('reason', '')[:80]}")
    except Exception as e:
        log(f"{name}: necessity check skipped — {e}")

    generic_reason = _audit_generic_blocker(skill, nec, None)
    duplicate_of = _skill_duplicate_blocker(skills_manager, name, owner)
    if generic_reason or duplicate_of or (isinstance(nec, dict) and nec.get("necessary") is False):
        reason = generic_reason or (f"Lower-priority duplicate of {duplicate_of}" if duplicate_of else str((nec or {}).get("reason") or "Unnecessary skill"))
        try:
            skills_manager.update_skill(name, {"status": "draft", "confidence": 0.35}, owner=owner)
            skills_manager.set_audit(
                name, "skipped", by_teacher=False, worker_model=worker_model, owner=owner,
                results={"verdict": "skipped", "issues": [reason]},
            )
            if duplicate_of:
                skills_manager.set_necessity(name, False, [duplicate_of], reason, owner=owner)
            else:
                skills_manager.set_necessity(name, False, [], reason, owner=owner)
        except Exception:
            pass
        log(f"{name}: draft — skipped functional test ({reason[:100]})")
        return {"skill": name, "result": "skipped", "reason": reason, "confidence": 0.35, "status": "draft"}

    # Retrieval precision check: if broad tags/trigger text would make this
    # narrow skill over-inject, fix only metadata before the functional test.
    try:
        if _should_check_retrieval_precision(skill):
            rp = await _eval_skill_retrieval_precision(
                md, others, worker_route, owner=owner,
                root_operation_id=operation_root,
            )
            if rp and not rp.get("ok"):
                issues = rp.get("issues") or ["metadata: retrieval: narrow tags and when_to_use to the intended trigger"]
                log(f"{name}: narrowing retrieval metadata — {(rp.get('summary') or issues[0])[:80]}")
                fixed = await _improve_skill_md(md, {
                    "verdict": "pass",
                    "confidence": 1.0,
                    "summary": rp.get("summary") or "Retrieval metadata is too broad.",
                    "issues": issues,
                }, "Retrieval audit only: the procedure may work, but matching metadata is too broad.",
                    worker_route, owner=owner,
                    root_operation_id=operation_root)
                if fixed and fixed.strip() != md.strip() and _apply_skill_md(skills_manager, name, fixed, owner):
                    md = fixed
                    refreshed = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
                    if refreshed:
                        skill = refreshed
                skills_manager.set_retrieval_precision(
                    name,
                    False,
                    rp.get("summary") or "retrieval metadata needs another audit",
                    owner=owner,
                )
            elif rp and rp.get("ok"):
                skills_manager.set_retrieval_precision(
                    name, True, rp.get("summary") or "retrieval precision passed",
                    owner=owner,
                )
            else:
                skills_manager.set_retrieval_precision(
                    name, False, "retrieval precision evaluation was inconclusive",
                    owner=owner,
                )
        else:
            skills_manager.set_retrieval_precision(
                name, True, "separate retrieval precision check not required",
                owner=owner,
            )
    except Exception as e:
        skills_manager.set_retrieval_precision(
            name, False, "retrieval precision evaluation failed", owner=owner
        )
        log(f"{name}: retrieval precision check skipped — {e}")

    task = _skill_test_task(skill)
    log(f"{name}: testing…")
    transcript, verdict = await _run_skill_test_once(
        md, task, worker_route, owner,
        root_operation_id=operation_root,
    )
    v = verdict.get("verdict")
    log(f"{name}: verdict = {v} ({verdict.get('summary', '')[:80]})")
    if v == "pass":
        # Procedure works. If the reviewer still flagged metadata (tags/category/
        # when_to_use/description), do ONE fixer pass to correct the frontmatter
        # without re-testing — a metadata-only fix can't break a passing run.
        meta_issues = [i for i in (verdict.get("issues") or []) if str(i).lower().lstrip().startswith("metadata:")]
        if meta_issues:
            log(f"{name}: pass, but fixing {len(meta_issues)} metadata issue(s)…")
            fixed = await _improve_skill_md(
                md, verdict, transcript, worker_route, owner=owner,
                root_operation_id=operation_root,
            )
            if fixed and fixed.strip() != md.strip():
                _apply_skill_md(skills_manager, name, fixed, owner)
        _set_conf(0.95)
        skills_manager.set_audit(
            name, "pass", by_teacher=False, worker_model=worker_model, owner=owner,
            results=verdict,
        )
        refreshed = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
        status = _audit_finalize_status(skills_manager, name, owner, "pass", 0.95, (refreshed or {}).get("necessity"), verdict)
        log(f"{name}: {status} — confidence 95%")
        return {"skill": name, "result": "pass", "verdict": verdict, "confidence": 0.95, "status": status}
    if v == "manual_verification_required":
        current_status = skill.get("status") or "draft"
        log(f"{name}: manual verification required — skill left unchanged")
        return {
            "skill": name,
            "result": "manual_verification_required",
            "verdict": verdict,
            "status": current_status,
        }
    if v in ("unknown", "inconclusive"):
        skills_manager.set_audit(
            name, "inconclusive", by_teacher=False, worker_model=worker_model, owner=owner,
            results=verdict,
        )
        status = _audit_finalize_status(skills_manager, name, owner, "inconclusive", skill.get("confidence") or 0.0, skill.get("necessity"))
        log(f"{name}: {status} — inconclusive")
        return {"skill": name, "result": "inconclusive", "verdict": verdict, "status": status}

    # Self-edit + retry.
    log(f"{name}: self-editing to fix issues…")
    new_md = await _improve_skill_md(
        md, verdict, transcript, worker_route, owner=owner,
        root_operation_id=operation_root,
    )
    if new_md and new_md.strip() != md.strip() and _apply_skill_md(skills_manager, name, new_md, owner):
        md = new_md
        transcript, verdict = await _run_skill_test_once(
            md, task, worker_route, owner,
            root_operation_id=operation_root,
        )
        v = verdict.get("verdict")
        log(f"{name}: retry (self) = {v}")
        if v == "pass":
            _set_conf(0.85)
            skills_manager.set_audit(
                name, "pass", by_teacher=False, worker_model=worker_model, owner=owner,
                results=verdict,
            )
            refreshed = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
            status = _audit_finalize_status(skills_manager, name, owner, "pass", 0.85, (refreshed or {}).get("necessity"), verdict)
            log(f"{name}: {status} — confidence 85% after self-edit")
            return {"skill": name, "result": "pass_after_self_edit", "verdict": verdict, "confidence": 0.85, "status": status}

    # Teacher escalation (if a distinct teacher model is configured). The
    # teacher only REWRITES the skill — it does NOT run the test. The point is
    # to verify the regular (student) model can now succeed with the teacher's
    # improved procedure, so the retry runs on the worker model, not the teacher.
    teacher_ran = False
    if teacher_route and teacher_route.model_route_id != worker_route.model_route_id:
        teacher_ran = True
        teacher_model = teacher_route.provider_model_id
        log(f"{name}: teacher {teacher_model} rewriting the skill…")
        t_md = await _improve_skill_md(
            md, verdict, transcript, teacher_route, owner=owner,
            root_operation_id=operation_root,
        )
        if t_md and t_md.strip() != md.strip() and _apply_skill_md(skills_manager, name, t_md, owner):
            md = t_md
        # Re-test with the STUDENT model (the model the skill runs under in use).
        transcript, verdict = await _run_skill_test_once(
            md, task, worker_route, owner,
            root_operation_id=operation_root,
        )
        v = verdict.get("verdict")
        log(f"{name}: retry on student after teacher rewrite = {v}")
        if v == "pass":
            _set_conf(0.8)
            skills_manager.set_audit(
                name, "pass", by_teacher=True, worker_model=worker_model,
                teacher_model=teacher_model, owner=owner, results=verdict,
            )
            refreshed = next((s for s in skills_manager.load(owner=owner) if s.get("name") == name), None)
            status = _audit_finalize_status(skills_manager, name, owner, "pass", 0.8, (refreshed or {}).get("necessity"), verdict)
            log(f"{name}: {status} — confidence 80% after teacher rewrite")
            return {"skill": name, "result": "pass_after_teacher", "verdict": verdict, "confidence": 0.8, "status": status}

    # Still failing → demote to draft + low confidence + flag (do NOT delete).
    try:
        skills_manager.update_skill(name, {"status": "draft", "confidence": 0.35}, owner=owner)
    except Exception:
        pass
    skills_manager.set_audit(
        name, v or "fail", by_teacher=teacher_ran,
        worker_model=worker_model,
        teacher_model=(teacher_route.provider_model_id if teacher_ran else ""),
        owner=owner,
        results=verdict,
    )
    log(f"{name}: flagged — confidence lowered, kept as draft for manual review")
    return {"skill": name, "result": "flagged", "verdict": verdict, "confidence": 0.35}


async def _run_audit_all_job(
    key, skills_manager, names, worker_route, teacher_route, owner,
    *, root_operation_id: Optional[str] = None,
):
    """Background: audit each named skill in sequence, recording progress."""
    import asyncio as _asyncio
    import time as _time

    job = _skill_audit_jobs.get(key)
    if job is None:
        return

    def log(msg):
        job["log"].append(msg)
        if len(job["log"]) > 1000:
            del job["log"][0:len(job["log"]) - 1000]
        if len(job["log"]) % 5 == 0:
            _persist_skill_job_state()

    cancelled = False
    try:
        for nm in names:
            if job.get("cancel"):
                cancelled = True
                log("(cancelled)")
                break
            job["current"] = nm
            skills = skills_manager.load(owner=owner)
            sk = next((s for s in skills if s.get("name") == nm), None)
            if not sk:
                continue
            try:
                res = await _audit_one_skill(
                    skills_manager, sk, worker_route, teacher_route, owner, log,
                    root_operation_id=root_operation_id,
                )
            except _asyncio.CancelledError:
                cancelled = True
                job["cancel"] = True
                log("(cancelled)")
                raise
            except Exception as e:
                log(f"{nm}: error — {e}")
                res = {"skill": nm, "result": "error"}
            try:
                refreshed = next((s for s in skills_manager.load(owner=owner) if s.get("name") == nm), None)
                if refreshed:
                    res["skill_state"] = {
                        "name": refreshed.get("name"),
                        "status": refreshed.get("status"),
                        "confidence": refreshed.get("confidence"),
                        "audit_verdict": refreshed.get("audit_verdict"),
                        "audit_by_teacher": refreshed.get("audit_by_teacher"),
                        "audit_worker_model": refreshed.get("audit_worker_model"),
                        "audit_teacher_model": refreshed.get("audit_teacher_model"),
                        "audited_at": refreshed.get("audited_at"),
                        "necessity": refreshed.get("necessity"),
                    }
            except Exception:
                pass
            job["results"].append(res)
            job["done"] = len(job["results"])
            _persist_skill_job_state()
    except _asyncio.CancelledError:
        cancelled = True
    finally:
        job["current"] = None
        job["status"] = "cancelled" if cancelled or job.get("cancel") else "done"
        job["finished"] = _time.time()
        _persist_skill_job_state()


def _resolve_audit_models(owner=None):
    """Resolve normalized worker and optional teacher routes for an audit run.

    The worker follows the durable Utility binding. The teacher is an explicit
    model selector from Settings. No provider URL, header, or credential crosses
    this application boundary.
    """
    worker_route = _bound_skill_route(owner, "utility")

    teacher_route = None
    try:
        from src.settings import get_user_setting
        if get_user_setting("teacher_enabled", owner or "", False):
            spec = (get_user_setting("teacher_model", owner or "", "") or "").strip()
            if spec:
                from src.openclank.chat_routing import resolve_chat_model_spec
                teacher_route = resolve_chat_model_spec(owner=owner, model_spec=spec)
    except Exception as e:
        logger.warning(f"Audit teacher resolve failed: {e}")
    return worker_route, teacher_route


def _scheduled_owner_is_fenced(
    owner: str,
    owner_is_fenced: Optional[Callable[[str], bool]],
) -> bool:
    if owner_is_fenced is None:
        return False
    try:
        return bool(owner_is_fenced(owner))
    except Exception:
        logger.exception("Scheduled skill audit owner-fence check failed")
        return True


async def _run_scheduled_owner_skill_audit(
    skills_manager: SkillsManager,
    *,
    owner: str,
    names: list[str],
    owner_is_fenced: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Run one concrete owner's scheduled batch behind a tracked handle."""
    import time as _time

    owner = str(owner or "").strip()
    if not owner:
        return {"status": "skipped", "reason": "scheduled audit requires an owner"}
    if _scheduled_owner_is_fenced(owner, owner_is_fenced):
        return {"status": "skipped", "reason": "account lifecycle is active"}

    key = (owner,)
    existing = _skill_audit_jobs.get(key)
    existing_handle = _skill_audit_handles.get(key)
    if existing_handle is not None and not existing_handle.done():
        logger.info("Scheduled skill audit skipped — a run is already active.")
        return {"status": "running", "skipped": True}

    try:
        worker_route, teacher_route = _resolve_audit_models(owner=owner)
    except ValueError as e:
        logger.info(f"Scheduled skill audit skipped — {e}")
        return {"status": "skipped", "reason": str(e)}

    if not names:
        return {"status": "done", "total": 0}

    _skill_audit_jobs[key] = {
        "status": "running", "scope": "scheduled",
        "model": worker_route.provider_model_id,
        "teacher": teacher_route.provider_model_id if teacher_route else None,
        "total": len(names), "done": 0, "current": None,
        "results": [], "log": [
            f"Nightly audit of {len(names)} least-recently-checked skill(s) "
            f"with {worker_route.provider_model_id}"
            + (f"; teacher {teacher_route.provider_model_id}" if teacher_route else "")
        ],
        "started": _time.time(), "cancel": False,
    }
    _persist_skill_job_state()
    logger.info("Scheduled skill audit starting: %s skill(s) (owner=%s)", len(names), owner)
    audit_task = asyncio.create_task(
        _run_audit_all_job(
            key, skills_manager, names, worker_route, teacher_route, owner
        )
    )
    track_skill_audit_handle(key, audit_task)
    try:
        await audit_task
    except asyncio.CancelledError:
        # Quiescence cancels the tracked child. Awaiting that child propagates
        # cancellation into this coordinator, which must still return a
        # truthful cancelled result to the nightly dispatcher.
        await asyncio.gather(audit_task, return_exceptions=True)
    job = _skill_audit_jobs.get(key, {})
    status = str(job.get("status") or "done")
    cancelled = status == "cancelled" or bool(job.get("cancel"))
    return {
        "status": "cancelled" if cancelled else "done",
        "total": len(names),
        "results": job.get("results", []),
    }


async def run_scheduled_skill_audit(
    skills_manager: SkillsManager,
    owner: Optional[str] = None,
    max_skills: int = 8,
    *,
    owner_is_fenced: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Audit the globally oldest skills as concrete owner-scoped jobs.

    ``owner=None`` is a dispatcher, never a write authority. It selects one
    bounded cross-account batch, groups it by each skill's durable owner, and
    then invokes the exact-owner worker. This avoids the former ``('',)`` job,
    which loaded every account's metadata but could only mutate ownerless
    skills. Ownerless files are intentionally skipped; startup ownership
    backfill must assign them before any autonomous audit may read them.
    """

    limit = max(1, int(max_skills))
    if owner is not None:
        exact_owner = str(owner or "").strip()
        skills = list(skills_manager.load(owner=exact_owner))
        skills.sort(
            key=lambda skill: (
                skill.get("audited_at")
                if skill.get("audited_at") is not None
                else -1.0
            )
        )
        names = [
            str(skill.get("name"))
            for skill in skills
            if skill.get("name")
        ][:limit]
        return await _run_scheduled_owner_skill_audit(
            skills_manager,
            owner=exact_owner,
            names=names,
            owner_is_fenced=owner_is_fenced,
        )

    candidates = [
        skill
        for skill in skills_manager.load_all()
        if skill.get("name") and str(skill.get("owner") or "").strip()
    ]
    candidates.sort(
        key=lambda skill: (
            skill.get("audited_at")
            if skill.get("audited_at") is not None
            else -1.0
        )
    )
    grouped: dict[str, list[str]] = {}
    for skill in candidates[:limit]:
        exact_owner = str(skill.get("owner") or "").strip()
        grouped.setdefault(exact_owner, []).append(str(skill["name"]))

    results: list[dict] = []
    audited = 0
    for exact_owner, names in grouped.items():
        result = await _run_scheduled_owner_skill_audit(
            skills_manager,
            owner=exact_owner,
            names=names,
            owner_is_fenced=owner_is_fenced,
        )
        results.append({"owner": exact_owner, **result})
        if result.get("status") == "done":
            audited += int(result.get("total") or 0)
    return {"status": "done", "total": audited, "owners": results}


def setup_skills_routes(skills_manager: SkillsManager) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    def _verify_owner(skill: dict, user: Optional[str]):
        if user is None:
            return
        # SECURITY: strict check — previously `sk_owner and sk_owner != user`
        # let any user mutate/read a skill that happened to have no owner
        # field (legacy or un-stamped writes), since the truthiness guard
        # short-circuited the comparison. Treat missing owner as not-owned.
        if skill.get("owner") != user:
            raise HTTPException(404, "Skill not found")

    def _fire_skill_added(user: Optional[str]):
        try:
            from src.event_bus import fire_event
            fire_event("skill_added", user)
        except Exception:
            logger.debug("skill_added event dispatch failed", exc_info=True)

    @router.get("")
    async def list_skills(request: Request):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        return {"skills": skills, "count": len(skills)}

    @router.get("/index")
    async def get_index(request: Request):
        """The lightweight `[{name, description, category}]` list that the
        agent's system prompt sees. Useful for the UI's "what does the model
        actually have access to?" view."""
        user = _owner(request)
        idx = skills_manager.index_for(owner=user)
        return {"index": idx, "count": len(idx)}

    @router.get("/slash-catalog")
    async def get_slash_catalog(request: Request):
        """Return skills that are available as slash commands.

        Mirrors the agent prompt's published-skill index so the UI never offers
        a slash command the model would not normally be allowed to discover.
        """
        user = _owner(request)
        all_skills = {s.get("name"): s for s in skills_manager.load(owner=user)}
        entries = []
        for s in skills_manager.index_for(owner=user):
            name = (s.get("name") or "").strip()
            if not name:
                continue
            full = all_skills.get(name) or {}
            category = (s.get("category") or full.get("category") or "general").strip() or "general"
            entries.append({
                "type": "skill",
                "token": f"/{name}",
                "name": name,
                "category": f"Skills / {category}",
                "help": s.get("description") or full.get("description") or "",
                "usage": f"/{name} <request>",
                "uses": int(full.get("uses") or 0),
                "last_used": full.get("last_used"),
            })
        entries.sort(key=lambda row: row["name"])
        return {"skills": entries, "count": len(entries)}

    @router.get("/builtin")
    async def list_builtin_skills(request: Request):
        """Read-only list of the agent's built-in tool capabilities (research,
        sessions, tasks, email, etc.) — the things it natively knows how to do.
        Surfaced so the Skills tab can show them in a separate "Built-in"
        section alongside the user's learned SKILL.md skills. Sourced from
        agent_loop.TOOL_SECTIONS (the same descriptions the model is given)."""
        import re

        def _clean(raw: str) -> str:
            s = raw or ""
            s = re.sub(r"```.*?```", "", s, flags=re.S)   # drop code fences (incl. inline ```name```)
            s = re.sub(r"\s+", " ", s).strip()
            s = re.sub(r"^[-–—:\s]+", "", s)              # drop leftover "- — " / ": " bullet prefix
            return s[:240]

        try:
            from src.agent_loop import TOOL_SECTIONS, get_builtin_overrides
        except Exception as e:
            return {"builtin": [], "count": 0, "error": str(e)}

        overrides = get_builtin_overrides()
        out = []
        for key, raw in TOOL_SECTIONS.items():
            names = key if isinstance(key, tuple) else (key,)
            for nm in names:
                if isinstance(nm, str):
                    overridden = nm in overrides
                    eff = overrides.get(nm, raw)
                    out.append({
                        "name": nm,
                        "description": _clean(eff),
                        "is_overridden": overridden,
                    })
        out.sort(key=lambda x: x["name"])
        return {"builtin": out, "count": len(out)}

    @router.get("/builtin/{name}")
    async def get_builtin_skill(name: str, request: Request):
        """Full text of a built-in tool's instruction block — the override
        if one is set, plus the shipped default (for the revert button)."""
        try:
            from src.agent_loop import TOOL_SECTIONS, get_builtin_overrides
        except Exception as e:
            raise HTTPException(500, str(e))
        default = None
        for key, raw in TOOL_SECTIONS.items():
            names = key if isinstance(key, tuple) else (key,)
            if name in names:
                default = raw
                break
        if default is None:
            raise HTTPException(404, f"No built-in tool named {name!r}")
        overrides = get_builtin_overrides()
        return {
            "name": name,
            "text": overrides.get(name, default),
            "default": default,
            "is_overridden": name in overrides,
        }

    @router.put("/builtin/{name}")
    async def set_builtin_override(name: str, request: Request):
        """Save a user override for a built-in tool's instruction block.
        WARNING surfaced in the UI — this changes how the assistant is
        told to use a native tool."""
        require_admin(request)
        from src.agent_loop import TOOL_SECTIONS
        valid = set()
        for key in TOOL_SECTIONS:
            valid.update(key if isinstance(key, tuple) else (key,))
        if name not in valid:
            raise HTTPException(404, f"No built-in tool named {name!r}")
        body = await request.json()
        text = (body or {}).get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "text is required")
        from src.settings import get_setting, save_settings, load_settings
        settings = load_settings()
        ov = settings.get("builtin_tool_overrides")
        if not isinstance(ov, dict):
            ov = {}
        ov[name] = text
        settings["builtin_tool_overrides"] = ov
        save_settings(settings)
        return {"ok": True, "name": name, "is_overridden": True}

    @router.delete("/builtin/{name}")
    async def reset_builtin_override(name: str, request: Request):
        """Revert a built-in tool to its shipped instruction block."""
        require_admin(request)
        from src.settings import load_settings, save_settings
        settings = load_settings()
        ov = settings.get("builtin_tool_overrides")
        if isinstance(ov, dict) and name in ov:
            del ov[name]
            settings["builtin_tool_overrides"] = ov
            save_settings(settings)
        return {"ok": True, "name": name, "is_overridden": False}

    @router.post("/import-from-url")
    async def import_skill_from_url(request: Request, body: SkillImportUrlRequest):
        """Install a SKILL.md bundle from a public GitHub URL (skills.sh links supported)."""
        require_admin(request)
        user = _owner(request)
        from services.memory.skill_importer import (
            SkillImportError,
            fetch_skill_bundle,
        )

        try:
            files, resolved_source = fetch_skill_bundle(body.url.strip())
            entry = skills_manager.import_bundle_from_files(
                files,
                owner=user,
                source_url=resolved_source.canonical_uri,
                source_revision=resolved_source.ref,
            )
        except SkillImportError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPError as e:
            logger.warning("skill import fetch failed: %s", e)
            detail = str(e).strip() or "Could not download skill from URL"
            raise HTTPException(502, detail) from e
        except Exception as e:
            logger.error("skill import failed: %s", e)
            raise HTTPException(500, "Skill import failed") from e

        _fire_skill_added(user)
        return {"ok": True, "skill": entry, "files": len(files)}

    @router.post("/add")
    async def add_skill(request: Request, body: SkillAddRequest):
        user = _owner(request)
        requested_publish = body.status == "published"
        entry = skills_manager.add_skill(
            # New shape
            name=body.name,
            description=body.description,
            category=body.category,
            tags=body.tags,
            platforms=body.platforms,
            requires_toolsets=body.requires_toolsets,
            fallback_for_toolsets=body.fallback_for_toolsets,
            when_to_use=body.when_to_use,
            procedure=body.procedure,
            pitfalls=body.pitfalls,
            verification=body.verification,
            status=body.status,
            version=body.version,
            confidence=body.confidence,
            source=body.source,
            teacher_model=body.teacher_model,
            session_id=body.session_id,
            owner=user,
            # Old shape (manager translates)
            title=body.title or "",
            problem=body.problem or "",
            solution=body.solution or "",
            steps=body.steps,
        )
        if not entry.get("_deduped"):
            _fire_skill_added(user)
        response = {
            "ok": True,
            "deduped": bool(entry.get("_deduped")),
            "skill": entry,
        }
        if requested_publish and not entry.get("_deduped"):
            # Creation is always staged. Returning success avoids the previous
            # confusing state where a 409 response still left a draft on disk.
            response["staged"] = True
            response["publish_readiness"] = skills_manager.publish_readiness(
                entry.get("skill_id"),
                owner=user,
            )
        return response

    @router.get("/promotions")
    async def list_promotions(request: Request):
        return {"promotions": skills_manager.list_promotions(_owner(request))}

    @router.post("/promotions/nominate")
    async def nominate_promotion(request: Request, body: SkillPromotionNominateRequest):
        user = _owner(request)
        provider = getattr(request.app.state, "memory_provider", None)
        if provider is None or not hasattr(provider, "get"):
            raise HTTPException(503, "Memory provider is unavailable")
        citations = []
        counterexamples = list(body.counterexamples)
        for memory_id in dict.fromkeys(body.memory_ids):
            record = await provider.get(memory_id, owner=user)
            if record is None or getattr(record, "archived", False):
                raise HTTPException(404, f"Curated memory not found: {memory_id}")
            if getattr(record, "provenance_conflict", False):
                counterexamples.append(
                    f"memory {getattr(record, 'id', memory_id)} has an unresolved provenance conflict"
                )
            text = str(getattr(record, "text", "") or "")
            citations.append({
                "memory_id": str(getattr(record, "id", memory_id)),
                "content_hash": (
                    str(getattr(record, "content_hash", "") or "")
                    or hashlib.sha256(text.encode("utf-8")).hexdigest()
                ),
                "source": str(getattr(record, "source", "") or ""),
                "source_type": str(getattr(record, "source_type", "") or ""),
                "kind": str(getattr(record, "kind", "") or ""),
                "updated_at": getattr(record, "updated_at", None),
                "source_uri": getattr(record, "source_uri", None),
                "source_revision": getattr(record, "source_revision", None),
            })
        try:
            promotion = skills_manager.nominate_promotion(
                owner=user,
                recommender=f"user:{user or 'local'}",
                citations=citations,
                name=body.name,
                scope=body.scope,
                counterexamples=counterexamples,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "promotion": promotion}

    @router.post("/promotions/{promotion_id}/draft")
    async def draft_promotion(
        request: Request,
        promotion_id: str,
        body: SkillPromotionDraftRequest,
    ):
        user = _owner(request)
        try:
            result = skills_manager.draft_promotion(
                promotion_id,
                owner=user,
                drafter=f"user:{user or 'local'}",
                fields=body.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        _fire_skill_added(user)
        return {"ok": True, **result}

    @router.post("/promotions/{promotion_id}/evaluate")
    async def evaluate_promotion(
        request: Request,
        promotion_id: str,
        body: SkillPromotionFeedbackRequest,
    ):
        user = _owner(request)
        rows = skills_manager.list_promotions(user)
        row = next((item for item in rows if item.get("id") == promotion_id), None)
        if not row:
            raise HTTPException(404, "Promotion not found")
        readiness = skills_manager.publish_readiness(row.get("skill_id") or "", user)
        evaluator = f"audit:{readiness.get('evaluator') or 'unknown'}"
        try:
            result = skills_manager.evaluate_promotion(
                promotion_id,
                owner=user,
                evaluator=evaluator,
                feedback=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "promotion": result}

    @router.post("/promotions/{promotion_id}/publish")
    async def publish_promotion(request: Request, promotion_id: str):
        user = _owner(request)
        try:
            result = skills_manager.publish_promotion(
                promotion_id,
                owner=user,
                publisher=f"user:{user or 'local'}",
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "promotion": result}

    @router.post("/promotions/{promotion_id}/reject")
    async def reject_promotion(
        request: Request,
        promotion_id: str,
        body: SkillPromotionFeedbackRequest,
    ):
        user = _owner(request)
        try:
            result = skills_manager.reject_promotion(
                promotion_id,
                owner=user,
                actor=f"user:{user or 'local'}",
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "promotion": result}

    @router.post("/promotions/{promotion_id}/rollback")
    async def rollback_promotion(
        request: Request,
        promotion_id: str,
        body: SkillPromotionRollbackRequest,
    ):
        user = _owner(request)
        try:
            result = skills_manager.rollback_promotion(
                promotion_id,
                body.target_revision,
                owner=user,
                actor=f"user:{user or 'local'}",
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "promotion": result}

    @router.post("/{skill_id}/invoke")
    async def invoke_skill(request: Request, skill_id: str):
        """Build a skill-pinned prompt for slash-command invocation.

        This is intentionally server-side so availability, ownership, and usage
        accounting use the same rules as the SkillsManager.
        """
        user = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        request_text = (body.get("request") or "").strip() if isinstance(body, dict) else ""

        invokable = {
            s.get("name"): s for s in skills_manager.index_for(owner=user)
            if (s.get("name") or "").strip()
        }
        match = invokable.get(skill_id)
        if not match:
            raise HTTPException(404, "Skill is not available for slash invocation")

        name = match.get("name")
        md = skills_manager.read_published_skill_md(name, owner=user)
        if md is None:
            raise HTTPException(404, "Skill source unavailable")

        skills_manager.record_use(
            match.get("skill_id") or name,
            owner=user,
            revision=match.get("revision"),
        )
        message = (
            "Apply the skill below to my request, following its Procedure / Pitfalls / Verification.\n\n"
            f"--- BEGIN SKILL ---\n{md}\n--- END SKILL ---\n\n"
            + (f"Request: {request_text}" if request_text else "Request: (use the skill as appropriate)")
        )
        return {
            "ok": True,
            "type": "skill",
            "name": name,
            "command": f"/{name}",
            "message": message,
        }

    @router.post("/{skill_id}/feedback")
    async def record_skill_feedback(
        request: Request,
        skill_id: str,
        body: SkillOutcomeRequest,
    ):
        user = _owner(request)
        match = next(
            (
                item for item in skills_manager.load(owner=user)
                if item.get("name") == skill_id or item.get("skill_id") == skill_id
            ),
            None,
        )
        if not match:
            raise HTTPException(404, "Skill not found")
        stable_id = match.get("skill_id") or match.get("name")
        if body.signal == "success":
            skills_manager.record_success(stable_id, user)
        elif body.signal == "failure":
            skills_manager.record_failure(stable_id, user)
        elif body.signal == "correction":
            skills_manager.record_correction(
                stable_id,
                user,
                replacement_skill_id=body.replacement_skill_id,
            )
        elif body.signal == "contradiction":
            skills_manager.record_contradiction(stable_id, user)
        else:
            skills_manager.record_mismatch(stable_id, user)
        return {"ok": True, "signal": body.signal}

    @router.get("/{skill_id}")
    async def get_skill(request: Request, skill_id: str):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        for sk in skills:
            if sk.get("name") == skill_id or sk.get("id") == skill_id:
                return sk
        raise HTTPException(404, "Skill not found")

    @router.get("/{skill_id}/markdown")
    async def get_skill_markdown(request: Request, skill_id: str):
        """Return the raw SKILL.md text — used by the slash-invocation flow
        and the editor's 'view source' affordance."""
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        md = skills_manager.read_skill_md(match.get("name"), owner=user)
        if md is None:
            raise HTTPException(404, "Skill source unavailable (legacy entry?)")
        return {"name": match.get("name"), "markdown": md}

    @router.post("/{skill_id}/test")
    async def test_skill(request: Request, skill_id: str):
        """Kick off a background skill test (agent run + LLM judge). Returns
        immediately; the run executes server-side so it survives the modal being
        closed. Poll GET /{skill_id}/test-status for progress + verdict.
        On completion it records the verdict and nudges the skill's confidence
        to match (pass→0.95, needs_work→0.6, fail→0.4; inconclusive/unknown leave
        it untouched). It never changes the skill's published/draft STATUS."""
        import time as _time
        import asyncio as _asyncio

        user = _owner(request)
        body = await request.json()
        task = (body.get("task") or "").strip()

        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        name = match.get("name")
        md = skills_manager.read_skill_md(name, owner=user) or ""

        if not task:
            task = _skill_test_task(match)

        try:
            route = _selected_skill_route(
                owner=user,
                model_spec=(body.get("model") or "").strip() or None,
                endpoint_id=(body.get("endpoint_id") or "").strip() or None,
                model_route_id=(body.get("model_route_id") or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        model = route.provider_model_id

        key = (user or "", name)
        existing_handle = _skill_test_handles.get(key)
        if existing_handle and not existing_handle.done():
            raise HTTPException(409, "A test for this skill is already running")
        _skill_test_jobs[key] = {
            "status": "running",
            "task": task,
            "model": model,
            "skill": name,
            "started": _time.time(),
            "log": [{"type": "skill_test_start", "task": task, "skill": name, "model": model}],
            "verdict": None,
        }
        _persist_skill_job_state()
        handle = _asyncio.create_task(_run_skill_test_job(
            key, name, md, task, route, user, skills_manager,
        ))
        track_skill_test_handle(key, handle)
        return {"ok": True, "status": "running", "skill": name, "model": model}

    @router.get("/{skill_id}/test-status")
    async def test_skill_status(request: Request, skill_id: str):
        """Current background-test state for a skill (status / log / verdict)."""
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        name = (match or {}).get("name", skill_id)
        job = _skill_test_jobs.get((user or "", name))
        if not job:
            return {"status": "none"}
        return {
            "status": job["status"],
            "task": job.get("task"),
            "model": job.get("model"),
            "log": job.get("log", []),
            "verdict": job.get("verdict"),
        }

    @router.post("/{skill_id}/test-cancel")
    async def cancel_skill_test(request: Request, skill_id: str):
        import asyncio as _asyncio

        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        name = (match or {}).get("name", skill_id)
        key = (user or "", name)
        handle = _skill_test_handles.get(key)
        if not handle or handle.done():
            return {"ok": False, "status": (_skill_test_jobs.get(key) or {}).get("status", "none")}
        handle.cancel()
        await _asyncio.gather(handle, return_exceptions=True)
        job = _skill_test_jobs.get(key) or {}
        job["status"] = "cancelled"
        job["verdict"] = {
            "verdict": "manual_verification_required",
            "confidence": 0,
            "summary": "Skill test cancelled; skill was not changed.",
            "issues": [],
        }
        _persist_skill_job_state()
        return {"ok": True, "status": "cancelled"}

    @router.post("/audit-all")
    async def audit_all_skills(request: Request):
        """Kick off a background audit of skills: each is tested + judged; if it
        needs work the model self-edits and retries; if a teacher model is
        configured it escalates; a skill that still fails is demoted to draft
        (never deleted). Poll GET /audit-status. Body:
        {scope: 'drafts'|'unchecked'|'all', names?: [...], skip_audited?: bool}. Default 'all'
        means every visible skill, including already-published skills, so audit
        can publish or demote according to the confidence threshold."""
        import asyncio as _asyncio
        import time as _time

        user = _owner(request)
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        scope = (body.get("scope") or "all").lower()
        requested_names = body.get("names")
        skip_audited = bool(body.get("skip_audited"))

        key = (user or "",)
        existing = _skill_audit_jobs.get(key)
        existing_handle = _skill_audit_handles.get(key)
        if existing_handle and not existing_handle.done():
            return {
                "ok": True, "status": "running", "total": existing.get("total", 0),
                "done": existing.get("done", 0), "model": existing.get("model"),
            }

        # Worker model (Default, normalized) + optional teacher — shared resolver.
        try:
            worker_route, teacher_route = _resolve_audit_models(owner=user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        model = worker_route.provider_model_id

        skills = skills_manager.load(owner=user)
        by_name = {s.get("name"): s for s in skills if s.get("name")}
        if isinstance(requested_names, list):
            names = []
            seen = set()
            for raw in requested_names:
                nm = str(raw or "").strip()
                if not nm or nm in seen or nm not in by_name:
                    continue
                if scope not in ("all", "selected") and (by_name[nm].get("status") or "draft") == "published":
                    continue
                if skip_audited and by_name[nm].get("audit_verdict"):
                    continue
                names.append(nm)
                seen.add(nm)
            scope = "selected" if requested_names else scope
        elif scope == "all":
            names = [
                s.get("name") for s in skills
                if s.get("name") and (not skip_audited or not s.get("audit_verdict"))
            ]
        else:
            scope = "unchecked" if scope == "drafts" else scope
            names = [
                s.get("name") for s in skills
                if s.get("name")
                and (s.get("status") or "draft") != "published"
                and not s.get("audit_verdict")
            ]
        if not names:
            return {"ok": True, "status": "done", "total": 0, "results": [], "log": ["No skills to audit."]}

        _skill_audit_jobs[key] = {
            "status": "running", "scope": scope, "model": model,
            "teacher": teacher_route.provider_model_id if teacher_route else None,
            "total": len(names), "done": 0, "current": None,
            "results": [], "log": [
                f"Auditing {len(names)} skill(s) with {model}"
                + (f"; teacher {teacher_route.provider_model_id}" if teacher_route else "")
            ],
            "started": _time.time(), "cancel": False,
        }
        _persist_skill_job_state()
        handle = _asyncio.create_task(
            _run_audit_all_job(
                key, skills_manager, names, worker_route, teacher_route, user
            )
        )
        track_skill_audit_handle(key, handle)
        return {"ok": True, "status": "running", "total": len(names), "model": model}

    @router.get("/audit-all/status")
    async def audit_status(request: Request):
        user = _owner(request)
        job = _skill_audit_jobs.get((user or "",))
        if not job:
            return {"status": "none"}
        return {
            "status": job["status"], "scope": job.get("scope"),
            "total": job.get("total", 0), "done": job.get("done", 0),
            "current": job.get("current"), "model": job.get("model"), "teacher": job.get("teacher"),
            "results": job.get("results", []), "log": job.get("log", []),
            "started": job.get("started"), "finished": job.get("finished"),
        }

    @router.post("/audit-all/cancel")
    async def audit_cancel(request: Request):
        import asyncio as _asyncio

        user = _owner(request)
        key = (user or "",)
        job = _skill_audit_jobs.get(key)
        if job:
            job["cancel"] = True
            handle = _skill_audit_handles.get(key)
            if handle and not handle.done():
                handle.cancel()
                await _asyncio.gather(handle, return_exceptions=True)
            job["status"] = "cancelled"
            job["current"] = None
            _persist_skill_job_state()
        return {"ok": True, "status": "cancelled" if job else "none"}

    @router.post("/{skill_id}/markdown")
    async def save_skill_markdown(request: Request, skill_id: str):
        """Replace SKILL.md with new raw content. Parses + validates first."""
        from services.memory.skill_format import Skill
        user = _owner(request)
        body = await request.json()
        new_content = body.get("markdown")
        if not isinstance(new_content, str) or not new_content.strip():
            raise HTTPException(400, "markdown is required")
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        try:
            sk = Skill.from_markdown(new_content)
        except Exception as e:
            raise HTTPException(400, f"Could not parse SKILL.md: {e}")
        # Never rename on save: a changed `name` in the markdown would move
        # the skill dir (update_skill) and orphan the original id, so a later
        # delete 404s (#1333). Pin to the stored name, like _apply_skill_md.
        sk.name = match.get("name")
        if not sk.owner:
            sk.owner = match.get("owner") or user
        sk.status = "draft"
        ok = skills_manager.update_skill(match.get("name"), {
            "name": sk.name,
            "description": sk.description,
            "version": sk.version,
            "category": sk.category,
            "tags": sk.tags,
            "platforms": sk.platforms,
            "requires_toolsets": sk.requires_toolsets,
            "fallback_for_toolsets": sk.fallback_for_toolsets,
            "status": "draft",
            "confidence": sk.confidence,
            "source": sk.source,
            "teacher_model": sk.teacher_model,
            "owner": sk.owner,
            "when_to_use": sk.when_to_use,
            "procedure": sk.procedure,
            "pitfalls": sk.pitfalls,
            "verification": sk.verification,
            "body_extra": sk.body_extra,
        }, owner=user)
        if not ok:
            raise HTTPException(500, "Update failed")
        # Manual markdown edits can create or substantially rewrite a draft
        # skill without going through /add. Treat unaudited saves as new audit
        # candidates so the event-driven Skills Audit pipeline still runs.
        if not match.get("audit_verdict"):
            _fire_skill_added(user)
        return {"ok": True, "name": sk.name}

    @router.put("/{skill_id}")
    async def update_skill(request: Request, skill_id: str, body: SkillUpdateRequest):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)

        updates = body.model_dump(exclude_none=True)
        if not updates:
            return {"ok": True}
        expected_revision = updates.pop("expected_revision", None)
        expected_hash = updates.pop("expected_hash", None)
        waiver_reason = str(updates.pop("waiver_reason", "") or "").strip()
        desired_status = updates.pop("status", None)
        if desired_status == "draft":
            updates["status"] = "draft"

        ok = True
        if updates:
            ok = skills_manager.update_skill(match.get("name"), updates, owner=user)
        if ok and desired_status == "published":
            if waiver_reason:
                require_admin(request)
            readiness = skills_manager.publish_readiness(match.get("skill_id"), owner=user)
            if not readiness.get("ready") and not waiver_reason:
                raise HTTPException(
                    409,
                    {"message": "Audit this exact revision before publishing", **readiness},
                )
            ok = skills_manager.publish_skill(
                match.get("skill_id"),
                owner=user,
                expected_revision=expected_revision or match.get("revision"),
                expected_hash=expected_hash or match.get("content_hash"),
                publisher=(
                    f"admin:{user or 'local'}"
                    if waiver_reason
                    else f"user:{user or 'local'}"
                ),
                waiver_reason=waiver_reason,
                allow_waiver=bool(waiver_reason),
            )
        if not ok:
            raise HTTPException(409, "Skill changed before the update completed")
        if not match.get("audit_verdict"):
            _fire_skill_added(user)
        return {"ok": True}

    @router.delete("/{skill_id}")
    async def delete_skill(request: Request, skill_id: str):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        ok = skills_manager.delete_skill(match.get("name"), owner=user)
        if not ok:
            raise HTTPException(404, "Skill not found")
        return {"ok": True}

    @router.post("/search")
    async def search_skills(request: Request):
        body = await request.json()
        query = body.get("query", "")
        if not query.strip():
            raise HTTPException(400, "query is required")
        user = _owner(request)
        skills = skills_manager.load_published(owner=user)
        results = skills_manager.get_relevant_skills(query, skills, max_items=10)
        return {"skills": results, "query": query, "count": len(results)}

    return router
