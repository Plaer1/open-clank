"""First-party Open Clank Hexes contract engine.

The data model and several structural check ideas were informed by Henxels
v0.11.1 (MIT, https://github.com/benquemax/henxels, pinned in
``.references/henxels``).  This module is an Open Clank implementation: it has
no runtime import, executable, package, or network dependency on Henxels.

New contracts use ``.clanker/hexes/contract.yaml`` with ``hexes:`` entries headed by ``hex:``.  The
loader accepts the former ``henxels:``/``henxel:`` vocabulary only as an
explicit compatibility input so existing activated projects can migrate
without losing their exact contract history.
"""

from __future__ import annotations

import datetime as _datetime
import difflib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import types
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import yaml
from src.clanker_paths import GLOBAL_HEX_CONTRACT_PATHS, is_reference_path, project_root_for_contract


ENGINE_VERSION = "open-clank-hexes/1"
ENGINE_VERSION_V2 = "open-clank-hexes/2"
CONTRACT_VERSION = "open-clank-hexes/v2"
SUPPORTED_CONTRACT_VERSIONS = frozenset({"open-clank-hexes/v1", CONTRACT_VERSION})
BLOCK = "block"
WARN = "warn"
MAX_DISCOVERED_FILES = 100_000
DEFAULT_EXCLUDES = frozenset(
    {
        ".git",
        ".references",
        ".venv",
        "venv",
        "ENV",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "dist",
        "build",
        "data",
        "_temp",
    }
)
_NEW_HEADLINES = ("hex", "rule", "must", "description")
_LEGACY_HEADLINES = ("henxel",)
_CONTEXT_KEYS = ("why", "context", "comment")
_RESERVED = set(_NEW_HEADLINES + _LEGACY_HEADLINES + _CONTEXT_KEYS) | {
    "in",
    "level",
    "except",
}
_STAGES = ("pre_commit", "pre_push")
_INJECTABLE = ("param", "scope", "file", "root", "settings", "diff")
_SETTING_NAMES = (
    "ask_me_before_staging",
    "confirm_before_push",
    "confirm_before_deleting",
    "warn_about_similar_files",
    "warn_about_large_files",
)


class HexContractError(ValueError):
    """A Hexes contract is absent, ambiguous, unsafe, or malformed."""


@dataclass(frozen=True)
class HexFinding:
    level: str
    hex: str
    path: str = ""
    message: str = ""
    reason: Optional[str] = None
    steer: Optional[str] = None
    fix: Optional[str] = None
    details: tuple[str, ...] = ()

    @property
    def is_block(self) -> bool:
        return self.level == BLOCK

    def to_wire(self) -> dict[str, Any]:
        # ``henxel`` is a bounded compatibility response key for old clients;
        # new clients and all first-party UI use ``hex``.
        return {
            "level": self.level,
            "hex": self.hex,
            "henxel": self.hex,
            "path": self.path,
            "message": self.message,
            "reason": self.reason,
            "steer": self.steer,
            "fix": self.fix,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class HexRule:
    text: str
    locations: tuple[str, ...] = ("./*",)
    excludes: tuple[str, ...] = ()
    level: str = BLOCK
    why: str = ""
    checks: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HexContract:
    settings: Mapping[str, Any]
    hexes: tuple[HexRule, ...]
    imports: tuple[str, ...]
    path: Path
    raw: Mapping[str, Any]
    vocabulary: str
    version: str


@dataclass(frozen=True)
class CandidateDiff:
    root: Path
    source_root: Path
    added: frozenset[str] = frozenset()
    modified: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()
    old_bytes: Mapping[str, Optional[bytes]] = field(default_factory=dict)
    new_bytes: Mapping[str, Optional[bytes]] = field(default_factory=dict)

    @property
    def changed(self) -> frozenset[str]:
        return self.added | self.modified | self.deleted

    def old_text(self, relative: str) -> Optional[str]:
        if relative in self.old_bytes:
            payload = self.old_bytes[relative]
            return None if payload is None else payload.decode("utf-8", errors="replace")
        return _read_text(self.source_root / relative)

    def new_text(self, relative: str) -> Optional[str]:
        if relative in self.new_bytes:
            payload = self.new_bytes[relative]
            return None if payload is None else payload.decode("utf-8", errors="replace")
        return _read_text(self.root / relative)


@dataclass(frozen=True)
class _Statement:
    name: str
    fn: Callable[..., Any]
    stage: Optional[str]
    params: tuple[str, ...]
    per_file: bool
    help: str
    builtin: bool


_STATEMENTS: dict[str, _Statement] = {}
_BUILTINS: set[str] = set()
_CUSTOM_NAMES: set[str] = set()
_COLLISIONS: set[str] = set()


def statement(
    name: str,
    *,
    stage: Optional[str] = None,
    help: Optional[str] = None,
    builtin: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a declarative check with name-based argument injection."""

    if stage is not None and stage not in _STAGES:
        raise ValueError(f"unknown Hexes stage {stage!r}")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        params = tuple(inspect.signature(fn).parameters)
        unknown = [param for param in params if param not in _INJECTABLE]
        if unknown:
            raise TypeError(f"Hexes check {name!r} has unsupported parameters: {unknown}")
        definition = _Statement(
            name=name,
            fn=fn,
            stage=stage,
            params=params,
            per_file="file" in params,
            help=help or (inspect.getdoc(fn) or "").split("\n", 1)[0],
            builtin=builtin,
        )
        if builtin:
            _BUILTINS.add(name)
            _STATEMENTS[name] = definition
        elif name in _BUILTINS:
            _COLLISIONS.add(name)
        else:
            _STATEMENTS[name] = definition
            _CUSTOM_NAMES.add(name)
        return fn

    return decorate


def _reset_custom_statements() -> None:
    for name in tuple(_CUSTOM_NAMES):
        _STATEMENTS.pop(name, None)
    _CUSTOM_NAMES.clear()
    _COLLISIONS.clear()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


@lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    value = str(pattern).replace("\\", "/")
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "*":
            if index + 1 < len(value) and value[index + 1] == "*":
                if index + 2 < len(value) and value[index + 2] == "/":
                    output.append("(?:.*/)?")
                    index += 3
                else:
                    output.append(".*")
                    index += 2
            else:
                output.append("[^/]*")
                index += 1
        elif char == "?":
            output.append("[^/]")
            index += 1
        else:
            output.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(output) + "$")


def glob_match(pattern: str, path: str) -> bool:
    return bool(_glob_regex(str(pattern)).match(str(path).replace("\\", "/")))


@dataclass(frozen=True)
class _Location:
    raw: str
    base: str
    kind: str
    recursive: bool = False
    target: str = ""
    pattern: str = ""

    def matches(self, path: str) -> bool:
        path = path.replace("\\", "/")
        if self.kind == "file":
            return path == self.target
        if self.kind == "glob":
            return glob_match(self.pattern, path)
        if self.recursive:
            return not self.base or path == self.base or path.startswith(self.base + "/")
        return (path.rsplit("/", 1)[0] if "/" in path else "") == self.base

    def governs(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip("/")
        if self.matches(normalized):
            return True
        if self.kind != "folder":
            return False
        return (not self.base and self.recursive) or normalized == self.base or normalized.startswith(self.base + "/")


def _location(spec: Any) -> _Location:
    raw = str(spec)
    value = raw.strip()
    if value.startswith("./"):
        value = value[2:]
    elif value == ".":
        value = ""
    recursive = False
    if value.endswith("/**"):
        recursive, value = True, value[:-3]
    elif value.endswith("/*"):
        recursive, value = True, value[:-2]
    elif value == "*":
        recursive, value = True, ""
    value = value.strip("/")
    if recursive:
        return _Location(raw, value, "folder", recursive=True)
    if "*" in value or "?" in value:
        base = value.rsplit("/", 1)[0] if "/" in value else ""
        return _Location(raw, base, "glob", pattern=value)
    if not value:
        return _Location(raw, "", "folder")
    if "." in value.rsplit("/", 1)[-1]:
        base = value.rsplit("/", 1)[0] if "/" in value else ""
        return _Location(raw, base, "file", target=value)
    return _Location(raw, value, "folder")


@dataclass
class HexScope:
    root: Path
    locations: list[str]
    files: list[str]
    all_files: list[str]
    settings: Mapping[str, Any]

    def read_text(self, relative: str) -> Optional[str]:
        return _read_text(self.root / relative)

    def line_count(self, relative: str) -> int:
        text = self.read_text(relative)
        return 0 if text is None else len(text.splitlines())

    def exists(self, relative: str) -> bool:
        return (self.root / relative).exists()

    def is_dir(self, relative: str) -> bool:
        return (self.root / relative).is_dir()

    def subfolders_of(self, location: str) -> list[str]:
        base = self.root / location if location else self.root
        if not base.is_dir():
            return []
        return sorted(
            entry.name
            for entry in os.scandir(base)
            if entry.is_dir(follow_symlinks=False)
            and entry.name not in DEFAULT_EXCLUDES
            and not entry.name.startswith(".")
        )


def _scope(rule: HexRule, files: Sequence[str], root: Path, settings: Mapping[str, Any]) -> HexScope:
    locations = [_location(value) for value in (rule.locations or ("./*",))]
    exclusions = [_location(value) for value in rule.excludes]
    selected = [
        value
        for value in files
        if any(location.matches(value) for location in locations)
        and not any(exclusion.matches(value) for exclusion in exclusions)
    ]
    return HexScope(
        root=root,
        locations=list(dict.fromkeys(location.base for location in locations)),
        files=selected,
        all_files=list(files),
        settings=settings,
    )


def load_contract(path: str | os.PathLike[str]) -> HexContract:
    contract_path = Path(path)
    if not contract_path.is_file():
        raise HexContractError(f"Hexes contract not found: {contract_path}")
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HexContractError(f"could not parse Hexes contract: {exc}") from exc
    if not isinstance(raw, dict):
        raise HexContractError("Hexes contract must be a mapping")
    has_new = "hexes" in raw
    has_legacy = "henxels" in raw
    if has_new and has_legacy:
        raise HexContractError("Hexes contract cannot mix hexes and legacy henxels lists")
    vocabulary = "hexes" if has_new else "legacy_henxels" if has_legacy else "hexes"
    values = raw.get("hexes" if has_new else "henxels", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        raise HexContractError("Hexes contract rules must be a list")
    rules: list[HexRule] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise HexContractError(f"Hexes rule {index} must be a mapping")
        headlines = _NEW_HEADLINES + (_LEGACY_HEADLINES if vocabulary == "legacy_henxels" else ())
        text = next((str(value[key]).strip() for key in headlines if key in value), "")
        if not text:
            raise HexContractError(f"Hexes rule {index} needs a non-empty hex sentence")
        why = next((str(value[key]).strip() for key in _CONTEXT_KEYS if key in value), "")
        raw_locations = value.get("in")
        locations = tuple(str(item).strip() for item in _as_list(raw_locations)) or ("./*",)
        excludes = tuple(str(item).strip() for item in _as_list(value.get("except")))
        checks = {key: item for key, item in value.items() if key not in _RESERVED}
        rules.append(
            HexRule(
                text=text,
                locations=locations,
                excludes=excludes,
                level=WARN if str(value.get("level", "")).lower() == WARN else BLOCK,
                why=why,
                checks=checks,
            )
        )
    settings = raw.get("settings") or {}
    if not isinstance(settings, dict):
        raise HexContractError("Hexes settings must be a mapping")
    imports = tuple(str(item).strip() for item in _as_list(raw.get("imports")) if str(item).strip())
    declared = str(raw.get("contract") or "").strip()
    if has_new and declared and declared not in SUPPORTED_CONTRACT_VERSIONS:
        raise HexContractError(f"unsupported Hexes contract version: {declared}")
    version = declared or ("legacy" if vocabulary == "legacy_henxels" else CONTRACT_VERSION)
    return HexContract(settings, tuple(rules), imports, contract_path, raw, vocabulary, version)


def find_contract(start: str | os.PathLike[str] = ".") -> Optional[Path]:
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        for relative in GLOBAL_HEX_CONTRACT_PATHS:
            canonical = directory / relative
            if canonical.is_file():
                return canonical
        primary = directory / ".hex"
        if primary.is_file():
            return primary
        legacy = [directory / name for name in ("henxels.yaml", ".henxels.yaml", "henxels.yml", ".henxels.yml")]
        present = [path for path in legacy if path.is_file()]
        if len(present) > 1:
            raise HexContractError(f"multiple legacy project contracts in {directory}")
        if present:
            return present[0]
        if (directory / ".git").exists():
            return None
    return None


def discover(root: str | os.PathLike[str]) -> list[str]:
    base = Path(root)
    values: Optional[list[str]] = None
    if (base / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=base,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            values = [item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item]
    if values is None:
        values = []
        for directory, names, files in os.walk(base, followlinks=False):
            names[:] = [name for name in names if name not in DEFAULT_EXCLUDES
                        and not is_reference_path((Path(directory) / name).relative_to(base))]
            parent = Path(directory)
            values.extend((parent / name).relative_to(base).as_posix() for name in files)
            if len(values) > MAX_DISCOVERED_FILES:
                raise HexContractError("Hexes file set exceeds the 100,000-file limit")
    filtered = sorted(
        set(
            value.replace("\\", "/")
            for value in values
            if not any(part in DEFAULT_EXCLUDES for part in Path(value).parts)
            and not is_reference_path(value)
        )
    )
    if len(filtered) > MAX_DISCOVERED_FILES:
        raise HexContractError("Hexes file set exceeds the 100,000-file limit")
    return filtered


def _install_import_aliases(*, legacy: bool) -> None:
    module = types.ModuleType("hexes")
    module.statement = statement
    sys.modules["hexes"] = module
    if legacy:
        compatibility = types.ModuleType("henxels")
        compatibility.statement = statement
        sys.modules["henxels"] = compatibility


def custom_check_references(contract: HexContract, root: str | os.PathLike[str]) -> list[str]:
    """Return workspace-relative checks shared by the loader and trust manifest.

    Only the selected contract contributes its adjacent checks. Discovery is
    read-only and never executes modules or transfers trust between layouts.
    """
    base = Path(root).resolve()
    references = list(contract.imports)
    if contract.vocabulary == "hexes":
        if any(contract.path.resolve().as_posix().endswith("/" + path) for path in GLOBAL_HEX_CONTRACT_PATHS):
            checks = contract.path.resolve().parent / "checks"
            references.extend(
                path.relative_to(base).as_posix()
                for path in sorted(checks.glob("*.py")) if not path.name.startswith("_")
            )
        if (base / "hexes_checks.py").is_file():
            references.append("hexes_checks.py")
        local = base / ".hexes"
    else:
        if (base / "henxels_checks.py").is_file():
            references.append("henxels_checks.py")
        local = base / ".henxels"
    if local.is_dir():
        references.extend(path.relative_to(base).as_posix() for path in sorted(local.glob("*.py")) if not path.name.startswith("_"))
    normalized = []
    for reference in dict.fromkeys(references):
        if not (reference.endswith(".py") or "/" in reference or "\\" in reference):
            module = Path(*reference.split("."))
            candidates = [base / module.with_suffix(".py"), base / module / "__init__.py"]
            present = [path for path in candidates if path.is_file()]
            if len(present) == 1:
                reference = present[0].relative_to(base).as_posix()
        normalized.append(reference)
    return list(dict.fromkeys(normalized))


def apply_imports(contract: HexContract, *, root: str | os.PathLike[str]) -> list[str]:
    _reset_custom_statements()
    _install_import_aliases(legacy=contract.vocabulary == "legacy_henxels")
    base = Path(root).resolve()
    failed: list[str] = []
    for index, reference in enumerate(custom_check_references(contract, base)):
        candidate = (base / reference).resolve(strict=False)
        try:
            candidate.relative_to(base)
        except ValueError:
            failed.append(reference)
            continue
        if not candidate.is_file() or candidate.suffix != ".py":
            failed.append(reference)
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"_openclank_hex_check_{index}", candidate)
            if spec is None or spec.loader is None:
                raise ImportError(reference)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            failed.append(reference)
    return failed


def custom_collisions() -> list[str]:
    findings = [f"custom check {name!r} collides with a built-in Hexes check and was ignored" for name in sorted(_COLLISIONS)]
    findings.extend(
        f"{name!r} is a Hexes setting, not a custom check"
        for name in _SETTING_NAMES
        if name in _CUSTOM_NAMES
    )
    return findings


def run_contract(
    contract: HexContract,
    root: str | os.PathLike[str],
    files: Optional[Sequence[str]] = None,
    *,
    diff: Optional[CandidateDiff] = None,
) -> list[HexFinding]:
    base = Path(root)
    all_files = list(files) if files is not None else discover(base)
    findings: list[HexFinding] = []
    for rule in contract.hexes:
        scope = _scope(rule, all_files, base, contract.settings)
        instructions: list[str] = []
        for name, parameter in rule.checks.items():
            definition = _STATEMENTS.get(name)
            if definition is None:
                instructions.append(f"unknown Hexes check {name!r}; define or remove it")
                continue
            if definition.stage is not None:
                continue
            instructions.extend(_invoke(definition, parameter, scope, rule.text, diff))
        if instructions:
            findings.append(
                HexFinding(
                    level=rule.level,
                    hex=rule.text,
                    reason=rule.why or None,
                    steer="change the matching rule in .hex, then review and reactivate the exact contract",
                    details=tuple(instructions),
                )
            )
    return findings


def _invoke(
    definition: _Statement,
    parameter: Any,
    scope: HexScope,
    sentence: str,
    diff: Optional[CandidateDiff],
) -> list[str]:
    available = {
        "param": parameter,
        "scope": scope,
        "root": scope.root,
        "settings": scope.settings,
        "diff": diff,
    }

    def call(extra: Optional[Mapping[str, Any]] = None) -> Any:
        values = {**available, **(extra or {})}
        try:
            return definition.fn(*[values[name] for name in definition.params])
        except Exception as exc:
            return f"check {definition.name!r} errored: {exc}"

    output: list[str] = []
    if definition.per_file:
        for relative in scope.files:
            output.extend(_normalize_result(call({"file": relative}), relative, sentence))
    else:
        output.extend(_normalize_result(call(), None, sentence))
    return output


def _normalize_result(result: Any, relative: Optional[str], sentence: str) -> list[str]:
    if result is None or result is True:
        return []
    if result is False:
        return [f"{relative} — {sentence}" if relative else sentence]
    if isinstance(result, str):
        return [f"{relative} — {result}" if relative and relative not in result else result]
    if isinstance(result, (list, tuple)):
        output: list[str] = []
        for item in result:
            output.extend(_normalize_result(item, relative, sentence))
        return output
    return []


def stage_commands(contract: HexContract, stage: str) -> list[str]:
    commands: list[str] = []
    for rule in contract.hexes:
        for name, parameter in rule.checks.items():
            definition = _STATEMENTS.get(name)
            if definition is not None and definition.stage == stage:
                commands.extend(str(item) for item in _as_list(parameter))
    return commands


def evaluate_contract(
    contract_path: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str],
    files: Optional[Sequence[str]] = None,
    diff: Optional[CandidateDiff] = None,
    stage: str = "check",
    command_root: Optional[str | os.PathLike[str]] = None,
    policy_root: Optional[str | os.PathLike[str]] = None,
) -> dict[str, Any]:
    if stage not in {"check", "pre-commit", "pre-push"}:
        raise HexContractError(f"unsupported Hexes stage: {stage}")
    contract = load_contract(contract_path)
    failed_imports = apply_imports(contract, root=policy_root if policy_root is not None else root)
    if failed_imports:
        raise HexContractError("Hexes custom checks failed to load: " + ", ".join(failed_imports))
    findings = run_contract(contract, root, files, diff=diff)
    if stage == "pre-commit":
        findings.extend(_deletion_findings(contract, diff))
        findings.extend(_large_file_findings(contract, root, diff))
        findings.extend(_similarity_findings(contract, root, diff))
        if not any(finding.is_block for finding in findings):
            findings.extend(_command_findings(contract, "pre_commit", Path(command_root or root)))
    elif stage == "pre-push":
        findings.extend(_command_findings(contract, "pre_push", Path(command_root or root)))
        if bool(contract.settings.get("confirm_before_push")):
            findings.append(
                HexFinding(
                    BLOCK,
                    "Pushing requires an explicit Open Clank Hexes blessing",
                    steer="review the outgoing commits and issue a consume-once push blessing",
                )
            )
    collisions = custom_collisions()
    if collisions:
        findings.append(
            HexFinding(
                BLOCK,
                "Custom checks may not replace built-in Hexes checks",
                details=tuple(collisions),
            )
        )
    payload = [finding.to_wire() for finding in findings]
    blocks = [finding for finding in payload if finding["level"] == BLOCK]
    warnings = [finding for finding in payload if finding["level"] != BLOCK]
    return {
        "allowed": not blocks,
        "engine_version": ENGINE_VERSION_V2 if contract.version == CONTRACT_VERSION else ENGINE_VERSION,
        "contract_version": contract.version,
        "vocabulary": contract.vocabulary,
        "findings": payload,
        "warnings": warnings,
    }


def explain_contract(contract: HexContract, path: str) -> list[dict[str, Any]]:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    output: list[dict[str, Any]] = []
    for rule in contract.hexes:
        locations = [_location(item) for item in rule.locations]
        exclusions = [_location(item) for item in rule.excludes]
        if any(location.governs(normalized) for location in locations) and not any(exclusion.governs(normalized) for exclusion in exclusions):
            output.append(
                {
                    "hex": rule.text,
                    "level": rule.level,
                    "in": list(rule.locations),
                    "except": list(rule.excludes),
                    "why": rule.why,
                    "checks": dict(rule.checks),
                }
            )
    return output


def render_agent_digest(contract: HexContract) -> str:
    root = project_root_for_contract(contract.path)
    source = contract.path.resolve().relative_to(root).as_posix()
    checks = contract.path.resolve().parent.relative_to(root) / "checks"
    lines = [
        "<!-- openclank-hexes:begin -->",
        "## The contract (Open Clank Hexes)",
        "",
        f"_Generated from `{source}` by `openclank hex sync`; edit the canonical contract, not this block._",
        "",
        "Before creating or changing a file, run `openclank hex explain <path>`.",
        "The exact activated contract hash is the authority for project mutations.",
        "",
        "### Rules",
        "",
    ]
    for rule in contract.hexes:
        suffix = f" (in {', '.join(rule.locations)})" if rule.locations else ""
        warning = " _(warn)_" if rule.level == WARN else ""
        lines.append(f"- {rule.text}{suffix}{warning}")
        if rule.why:
            lines.append(f"  ↳ {rule.why}")
    behaviors: list[str] = []
    if contract.settings.get("confirm_before_deleting"):
        behaviors.append("deleting files or removing many lines requires an explicit consume-once blessing")
    if contract.settings.get("confirm_before_push"):
        behaviors.append("pushing requires an explicit consume-once blessing")
    if contract.settings.get("ask_me_before_staging"):
        behaviors.append("staging requires explicit owner confirmation")
    if behaviors:
        lines.extend(["", "### Behaviours", ""])
        lines.extend(f"- {item}" for item in behaviors)
    lines.extend(
        [
            "",
            f"Custom checks live in `{checks.as_posix()}/*.py` and may not" if source in GLOBAL_HEX_CONTRACT_PATHS else "Custom checks use project imports, `hexes_checks.py` or `.hexes/*.py` and may not",
            "replace built-ins. Activated runtime mutations execute them in the contained",
            "policy worker; explicit local checks/hooks execute repository-owned checks.",
            "<!-- openclank-hexes:end -->",
            "",
        ]
    )
    return "\n".join(lines)


def replace_agent_digest(text: str, digest: str) -> str:
    patterns = (
        re.compile(r"<!-- openclank-hexes:begin -->.*?<!-- openclank-hexes:end -->\n?", re.DOTALL),
        re.compile(r"<!-- henxels:begin -->.*?<!-- henxels:end -->\n?", re.DOTALL),
    )
    result = text
    for pattern in patterns:
        if pattern.search(result):
            return pattern.sub(digest, result, count=1)
    return digest + ("\n" + result.lstrip() if result.strip() else "")


def _command_findings(contract: HexContract, stage: str, root: Path) -> list[HexFinding]:
    findings: list[HexFinding] = []
    for command in stage_commands(contract, stage):
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120,
                check=False,
            )
            code = result.returncode
            detail = (result.stdout or "")[-2048:]
        except subprocess.TimeoutExpired:
            code, detail = 124, "command timed out"
        if code:
            findings.append(
                HexFinding(
                    BLOCK,
                    f"`{command}` must pass before {stage.replace('_', ' ')}",
                    details=(f"contained command failed (exit {code}): {detail}".strip(),),
                )
            )
    return findings


def _deletion_findings(contract: HexContract, diff: Optional[CandidateDiff]) -> list[HexFinding]:
    raw = contract.settings.get("confirm_before_deleting")
    if not raw or diff is None:
        return []
    threshold = 5 if raw is True else int(raw.get("over_lines", 5)) if isinstance(raw, dict) else 5
    details = [f"delete {path}" for path in sorted(diff.deleted)]
    for path in sorted(diff.modified):
        old = diff.old_text(path)
        new = diff.new_text(path)
        if old is None or new is None:
            continue
        removed = max(0, len(old.splitlines()) - len(new.splitlines()))
        if removed > threshold:
            details.append(f"{path} removes {removed} lines (threshold {threshold})")
    return [] if not details else [
        HexFinding(
            BLOCK,
            "Destructive changes require an explicit Open Clank Hexes blessing",
            details=tuple(details),
            steer="review the exact staged deletion set and issue a consume-once delete blessing",
        )
    ]


def _large_file_findings(contract: HexContract, root: str | os.PathLike[str], diff: Optional[CandidateDiff]) -> list[HexFinding]:
    raw = contract.settings.get("warn_about_large_files")
    if not raw or diff is None:
        return []
    spec = raw if isinstance(raw, dict) else {"over": "8000 tokens", "ignore": []}
    threshold, unit = _parse_size(str(spec.get("over", "8000 tokens")))
    ignores = [str(item) for item in _as_list(spec.get("ignore"))]
    details: list[str] = []
    for path in sorted(diff.added | diff.modified):
        if any(glob_match(pattern, path) for pattern in ignores):
            continue
        new_bytes = getattr(diff, "new_bytes", {})
        payload = new_bytes.get(path) if path in new_bytes else _read_bytes(Path(root) / path)
        if payload is None:
            continue
        observed = _size_value(payload, unit)
        if observed > threshold:
            details.append(f"{path} — {observed} {unit}; consider splitting it below {threshold} {unit}")
    return [] if not details else [HexFinding(WARN, "Large changed files deserve a split review", details=tuple(details))]


def _similarity_findings(contract: HexContract, root: str | os.PathLike[str], diff: Optional[CandidateDiff]) -> list[HexFinding]:
    raw = contract.settings.get("warn_about_similar_files")
    if not raw or diff is None:
        return []
    spec = raw if isinstance(raw, dict) else {}
    threshold = float(spec.get("above", 0.85))
    at_most = int(spec.get("at_most", 20))
    ignores = [str(item) for item in _as_list(spec.get("ignore"))]
    all_files = discover(root)
    details: list[str] = []
    for changed in sorted(diff.added | diff.modified):
        if any(glob_match(pattern, changed) for pattern in ignores):
            continue
        new = diff.new_text(changed)
        if not new or len(new) < 80:
            continue
        for other in all_files:
            if other == changed or any(glob_match(pattern, other) for pattern in ignores):
                continue
            old = _read_text(Path(root) / other)
            if not old or len(old) < 80:
                continue
            score = difflib.SequenceMatcher(None, new, old, autojunk=True).ratio()
            if score >= threshold:
                details.append(f"{changed} resembles {other} ({score:.2f}); update/reuse instead of scattering duplicates")
                if len(details) >= at_most:
                    return [HexFinding(WARN, "Changed files should not duplicate existing knowledge", details=tuple(details))]
    return [] if not details else [HexFinding(WARN, "Changed files should not duplicate existing knowledge", details=tuple(details))]


def _parse_size(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"\s*(\d+)\s*(lines?|kb|mb|bytes?|tokens?)\s*", value, re.IGNORECASE)
    if not match:
        raise HexContractError(f"invalid large-file threshold: {value}")
    return int(match.group(1)), match.group(2).lower().rstrip("s")


def _size_value(payload: bytes, unit: str) -> int:
    if unit == "line":
        return len(payload.splitlines())
    if unit == "kb":
        return (len(payload) + 1023) // 1024
    if unit == "mb":
        return (len(payload) + 1024 * 1024 - 1) // (1024 * 1024)
    if unit == "token":
        return (len(payload.decode("utf-8", errors="replace")) + 3) // 4
    return len(payload)


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _file_matches(pattern: str, path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if pattern.startswith(".") and "*" not in pattern and "?" not in pattern:
        return name.endswith(pattern)
    return glob_match(pattern, name) or glob_match(pattern, path)


@statement("run_before_commit", stage="pre_commit", help="run a command before commit", builtin=True)
def _run_before_commit() -> None:
    return None


@statement("run_before_push", stage="pre_push", help="run a command before push", builtin=True)
def _run_before_push() -> None:
    return None


@statement("allowed_filetypes", help="files in scope use an allowed extension or glob", builtin=True)
def _allowed_filetypes(param: Any, scope: HexScope) -> list[str]:
    patterns = [str(item) for item in _as_list(param)]
    return [f"{path} — should be {' or '.join(patterns)}" for path in scope.files if not any(_file_matches(pattern, path) for pattern in patterns)]


@statement("untracked_only", help="project-relative directories stay out of the Git index", builtin=True)
def _untracked_only(param: Any, scope: HexScope, diff: Optional[CandidateDiff]) -> list[str]:
    """Check index paths, including ignored force-adds and submodule gitlinks.

    The candidate tree has no Git index. Its source root remains the authority;
    a candidate file's presence alone does not mean it is being tracked.
    """
    directories: list[str] = []
    for value in _as_list(param):
        if not isinstance(value, str):
            return ["configure untracked_only with project-relative directory paths"]
        path = value.removeprefix("./").rstrip("/")
        if (
            not path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(char in path for char in "\\*?[]")
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            return ["configure untracked_only with literal project-relative directory paths, not globs or traversal"]
        directories.append(path)
    if not directories:
        return ["configure untracked_only with at least one project-relative directory"]

    projection = getattr(diff, "git_index_paths", None) if diff is not None else None
    if projection is not None:
        state = getattr(diff, "git_index_state", None)
        if state not in {"git", "non-git"} or (state == "non-git" and projection) or any(not isinstance(path, str) for path in projection):
            return ["cannot verify untracked_only: native Git index projection is malformed"]
        prefixes = tuple(dict.fromkeys(directories))
        return [f"{path} — must stay untracked; remove it from the Git index while keeping the local reference"
                for path in sorted(projection) if any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)]
    root = Path(diff.source_root if diff is not None else scope.root).resolve()
    # Do not turn a non-Git workspace into an error or read reference contents.
    if not any((parent / ".git").exists() or (parent / ".git").is_symlink()
               for parent in (root, *root.parents)):
        return []
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "ls-files", "--cached", "-z", "--",
             *dict.fromkeys(directories)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ["cannot verify untracked_only: Git index is unavailable"]
    if result.returncode:
        return ["cannot verify untracked_only: Git index is unavailable"]
    paths = sorted({value.decode("utf-8", errors="surrogateescape")
                    for value in result.stdout.split(b"\0") if value})
    return [f"{path} — must stay untracked; remove it from the Git index while keeping the local reference"
            for path in paths]


@statement("required_files", help="required files exist in every scoped location", builtin=True)
def _required_files(param: Any, scope: HexScope) -> list[str]:
    return [f"create {location + '/' if location else ''}{name}" for location in scope.locations for name in _as_list(param) if not scope.exists(f"{location + '/' if location else ''}{name}")]


@statement("required_subfolders", help="required folders exist in every scoped location", builtin=True)
def _required_subfolders(param: Any, scope: HexScope) -> list[str]:
    return [f"create {location + '/' if location else ''}{name}/" for location in scope.locations for name in _as_list(param) if not scope.is_dir(f"{location + '/' if location else ''}{name}")]


@statement("only_these_subfolders", help="only named immediate subfolders may exist", builtin=True)
def _only_subfolders(param: Any, scope: HexScope) -> list[str]:
    allowed = {str(item) for item in _as_list(param)}
    return [f"{location + '/' if location else ''}{name}/ — remove or relocate" for location in scope.locations for name in scope.subfolders_of(location) if name not in allowed]


@statement("forbidden_files", help="forbidden file names or globs do not exist", builtin=True)
def _forbidden_files(param: Any, scope: HexScope) -> list[str]:
    patterns = [str(item).removeprefix("./") for item in _as_list(param)]
    return [f"{path} — forbidden; remove or relocate it" for path in scope.files if any(_file_matches(pattern, path) or ("/" not in pattern and path.rsplit("/", 1)[-1] == pattern) for pattern in patterns)]


@statement("forbidden_subfolders", help="forbidden subfolders do not exist", builtin=True)
def _forbidden_subfolders(param: Any, scope: HexScope) -> list[str]:
    return [f"{location + '/' if location else ''}{name}/ — forbidden folder" for location in scope.locations for name in _as_list(param) if scope.is_dir(f"{location + '/' if location else ''}{name}")]


@statement("must_not_exist", help="scoped locations do not exist", builtin=True)
def _must_not_exist(param: Any, scope: HexScope) -> list[str]:
    return [] if param is False else [f"{location} — must not exist" for location in scope.locations if location and scope.exists(location)]


@statement("max_lines", help="each file stays under a line budget", builtin=True)
def _max_lines(param: Any, file: str, scope: HexScope) -> Optional[str]:
    count, limit = scope.line_count(file), int(param)
    return f"split {file}: keep it under {limit} lines (now {count})" if count > limit else None


_SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("hardcoded credential", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
)


@statement("no_secrets", help="files contain no recognizable credentials", builtin=True)
def _no_secrets(param: Any, scope: HexScope) -> list[str]:
    if param is False:
        return []
    output: list[str] = []
    for path in scope.files:
        text = scope.read_text(path)
        if not text:
            continue
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                output.append(f"{path} — looks like a {label}; use the secret store")
                break
    return output


@statement("append_only", help="existing bytes remain an exact prefix", builtin=True)
def _append_only(param: Any, scope: HexScope, diff: Optional[CandidateDiff]) -> list[str]:
    if param is False or diff is None:
        return []
    return [f"{path} — append-only; add at the end" for path in scope.files if path in diff.modified and not (diff.new_text(path) or "").startswith(diff.old_text(path) or "")]


@statement("immutable", help="existing files cannot be modified", builtin=True)
def _immutable(param: Any, scope: HexScope, diff: Optional[CandidateDiff]) -> list[str]:
    if param is False or diff is None:
        return []
    return [f"{path} — immutable; add a new version" for path in scope.files if path in diff.modified]


@statement("changed_with", help="trigger files change with companion paths", builtin=True)
def _changed_with(param: Any, diff: Optional[CandidateDiff]) -> Optional[str]:
    if diff is None or not isinstance(param, dict):
        return None
    when = [str(item) for item in _as_list(param.get("when"))]
    expect = [str(item) for item in _as_list(param.get("expect"))]
    staged = diff.changed
    if not when or not expect or not any(glob_match(pattern, path) for path in staged for pattern in when):
        return None
    if any(glob_match(pattern, path) for path in staged for pattern in expect):
        return None
    return f"you changed {', '.join(when)} but none of {', '.join(expect)}"


def _frontmatter(text: Optional[str]) -> Mapping[str, Any]:
    if not text or not text.startswith("---"):
        return {}
    lines = text.splitlines()
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        value = yaml.safe_load("\n".join(lines[1:end])) or {}
    except (StopIteration, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


@statement("required_frontmatter", help="markdown frontmatter declares required keys", builtin=True)
def _required_frontmatter(param: Any, scope: HexScope) -> list[str]:
    keys = [str(item) for item in _as_list(param)]
    output: list[str] = []
    for path in scope.files:
        if not path.endswith(".md"):
            continue
        metadata = _frontmatter(scope.read_text(path))
        output.extend(f"{path} — add frontmatter key {key!r}" for key in keys if key not in metadata or metadata[key] in (None, "", [], {}))
    return output


@statement("no_frontmatter", help="markdown files carry no frontmatter", builtin=True)
def _no_frontmatter(scope: HexScope) -> list[str]:
    return [f"{path} — remove the frontmatter block" for path in scope.files if path.endswith(".md") and (scope.read_text(path) or "").startswith("---")]


_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


@statement("links_are_relative", help="internal Markdown links are relative", builtin=True)
def _links_relative(scope: HexScope) -> list[str]:
    output: list[str] = []
    for path in scope.files:
        if not path.endswith(".md"):
            continue
        for target in _MD_LINK.findall(scope.read_text(path) or ""):
            clean = target.strip().split()[0]
            if clean.startswith("/") and not clean.startswith("//"):
                output.append(f"{path} — make link relative: {clean}")
    return output


@statement("links_resolve", help="relative Markdown links resolve", builtin=True)
def _links_resolve(scope: HexScope) -> list[str]:
    output: list[str] = []
    for path in scope.files:
        if not path.endswith(".md"):
            continue
        parent = Path(path).parent
        for target in _MD_LINK.findall(scope.read_text(path) or ""):
            clean = target.strip().split()[0]
            if clean.startswith(("http://", "https://", "mailto:", "tel:", "//", "/", "#")):
                continue
            relative = (parent / clean.split("#", 1)[0]).as_posix()
            normalized = os.path.normpath(relative).replace("\\", "/")
            if not scope.exists(normalized):
                output.append(f"{path} — dead link {clean}")
    return output


__all__ = [
    "BLOCK",
    "CONTRACT_VERSION",
    "CandidateDiff",
    "ENGINE_VERSION",
    "HexContract",
    "HexContractError",
    "HexFinding",
    "apply_imports",
    "discover",
    "evaluate_contract",
    "explain_contract",
    "find_contract",
    "load_contract",
    "render_agent_digest",
    "replace_agent_digest",
    "run_contract",
    "stage_commands",
    "statement",
]
