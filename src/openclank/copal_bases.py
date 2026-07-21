"""Versioned, bounded Copal Bases parser and live document query engine."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import cmp_to_key
from pathlib import PurePosixPath
from typing import Any, Iterable

import yaml


MAX_DEFINITION_BYTES = 262_144
MAX_STRUCTURE_NODES = 10_000
MAX_EXPRESSION_NODES = 96
MAX_SOURCE_ROWS = 5_000
MAX_PAGE_SIZE = 500
INTERNAL_KINDS = {"asset", "planning", "calendar-projection", "treehouse-state"}
PROPERTY = re.compile(r"^(?:file\.)?[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
FAST_INTEGER = re.compile(r"^[+-]?(?:0|[1-9][0-9_]*|[1-9][0-9_]*[0-9])$")
FAST_FLOAT = re.compile(
    r"^[+-]?(?:(?:[0-9][0-9_]*)?\.[0-9_]+(?:[eE][+-]?[0-9_]+)?|[0-9][0-9_]*[eE][+-]?[0-9_]+)$"
)
ISO_DATE_LIKE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ][^\s]*)?$")


class BaseDefinitionError(ValueError):
    def __init__(self, message: str, *, path: str = "$", code: str = "invalid_definition"):
        super().__init__(message)
        self.diagnostics = [{"path": path, "code": code, "message": message}]


def _bounded_walk(value: Any) -> None:
    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        count += 1
        if count > MAX_STRUCTURE_NODES:
            raise BaseDefinitionError("Base definition is too structurally complex", code="definition_too_complex")
        if isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _repair_invalid_legacy_layout(content: str) -> str | None:
    """Repair the one shipped pre-v1 shape that is not valid YAML at all.

    Its ``filters`` list contains scalar expressions followed by mapping keys
    indented as though a scalar list item could own them.  Move that mapping to
    an explicit migration-only root key; the canonicalizer consumes it below.
    """
    lines = content.splitlines()
    nested = next((index for index, line in enumerate(lines) if re.match(r"^    (filters|order|sort):\s*$", line)), None)
    if nested is None or not any(line.strip() == "filters:" and not line.startswith(" ") for line in lines[:nested]):
        return None
    repaired = [*lines[:nested], "legacyNested:"]
    for line in lines[nested:]:
        if line.strip() and not line.startswith("    "):
            return None
        repaired.append(line[2:] if line.startswith("  ") else line)
    return "\n".join(repaired) + ("\n" if content.endswith("\n") else "")


def _property(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not PROPERTY.fullmatch(text):
        raise BaseDefinitionError(f"Invalid property name: {text!r}", path=path, code="invalid_property")
    return text


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or fallback


def _legacy_filter(value: str, path: str) -> dict[str, Any]:
    has_property = re.fullmatch(r'\s*file\.hasProperty\(["\']([^"\']+)["\']\)\s*', value)
    if has_property:
        return {"property": _property(has_property.group(1), path), "operator": "exists"}
    comparison = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*",
        value,
    )
    if not comparison:
        raise BaseDefinitionError(f"Unsupported legacy filter expression: {value!r}", path=path, code="unsupported_filter")
    raw = comparison.group(3)
    try:
        expected = yaml.safe_load(raw)
    except yaml.YAMLError:
        expected = raw.strip('"\'')
    op = {"==": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}[comparison.group(2)]
    return {"property": _property(comparison.group(1), path), "operator": op, "value": expected}


def _canonical_filter(value: Any, path: str = "$.views[0].filters") -> dict[str, Any] | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        return _legacy_filter(value, path)
    if isinstance(value, list):
        children = [_canonical_filter(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return {"and": [item for item in children if item]}
    if not isinstance(value, dict):
        raise BaseDefinitionError("Filter must be a string, object, or list", path=path)
    for compound in ("and", "or"):
        if compound in value:
            raw = value[compound]
            if not isinstance(raw, list) or not raw:
                raise BaseDefinitionError(f"{compound} filter must be a non-empty list", path=f"{path}.{compound}")
            return {compound: [_canonical_filter(item, f"{path}.{compound}[{index}]") for index, item in enumerate(raw)]}
    if "not" in value:
        return {"not": _canonical_filter(value["not"], f"{path}.not")}
    prop = _property(value.get("property"), f"{path}.property")
    op = str(value.get("operator") or "eq").lower()
    allowed = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "starts_with", "ends_with", "in", "exists", "missing"}
    if op not in allowed:
        raise BaseDefinitionError(f"Unsupported filter operator: {op}", path=f"{path}.operator", code="unsupported_operator")
    leaf = {"property": prop, "operator": op}
    if op not in {"exists", "missing"}:
        leaf["value"] = value.get("value")
    return leaf


def _canonical_column(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"property": _property(value, path), "label": value}
    if not isinstance(value, dict):
        raise BaseDefinitionError("Column must be a property string or object", path=path)
    prop = _property(value.get("property") or value.get("id"), f"{path}.property")
    column = {"property": prop, "label": str(value.get("label") or prop)[:128]}
    if value.get("formula") not in (None, ""):
        formula = str(value["formula"])
        compile_formula(formula)
        column["formula"] = formula
    if value.get("width") is not None:
        column["width"] = max(48, min(800, int(value["width"])))
    return column


def _canonical_sort(value: Any, path: str) -> dict[str, str]:
    if isinstance(value, str):
        return {"property": _property(value, path), "direction": "asc"}
    if not isinstance(value, dict):
        raise BaseDefinitionError("Sort must be a property string or object", path=path)
    direction = str(value.get("direction") or "asc").lower()
    if direction not in {"asc", "desc"}:
        raise BaseDefinitionError("Sort direction must be asc or desc", path=f"{path}.direction")
    return {"property": _property(value.get("property"), f"{path}.property"), "direction": direction}


def _canonical_view(raw: dict[str, Any], index: int, inherited: dict[str, Any]) -> dict[str, Any]:
    path = f"$.views[{index}]"
    if not isinstance(raw, dict):
        raise BaseDefinitionError("View must be an object", path=path)
    name = str(raw.get("name") or f"View {index + 1}")[:128]
    view_type = str(raw.get("type") or "table").lower()
    if view_type not in {"table", "card", "list"}:
        raise BaseDefinitionError("View type must be table, card, or list", path=f"{path}.type", code="unsupported_view")
    columns_raw = raw.get("columns") or inherited.get("columns") or ["file.name", "tags"]
    if not isinstance(columns_raw, list) or not columns_raw:
        raise BaseDefinitionError("View needs at least one column", path=f"{path}.columns")
    columns = [_canonical_column(item, f"{path}.columns[{i}]") for i, item in enumerate(columns_raw[:64])]
    sorts_raw = raw.get("sorts", raw.get("sort", inherited.get("sort", []))) or []
    if not isinstance(sorts_raw, list):
        sorts_raw = [sorts_raw]
    group_by = raw.get("groupBy", raw.get("group_by", inherited.get("groupBy")))
    summaries_raw = raw.get("summaries", inherited.get("summaries", {})) or {}
    if not isinstance(summaries_raw, dict):
        raise BaseDefinitionError("summaries must be an object", path=f"{path}.summaries")
    summaries: dict[str, str] = {}
    for key, operation in list(summaries_raw.items())[:32]:
        prop = _property(key, f"{path}.summaries")
        op = str(operation).lower()
        if op not in {"count", "sum", "avg", "min", "max", "distinct"}:
            raise BaseDefinitionError(f"Unsupported summary: {op}", path=f"{path}.summaries.{key}")
        summaries[prop] = op
    known = {"id", "name", "type", "columns", "filters", "filter", "sort", "sorts", "groupBy", "group_by", "summaries", "limit", "extensions"}
    extensions = dict(raw.get("extensions") or {}) if isinstance(raw.get("extensions"), dict) else {}
    extensions.update({key: value for key, value in raw.items() if key not in known})
    return {
        "id": _slug(str(raw.get("id") or name), f"view-{index + 1}"),
        "name": name,
        "type": view_type,
        "columns": columns,
        "filters": _canonical_filter(raw.get("filters", raw.get("filter", inherited.get("filters"))), f"{path}.filters"),
        "sorts": [_canonical_sort(item, f"{path}.sorts[{i}]") for i, item in enumerate(sorts_raw[:8])],
        "groupBy": _property(group_by, f"{path}.groupBy") if group_by else None,
        "summaries": summaries,
        "limit": max(1, min(MAX_SOURCE_ROWS, int(raw.get("limit", inherited.get("limit", 1000))))),
        "extensions": extensions,
    }


def parse_base_definition(content: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if len(content.encode()) > MAX_DEFINITION_BYTES:
        raise BaseDefinitionError("Base definition exceeds 256 KiB", code="definition_too_large")
    repaired_legacy = False
    try:
        raw = yaml.safe_load(content) if content.strip() else {}
    except yaml.YAMLError as exc:
        repaired = _repair_invalid_legacy_layout(content)
        if repaired is not None:
            try:
                raw = yaml.safe_load(repaired)
                repaired_legacy = True
            except yaml.YAMLError:
                raw = None
        else:
            raw = None
        if raw is None:
            mark = getattr(exc, "problem_mark", None)
            path = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "$"
            raise BaseDefinitionError(f"YAML/JSON parse error: {exc}", path=path, code="parse_error") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BaseDefinitionError("Base definition root must be an object")
    _bounded_walk(raw)
    version = raw.get("version", raw.get("schemaVersion", 0))
    if version not in {0, 1, "1"}:
        raise BaseDefinitionError(f"Unsupported Base version: {version}", path="$.version", code="unsupported_version")

    diagnostics: list[dict[str, str]] = []
    inherited = {
        "columns": raw.get("columns"),
        "filters": raw.get("filters", raw.get("filter")),
        "sort": raw.get("sort", raw.get("sorts", [])),
        "groupBy": raw.get("groupBy", raw.get("group_by")),
        "summaries": raw.get("summaries", {}),
        "limit": raw.get("limit", 1000),
    }
    legacy_nested = raw.get("legacyNested") if isinstance(raw.get("legacyNested"), dict) else {}
    if repaired_legacy:
        nested_filters = legacy_nested.get("filters", {}).get("and", []) if isinstance(legacy_nested.get("filters"), dict) else []
        current_filters = inherited["filters"] if isinstance(inherited["filters"], list) else []
        inherited["filters"] = [*current_filters, *nested_filters]
        inherited["columns"] = inherited["columns"] or legacy_nested.get("order")
        inherited["sort"] = inherited["sort"] or legacy_nested.get("sort", [])
        diagnostics.append({"path": "$.filters", "code": "legacy_nested_view_fields", "message": "Recovered invalid nested filters/order/sort from the shipped legacy fixture"})
    # The shipped Copal fixture accidentally nested order/sort under a filter
    # entry.  Recover those keys explicitly and report the migration instead
    # of silently pretending the malformed shape was canonical.
    if version == 0 and isinstance(inherited["filters"], list):
        filters = []
        for item in inherited["filters"]:
            if isinstance(item, dict) and any(key in item for key in ("filters", "order", "sort")):
                filters.extend(item.get("filters", {}).get("and", []) if isinstance(item.get("filters"), dict) else [])
                inherited["columns"] = inherited["columns"] or item.get("order")
                inherited["sort"] = inherited["sort"] or item.get("sort", [])
                diagnostics.append({"path": "$.filters", "code": "legacy_nested_view_fields", "message": "Recovered nested filters/order/sort from legacy fixture"})
            else:
                filters.append(item)
        inherited["filters"] = filters

    views_raw = raw.get("views") or [{"name": "Table", "type": "table"}]
    if not isinstance(views_raw, list) or not views_raw:
        raise BaseDefinitionError("views must be a non-empty list", path="$.views")
    views = [_canonical_view(view, index, inherited) for index, view in enumerate(views_raw[:32])]
    known = {"version", "schemaVersion", "views", "columns", "filters", "filter", "sort", "sorts", "groupBy", "group_by", "summaries", "limit", "extensions", "legacyNested"}
    extensions = dict(raw.get("extensions") or {}) if isinstance(raw.get("extensions"), dict) else {}
    extensions.update({key: value for key, value in raw.items() if key not in known})
    definition = {
        "version": 1,
        "views": views,
        "extensions": extensions,
    }
    if version == 0:
        diagnostics.append({"path": "$.version", "code": "legacy_version", "message": "Legacy Base normalized to version 1"})
    return definition, diagnostics


def dump_base_definition(definition: dict[str, Any]) -> str:
    """Canonical JSON is valid YAML and avoids lossy browser-side YAML emitters."""
    return json.dumps(definition, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def set_frontmatter_property(text: str, prop: str, value: Any) -> str:
    """Update one top-level property while preserving the document body."""
    prop = _property(prop, "$.property")
    if prop.startswith("file.") or "." in prop:
        raise BaseDefinitionError("Only top-level document properties are editable", path="$.property", code="read_only_property")
    rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    if len(lines) >= 2 and lines[0].strip() == "---":
        try:
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration as exc:
            raise BaseDefinitionError("Document has an unterminated frontmatter block", code="invalid_frontmatter") from exc
        replaced = False
        for index in range(1, end):
            if lines[index].split(":", 1)[0].strip() == prop:
                lines[index] = f"{prop}: {rendered}"
                replaced = True
                break
        if not replaced:
            lines.insert(end, f"{prop}: {rendered}")
    else:
        lines = ["---", f"{prop}: {rendered}", "---", *lines]
    result = "\n".join(lines)
    return result + ("\n" if trailing_newline else "")


def _coerce(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return ""
    lowered = stripped.casefold()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    if FAST_INTEGER.fullmatch(stripped):
        return int(stripped.replace("_", ""), 10)
    if FAST_FLOAT.fullmatch(stripped):
        return float(stripped.replace("_", ""))

    # Redb exposes frontmatter scalars as strings. Most values are ordinary
    # prose, and starting a complete YAML parser for every table cell dominated
    # large Base queries. Preserve YAML semantics for structured, quoted,
    # date/time, tagged, and uncommon numeric literals; return plain text
    # immediately.
    yaml_candidate = (
        stripped[0] in "[{\"'&*!|>"
        or ":" in stripped
        or bool(ISO_DATE_LIKE.fullmatch(stripped))
        or lowered.startswith(("0x", "0o", "0b", ".inf", "+.inf", "-.inf", ".nan"))
    )
    if not yaml_candidate:
        return value
    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return value
    return parsed if isinstance(parsed, (str, int, float, bool, list, dict, type(None), date, datetime)) else value


def document_values(doc: dict[str, Any]) -> dict[str, Any]:
    name = str(doc.get("name") or "")
    path = PurePosixPath(name)
    frontmatter = {str(key): _coerce(value) for key, value in (doc.get("frontmatter") or {}).items()}
    values = dict(frontmatter)
    values.update({
        "name": name,
        "kind": doc.get("kind"),
        "tags": doc.get("tags") or [],
        "links": doc.get("links") or [],
        "file.name": path.name,
        "file.path": name,
        "file.ext": path.suffix.lstrip("."),
        "file.kind": doc.get("kind"),
        "file.modified": doc.get("ts"),
        "file.tags": doc.get("tags") or [],
        "file.links": doc.get("links") or [],
    })
    return values


def get_property(values: dict[str, Any], prop: str) -> Any:
    if prop in values:
        return values[prop]
    if prop.startswith("properties."):
        return values.get(prop.removeprefix("properties."))
    return None


def _comparable(value: Any) -> tuple[int, Any]:
    if value is None:
        return (5, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, value if math.isfinite(value) else 0)
    if isinstance(value, (date, datetime)):
        return (2, value.isoformat())
    if isinstance(value, (list, tuple, set)):
        return (3, tuple(str(item).casefold() for item in value))
    return (4, str(value).casefold())


def _compare(actual: Any, expected: Any, op: str) -> bool:
    if op == "exists":
        return actual is not None
    if op == "missing":
        return actual is None
    if op in {"contains", "not_contains"}:
        if isinstance(actual, (list, tuple, set)):
            found = any(str(item).casefold() == str(expected).casefold() for item in actual)
        else:
            found = str(expected).casefold() in str(actual or "").casefold()
        return found if op == "contains" else not found
    if op == "starts_with":
        return str(actual or "").casefold().startswith(str(expected).casefold())
    if op == "ends_with":
        return str(actual or "").casefold().endswith(str(expected).casefold())
    if op == "in":
        return any(_compare(actual, item, "eq") for item in (expected if isinstance(expected, list) else [expected]))
    left, right = _comparable(actual), _comparable(_coerce(expected))
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if actual is None:
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def matches_filter(values: dict[str, Any], rule: dict[str, Any] | None) -> bool:
    if not rule:
        return True
    if "and" in rule:
        return all(matches_filter(values, child) for child in rule["and"])
    if "or" in rule:
        return any(matches_filter(values, child) for child in rule["or"])
    if "not" in rule:
        return not matches_filter(values, rule["not"])
    return _compare(get_property(values, rule["property"]), rule.get("value"), rule["operator"])


_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod}
_CMP_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le}


@dataclass(frozen=True)
class Formula:
    source: str
    tree: ast.Expression


def compile_formula(source: str) -> Formula:
    if len(source) > 512:
        raise BaseDefinitionError("Formula exceeds 512 characters", code="formula_too_large")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise BaseDefinitionError(f"Invalid formula: {exc.msg}", code="formula_syntax") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_EXPRESSION_NODES:
        raise BaseDefinitionError("Formula is too complex", code="formula_too_complex")
    allowed = (
        ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.BinOp, ast.UnaryOp,
        ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Load,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd, ast.Not,
        ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
    )
    if any(not isinstance(node, allowed) for node in nodes):
        bad = next(node for node in nodes if not isinstance(node, allowed))
        raise BaseDefinitionError(f"Formula operation is not allowed: {type(bad).__name__}", code="formula_unsafe")
    for node in nodes:
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in {"lower", "upper", "length", "coalesce", "round", "abs"}):
            raise BaseDefinitionError("Formula call is not allowed", code="formula_unsafe")
    return Formula(source=source, tree=tree)


def _attribute_name(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def evaluate_formula(formula: Formula, values: dict[str, Any]) -> Any:
    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else _attribute_name(node)
            return get_property(values, name or "")
        if isinstance(node, ast.BinOp):
            return _BIN_OPS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            if isinstance(node.op, ast.Not): return not value
            if isinstance(node.op, ast.USub): return -value
            return +value
        if isinstance(node, ast.BoolOp):
            items = [visit(value) for value in node.values]
            return all(items) if isinstance(node.op, ast.And) else any(items)
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                if not _CMP_OPS[type(op_node)](left, right): return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return visit(node.body) if visit(node.test) else visit(node.orelse)
        if isinstance(node, ast.Call):
            args = [visit(arg) for arg in node.args]
            functions = {
                "lower": lambda value: str(value or "").lower(),
                "upper": lambda value: str(value or "").upper(),
                "length": lambda value: len(value or []),
                "coalesce": lambda *items: next((item for item in items if item is not None), None),
                "round": round,
                "abs": abs,
            }
            return functions[node.func.id](*args)  # type: ignore[union-attr]
        raise ValueError(f"Unsupported formula node: {type(node).__name__}")

    return visit(formula.tree)


def _summary(rows: list[dict[str, Any]], prop: str, operation: str) -> Any:
    values = [row["values"].get(prop) for row in rows if row["values"].get(prop) is not None]
    if operation == "count": return len(values)
    if operation == "distinct": return len({json.dumps(value, sort_keys=True, default=str) for value in values})
    if not values: return None
    if operation in {"sum", "avg"}:
        numbers = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if not numbers: return None
        return sum(numbers) if operation == "sum" else sum(numbers) / len(numbers)
    return (min if operation == "min" else max)(values, key=_comparable)


def query_base(
    definition: dict[str, Any],
    documents: Iterable[dict[str, Any]],
    *,
    view_id: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    views = definition["views"]
    view = next((item for item in views if item["id"] == view_id), views[0]) if view_id else views[0]
    formulas = {
        column["property"]: compile_formula(column["formula"])
        for column in view["columns"] if column.get("formula")
    }
    rows = []
    source_count = 0
    truncated_source = False
    for doc in documents:
        if doc.get("kind") == "base" or doc.get("kind") in INTERNAL_KINDS:
            continue
        source_count += 1
        if source_count > MAX_SOURCE_ROWS:
            truncated_source = True
            break
        values = document_values(doc)
        errors = []
        for prop, formula in formulas.items():
            try:
                values[prop] = evaluate_formula(formula, values)
            except Exception as exc:
                values[prop] = None
                errors.append({"property": prop, "message": str(exc)[:200]})
        if matches_filter(values, view["filters"]):
            rows.append({
                "documentId": doc.get("id"),
                "head": doc.get("head"),
                "name": doc.get("name"),
                "kind": doc.get("kind"),
                "values": values,
                "errors": errors,
            })
        if len(rows) >= view["limit"]:
            break

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        for sort in view["sorts"]:
            a = _comparable(left["values"].get(sort["property"]))
            b = _comparable(right["values"].get(sort["property"]))
            if a != b:
                result = -1 if a < b else 1
                return result if sort["direction"] == "asc" else -result
        return -1 if str(left["documentId"]) < str(right["documentId"]) else int(str(left["documentId"]) > str(right["documentId"]))

    rows.sort(key=cmp_to_key(compare))
    total = len(rows)
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    page = max(1, page)
    start = (page - 1) * page_size
    visible = rows[start:start + page_size]
    group_by = view.get("groupBy")
    groups = []
    if group_by:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in visible:
            key_value = row["values"].get(group_by)
            key = "(missing)" if key_value is None else str(key_value)
            grouped.setdefault(key, []).append(row)
        groups = [{"key": key, "rows": items} for key, items in grouped.items()]
    return {
        "view": view,
        "rows": visible,
        "groups": groups,
        "summaries": {prop: _summary(rows, prop, operation) for prop, operation in view["summaries"].items()},
        "page": page,
        "pageSize": page_size,
        "total": total,
        "pages": max(1, math.ceil(total / page_size)),
        "sourceCount": min(source_count, MAX_SOURCE_ROWS),
        "sourceTruncated": truncated_source,
    }
