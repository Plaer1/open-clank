"""Versioned, bounded Copal Bases parser and live document query engine."""

from __future__ import annotations

import ast
import copy
import json
import math
import operator
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import cmp_to_key
from typing import Any, Iterable

import yaml


MAX_DEFINITION_BYTES = 262_144
MAX_STRUCTURE_NODES = 10_000
MAX_EXPRESSION_NODES = 96
# Legacy filter strings are parsed before they become typed predicates. Keep
# their lexical work bounded independently from the complete Base document
# limit so a single hostile scalar cannot consume the parser budget.
MAX_FILTER_SOURCE = 16_384
# A Base query must be complete for the audited 5,001+ source corpus. Keep a
# generous safety ceiling for pathological workspaces while allowing normal
# large vaults to be indexed in one authoritative query.
MAX_SOURCE_ROWS = 50_000
MAX_PAGE_SIZE = 500
MAX_TYPED_RELATIONS = 256
MAX_RELATION_TARGET_BYTES = 4_096
MAX_RELATION_ID_BYTES = 1_024
INTERNAL_KINDS = {"asset", "planning", "calendar-projection", "treehouse-state"}
PROPERTY = re.compile(r"^(?:file\.)?[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
FILE_FIELDS = {
    "file.name", "file.basename", "file.path", "file.folder", "file.ext", "file.size",
    "file.properties", "file.tags", "file.links", "file.ctime", "file.mtime", "file.kind",
    "file.modified", "file.link", "file.day",
}
FAST_INTEGER = re.compile(r"^[+-]?(?:0|[1-9][0-9_]*|[1-9][0-9_]*[0-9])$")
FAST_FLOAT = re.compile(
    r"^[+-]?(?:(?:[0-9][0-9_]*)?\.[0-9_]+(?:[eE][+-]?[0-9_]+)?|[0-9][0-9_]*[eE][+-]?[0-9_]+)$"
)
ISO_DATE_LIKE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ][^\s]*)?$")


class BaseDefinition(dict):
    """Canonical internal definition with an out-of-band source snapshot."""

    def __init__(self, *args: Any, source_text: str | None = None, source_node: Any = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._source_text = source_text
        self._source_node = source_node
        self._allow_structural_edits = False
        self._source_snapshot = json.loads(json.dumps(self, ensure_ascii=False, default=str))

    @property
    def source_text(self) -> str | None:
        return self._source_text


class BaseDefinitionError(ValueError):
    def __init__(self, message: str, *, path: str = "$", code: str = "invalid_definition", source: str | None = None):
        super().__init__(message)
        self.source = source
        diagnostic = {"path": path, "code": code, "message": message}
        if source is not None:
            diagnostic["source"] = source
        self.diagnostics = [diagnostic]


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




def _property(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not PROPERTY.fullmatch(text):
        raise BaseDefinitionError(f"Invalid property name: {text!r}", path=path, code="invalid_property")
    return text


def canonical_base_property(value: Any, path: str = "$.property") -> str:
    """Normalize source-property aliases without mapping derived fields."""
    text = _property(value, path)
    for prefix in ("note.", "properties."):
        if text.startswith(prefix):
            return _property(text[len(prefix):], path)
    return text


def _filter_property(value: Any, path: str) -> str:
    text = canonical_base_property(value, path)
    if text.startswith("this.") or text == "this":
        raise BaseDefinitionError("The this context is unavailable for server Base queries", path=path, code="unsupported_context")
    if text.startswith("file.") and text not in FILE_FIELDS:
        raise BaseDefinitionError(f"Unsupported file field: {text}", path=path, code="unsupported_property")
    return text


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or fallback


def _normalize_logical_path(value: Any, path: str, *, allow_empty: bool = False) -> str:
    """Normalize a vault/corpus-relative path without touching a host path."""
    if not isinstance(value, str):
        raise BaseDefinitionError("Folder must be a string", path=path, code="invalid_folder")
    text = value.strip()
    if not text and allow_empty:
        return ""
    if not text or text.startswith("/") or "\\" in text or ":" in text:
        raise BaseDefinitionError("Folder must be a relative slash-separated path", path=path, code="invalid_folder")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise BaseDefinitionError("Folder cannot contain traversal segments", path=path, code="invalid_folder")
    return "/".join(parts)


def _folder_rule(folder: Any, path: str) -> dict[str, Any]:
    return {"property": "file.path", "operator": "in_folder", "value": _normalize_logical_path(folder, path)}


def _collection_rule(function: str, value: Any, path: str) -> dict[str, Any]:
    property_name = "file.tags" if function == "tag" else "file.links"
    operator_name = "has_tag" if function == "tag" else "has_link"
    if not isinstance(value, str) or not value.strip():
        raise BaseDefinitionError(f"file.has{function.title()} requires a non-empty string", path=path, code="invalid_filter")
    return {"property": property_name, "operator": operator_name, "value": value.strip()}


class _FilterExpressionParser:
    """Small parser for the native Bases boolean/filter subset.

    It intentionally has no name lookup or evaluation step. The output is the
    same typed predicate tree used by YAML definitions, so unsupported syntax
    remains a source diagnostic rather than becoming executable code.
    """

    _OPERATORS = ("&&", "||", "==", "!=", ">=", "<=", ">", "<", "!", "(", ")", ",")

    def __init__(self, source: str, path: str):
        if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_FILTER_SOURCE:
            raise BaseDefinitionError(
                "Legacy filter expression exceeds 16 KiB",
                path=path,
                code="filter_too_large",
                source=source if isinstance(source, str) else None,
            )
        self.source = source
        self.path = path
        self.tokens: list[tuple[str, str, int]] = []
        self.index = 0
        self._tokenize()

    def _error(self, message: str) -> BaseDefinitionError:
        position = self.tokens[self.index][2] if self.index < len(self.tokens) else len(self.source)
        return BaseDefinitionError(f"{message} at character {position}", path=self.path, code="unsupported_filter", source=self.source)

    def _tokenize(self) -> None:
        source = self.source
        index = 0
        while index < len(source):
            if source[index].isspace():
                index += 1
                continue
            start = index
            if source[index] in {"'", '"'}:
                quote = source[index]
                index += 1
                chars: list[str] = []
                while index < len(source):
                    char = source[index]
                    if char == "\\":
                        if index + 1 >= len(source):
                            raise self._error("Unterminated quoted string")
                        escaped = source[index + 1]
                        if escaped in {"n", "r", "t"}:
                            chars.append({"n": "\n", "r": "\r", "t": "\t"}[escaped])
                        elif escaped in {quote, "\\"}:
                            chars.append(escaped)
                        elif escaped == "u":
                            # The compatibility grammar accepts the same
                            # bounded Unicode escape commonly emitted by
                            # JSON/JavaScript string serializers. Decode it
                            # without handing source text to an evaluator.
                            digits = source[index + 2:index + 6]
                            if len(digits) != 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                                raise self._error("Invalid Unicode escape in quoted string")
                            chars.append(chr(int(digits, 16)))
                            index += 4
                        elif escaped == "x":
                            digits = source[index + 2:index + 4]
                            if len(digits) != 2 or not re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
                                raise self._error("Invalid hexadecimal escape in quoted string")
                            chars.append(chr(int(digits, 16)))
                            index += 2
                        else:
                            # Keep unknown escapes visible so a host-style
                            # separator such as ``\\Templates`` cannot be
                            # normalized into an unrelated logical path.
                            chars.extend(["\\", escaped])
                        index += 2
                    elif char == quote:
                        index += 1
                        break
                    else:
                        chars.append(char)
                        index += 1
                else:
                    raise self._error("Unterminated quoted string")
                try:
                    # Pair escaped UTF-16 surrogates into the actual Unicode
                    # scalar while rejecting lone surrogates that cannot be
                    # a safe logical resource path.
                    literal = "".join(chars).encode("utf-16", "surrogatepass").decode("utf-16")
                except UnicodeError as exc:
                    raise self._error("Invalid Unicode surrogate in quoted string") from exc
                self.tokens.append(("literal", literal, start))
                continue
            matched = next((op for op in self._OPERATORS if source.startswith(op, index)), None)
            if matched:
                self.tokens.append(("operator", matched, start))
                index += len(matched)
                continue
            while index < len(source) and not source[index].isspace() and not any(source.startswith(op, index) for op in self._OPERATORS):
                index += 1
            self.tokens.append(("atom", source[start:index], start))
        self.tokens.append(("eof", "", len(source)))

    def _peek(self, value: str | None = None) -> tuple[str, str, int]:
        token = self.tokens[self.index]
        if value is not None and token[1] != value:
            raise self._error(f"Expected {value!r}")
        return token

    def _take(self, value: str | None = None) -> tuple[str, str, int]:
        token = self._peek(value)
        self.index += 1
        return token

    def parse(self) -> dict[str, Any]:
        result = self._parse_or()
        if self._peek()[0] != "eof":
            raise self._error(f"Unexpected token {self._peek()[1]!r}")
        node_count = self._count_nodes(result)
        if node_count > MAX_EXPRESSION_NODES:
            raise BaseDefinitionError(
                "Legacy filter expression is too complex",
                path=self.path,
                code="filter_too_complex",
                source=self.source,
            )
        return result

    def _count_nodes(self, value: Any) -> int:
        if isinstance(value, dict):
            children = []
            for key in ("and", "or"):
                if key in value and isinstance(value[key], list):
                    children.extend(value[key])
            if "not" in value:
                children.append(value["not"])
            return 1 + sum(self._count_nodes(item) for item in children)
        return 0

    def _parse_or(self) -> dict[str, Any]:
        items = [self._parse_and()]
        while self._peek()[1] == "||":
            self._take("||")
            items.append(self._parse_and())
        return items[0] if len(items) == 1 else {"or": items}

    def _parse_and(self) -> dict[str, Any]:
        items = [self._parse_unary()]
        while self._peek()[1] == "&&":
            self._take("&&")
            items.append(self._parse_unary())
        return items[0] if len(items) == 1 else {"and": items}

    def _parse_unary(self) -> dict[str, Any]:
        if self._peek()[1] == "!":
            self._take("!")
            return {"not": self._parse_unary()}
        if self._peek()[1] == "(":
            self._take("(")
            result = self._parse_or()
            self._take(")")
            return result
        return self._parse_condition()

    def _parse_condition(self) -> dict[str, Any]:
        kind, name, _ = self._take()
        if kind != "atom":
            raise self._error("Expected a field or function")
        if self._peek()[1] == "(":
            self._take("(")
            args: list[tuple[str, str, int]] = []
            if self._peek()[1] != ")":
                args.append(self._take())
                while self._peek()[1] == ",":
                    self._take(",")
                    args.append(self._take())
            self._take(")")
            lowered = name.casefold()
            if lowered == "file.infolder" and len(args) == 1 and args[0][0] == "literal":
                return _folder_rule(args[0][1], f"{self.path}.inFolder")
            if lowered == "file.hasproperty" and len(args) == 1 and args[0][0] == "literal":
                return {"property": _property(args[0][1], self.path), "operator": "exists"}
            if lowered == "file.hastag" and len(args) == 1 and args[0][0] == "literal":
                return _collection_rule("tag", args[0][1], f"{self.path}.hasTag")
            if lowered == "file.haslink" and len(args) == 1 and args[0][0] == "literal":
                return _collection_rule("link", args[0][1], f"{self.path}.hasLink")
            raise self._error(f"Unsupported filter function {name!r}")
        operator_token = self._take()
        if operator_token[1] not in {"==", "!=", ">", ">=", "<", "<="}:
            raise self._error(f"Expected a comparison after {name!r}")
        value_kind, value, _ = self._take()
        if value_kind not in {"atom", "literal"}:
            raise self._error("Expected a comparison value")
        try:
            expected = value if value_kind == "literal" else yaml.safe_load(value)
        except yaml.YAMLError:
            expected = value
        op = {"==": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}[operator_token[1]]
        return {"property": _filter_property(name, self.path), "operator": op, "value": expected}


def _legacy_filter(value: str, path: str) -> dict[str, Any]:
    try:
        return _FilterExpressionParser(value, path).parse()
    except BaseDefinitionError as exc:
        if exc.source is not None:
            raise
        raise BaseDefinitionError(str(exc), path=exc.diagnostics[0]["path"], code=exc.diagnostics[0]["code"], source=value) from exc
    except (IndexError, ValueError) as exc:
        raise BaseDefinitionError(f"Unsupported legacy filter expression: {value!r}", path=path, code="unsupported_filter") from exc


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
    # A few importers represent native functions as an object instead of the
    # string form. Keep this adapter deliberately narrow and typed.
    function_name = value.get("function") or value.get("func")
    if function_name is not None:
        if str(function_name).casefold() == "file.infolder":
            return _folder_rule(value.get("folder", value.get("value")), f"{path}.folder")
        if str(function_name).casefold() == "file.hasproperty":
            return {"property": _property(value.get("property", value.get("value")), f"{path}.property"), "operator": "exists"}
        if str(function_name).casefold() in {"file.hastag", "file.haslink"}:
            function = "tag" if str(function_name).casefold() == "file.hastag" else "link"
            return _collection_rule(function, value.get("value", value.get("tag", value.get("link"))), f"{path}.value")
        raise BaseDefinitionError(f"Unsupported filter function: {function_name}", path=f"{path}.function", code="unsupported_filter")
    for compound in ("and", "or"):
        if compound in value:
            raw = value[compound]
            if not isinstance(raw, list) or not raw:
                raise BaseDefinitionError(f"{compound} filter must be a non-empty list", path=f"{path}.{compound}")
            return {compound: [_canonical_filter(item, f"{path}.{compound}[{index}]") for index, item in enumerate(raw)]}
    if "not" in value:
        return {"not": _canonical_filter(value["not"], f"{path}.not")}
    prop = _filter_property(value.get("property"), f"{path}.property")
    op = str(value.get("operator") or "eq").lower()
    allowed = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "starts_with", "ends_with", "in", "exists", "missing", "in_folder", "has_tag", "has_link"}
    if op not in allowed:
        raise BaseDefinitionError(f"Unsupported filter operator: {op}", path=f"{path}.operator", code="unsupported_operator")
    if op == "in_folder" and prop != "file.path":
        raise BaseDefinitionError("in_folder applies only to file.path", path=f"{path}.property", code="invalid_folder")
    if op == "has_tag" and prop != "file.tags":
        raise BaseDefinitionError("has_tag applies only to file.tags", path=f"{path}.property", code="invalid_filter")
    if op == "has_link" and prop != "file.links":
        raise BaseDefinitionError("has_link applies only to file.links", path=f"{path}.property", code="invalid_filter")
    leaf = {"property": prop, "operator": op}
    if op not in {"exists", "missing"}:
        leaf["value"] = (
            _normalize_logical_path(value.get("value"), f"{path}.value")
            if op == "in_folder" else value.get("value")
        )
    return leaf


def _canonical_column(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, str):
        property_name = canonical_base_property(value, path)
        return {"property": property_name, "label": value}
    if not isinstance(value, dict):
        raise BaseDefinitionError("Column must be a property string or object", path=path)
    prop = canonical_base_property(value.get("property") or value.get("id"), f"{path}.property")
    column = {"property": prop, "label": str(value.get("label") or prop)[:128]}
    if value.get("formula") not in (None, ""):
        formula = str(value["formula"])
        compile_formula(formula)
        column["formula"] = formula
    if value.get("width") is not None:
        column["width"] = max(48, min(800, int(value["width"])))
    if "visible" in value:
        if not isinstance(value["visible"], bool):
            raise BaseDefinitionError("Column visibility must be boolean", path=f"{path}.visible", code="invalid_column_visibility")
        column["visible"] = value["visible"]
    return column


def _canonical_sort(value: Any, path: str) -> dict[str, str]:
    if isinstance(value, str):
        return {"property": _property(value, path), "direction": "asc"}
    if not isinstance(value, dict):
        raise BaseDefinitionError("Sort must be a property string or object", path=path)
    direction = str(value.get("direction") or "asc").lower()
    if direction not in {"asc", "desc"}:
        raise BaseDefinitionError("Sort direction must be asc or desc", path=f"{path}.direction")
    return {"property": canonical_base_property(value.get("property"), f"{path}.property"), "direction": direction}


def _canonical_view(raw: dict[str, Any], index: int, inherited: dict[str, Any]) -> dict[str, Any]:
    path = f"$.views[{index}]"
    if not isinstance(raw, dict):
        raise BaseDefinitionError("View must be an object", path=path)
    name = str(raw.get("name") or f"View {index + 1}")[:128]
    view_type = str(raw.get("type") or "table").lower()
    if view_type not in {"table", "card", "list"}:
        raise BaseDefinitionError("View type must be table, card, or list", path=f"{path}.type", code="unsupported_view")
    columns_raw = raw.get("columns") or raw.get("order") or inherited.get("columns") or ["file.name", "tags"]
    if not isinstance(columns_raw, list) or not columns_raw:
        raise BaseDefinitionError("View needs at least one column", path=f"{path}.columns")
    column_sizes = raw.get("columnSize", raw.get("columnSizes", inherited.get("columnSize", {})))
    if not isinstance(column_sizes, dict):
        column_sizes = {}
    column_sizes = {
        canonical_base_property(key, f"{path}.columnSize"): value
        for key, value in column_sizes.items()
    }
    columns = []
    for i, item in enumerate(columns_raw[:64]):
        column = _canonical_column(item, f"{path}.columns[{i}]")
        if column.get("property") in column_sizes and column.get("width") is None:
            try:
                column["width"] = max(48, min(800, int(column_sizes[column["property"]])))
            except (TypeError, ValueError) as exc:
                raise BaseDefinitionError("Column size must be an integer", path=f"{path}.columnSize.{column['property']}", code="invalid_column_size") from exc
        columns.append(column)
    sorts_raw = raw.get("sorts", raw.get("sort", inherited.get("sort", []))) or []
    if not isinstance(sorts_raw, list):
        sorts_raw = [sorts_raw]
    group_by = raw.get("groupBy", raw.get("group_by", inherited.get("groupBy")))
    summaries_raw = raw.get("summaries", inherited.get("summaries", {})) or {}
    if not isinstance(summaries_raw, dict):
        raise BaseDefinitionError("summaries must be an object", path=f"{path}.summaries")
    summaries: dict[str, str] = {}
    for key, operation in list(summaries_raw.items())[:32]:
        prop = canonical_base_property(key, f"{path}.summaries")
        op = str(operation).lower()
        if op not in {"count", "sum", "avg", "min", "max", "distinct"}:
            raise BaseDefinitionError(f"Unsupported summary: {op}", path=f"{path}.summaries.{key}")
        summaries[prop] = op
    known = {"id", "name", "type", "columns", "order", "columnSize", "columnSizes", "filters", "filter", "localFilters", "sort", "sorts", "groupBy", "group_by", "summaries", "limit", "extensions"}
    extensions = dict(raw.get("extensions") or {}) if isinstance(raw.get("extensions"), dict) else {}
    extensions.update({key: value for key, value in raw.items() if key not in known})
    local_filters_raw = raw.get("localFilters", raw.get("filters", raw.get("filter", inherited.get("filters"))))
    local_filters = _canonical_filter(local_filters_raw, f"{path}.localFilters")
    return {
        "id": _slug(str(raw.get("id") or name), f"view-{index + 1}"),
        "name": name,
        "type": view_type,
        "columns": columns,
        "columnSize": {column["property"]: column["width"] for column in columns if column.get("width") is not None},
        # localFilters is the normalized name. ``filters`` remains a read-only
        # compatibility alias for existing Copal view consumers.
        "localFilters": local_filters,
        "filters": local_filters,
        "sorts": [_canonical_sort(item, f"{path}.sorts[{i}]") for i, item in enumerate(sorts_raw[:8])],
        "groupBy": canonical_base_property(group_by, f"{path}.groupBy") if group_by else None,
        "summaries": summaries,
        "limit": max(1, min(MAX_SOURCE_ROWS, int(raw.get("limit", inherited.get("limit", 1000))))),
        "extensions": extensions,
    }


def parse_base_definition(content: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if len(content.encode()) > MAX_DEFINITION_BYTES:
        raise BaseDefinitionError("Base definition exceeds 256 KiB", code="definition_too_large")
    try:
        raw = yaml.safe_load(content) if content.strip() else {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        path = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "$"
        raise BaseDefinitionError("Base source is malformed; preserve the original and repair or convert it offline before reimporting.", path=path, code="parse_error") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BaseDefinitionError("Base definition root must be an object")
    _bounded_walk(raw)
    version = raw.get("version", raw.get("schemaVersion", 1))
    if str(version) == "0" or "legacyNested" in raw:
        raise BaseDefinitionError("This Base uses an unsupported source format. Preserve it and convert it offline to the current Base definition before reimporting.", path="$.version", code="manual_migration_required")
    if version not in {1, "1"}:
        raise BaseDefinitionError(f"Unsupported Base version: {version}", path="$.version", code="unsupported_version")

    diagnostics: list[dict[str, str]] = []
    source_version = 1
    root_filter = raw.get("globalFilters", raw.get("filters", raw.get("filter")))
    inherited = {
        "columns": raw.get("columns"),
        "columnSize": raw.get("columnSize", raw.get("columnSizes", {})),
        "filters": None,
        "sort": raw.get("sort", raw.get("sorts", [])),
        "groupBy": raw.get("groupBy", raw.get("group_by")),
        "summaries": raw.get("summaries", {}),
        "limit": raw.get("limit", 1000),
    }
    views_raw = raw.get("views") or [{"name": "Table", "type": "table"}]
    if not isinstance(views_raw, list) or not views_raw:
        raise BaseDefinitionError("views must be a non-empty list", path="$.views")
    views = [_canonical_view(view, index, inherited) for index, view in enumerate(views_raw[:32])]
    known = {"version", "schemaVersion", "sourceVersion", "globalFilters", "views", "columns", "filters", "filter", "sort", "sorts", "groupBy", "group_by", "summaries", "limit", "extensions", "legacyNested"}
    extensions = dict(raw.get("extensions") or {}) if isinstance(raw.get("extensions"), dict) else {}
    extensions.update({key: value for key, value in raw.items() if key not in known})
    definition = {
        "version": 1,
        "sourceVersion": source_version,
        "globalFilters": _canonical_filter(root_filter, "$.filters"),
        "views": views,
        "extensions": extensions,
    }
    try:
        source_node = yaml.compose(content)
    except yaml.YAMLError:
        source_node = None
    return BaseDefinition(definition, source_text=content, source_node=source_node), diagnostics


def _edit_projection(value: Any, *, scope: str | None = "root") -> Any:
    """Remove only normalized root/view compatibility aliases.

    Extension payloads are opaque and may legitimately contain a ``filters``
    key. Recursing with a key-name rule would hide edits in those payloads and
    incorrectly return the original source.
    """
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if scope in {"root", "view"} and key == "filters":
                continue
            if scope == "root" and key == "views" and isinstance(item, list):
                projected[key] = [_edit_projection(view, scope="view") for view in item]
            else:
                projected[key] = _edit_projection(item, scope=None)
        return projected
    if isinstance(value, list):
        return [_edit_projection(item, scope=None) for item in value]
    return value


def _source_mapping_value(node: Any, key: str) -> Any:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == key:
            return value_node
    return None


def _source_view_node(root: Any, index: int) -> Any:
    views = _source_mapping_value(root, "views")
    if not isinstance(views, yaml.SequenceNode) or index >= len(views.value):
        return None
    return views.value[index]


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        # JSON string quoting is valid YAML and keeps expressions containing
        # comments, colons, or leading YAML punctuation unambiguous.
        return json.dumps(value, ensure_ascii=False)
    rendered = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    return rendered.removesuffix("...").rstrip()


def _source_column_value(column: Any, source_node: Any = None) -> Any:
    if isinstance(column, str):
        return column
    if not isinstance(column, dict):
        return column
    source_property = None
    source_has_width = True
    if isinstance(source_node, yaml.ScalarNode):
        source_property = str(source_node.value)
        source_has_width = False
    elif isinstance(source_node, yaml.MappingNode):
        source_property_node = _source_mapping_value(source_node, "property") or _source_mapping_value(source_node, "id")
        if source_property_node is not None:
            source_property = str(source_property_node.value)
        source_has_width = _source_mapping_value(source_node, "width") is not None
    property_value = source_property if source_property and canonical_base_property(source_property) == column.get("property") else column.get("property")
    if source_property and column.get("label") == source_property and set(column) <= {"property", "label", "width"} and not source_has_width:
        return source_property
    result = {"property": property_value}
    if column.get("label") != property_value:
        result["label"] = column.get("label")
    for key in ("formula", "visible"):
        if key in column:
            result[key] = column[key]
    # A width inherited from a separate columnSize map belongs there; copying
    # it into order/columns creates two contradictory source declarations.
    if source_has_width and "width" in column:
        result["width"] = column["width"]
    return result


def _source_view_field(field: str, value: Any) -> Any:
    if field == "columns":
        return [_source_column_value(column) for column in (value or [])]
    if field == "extensions":
        return dict(value or {})
    return value


def _filter_source(rule: Any) -> str:
    if not rule:
        raise BaseDefinitionError("Removing an imported filter requires explicit conversion", code="unsupported_edit")
    if "and" in rule:
        return " && ".join(f"({_filter_source(item)})" for item in rule["and"])
    if "or" in rule:
        return " || ".join(f"({_filter_source(item)})" for item in rule["or"])
    if "not" in rule:
        return f"!({_filter_source(rule['not'])})"
    prop = str(rule.get("property", ""))
    op = rule.get("operator")
    value = rule.get("value")
    if op == "in_folder":
        return f"file.inFolder({_yaml_scalar(value)})"
    if op == "has_tag":
        return f"file.hasTag({_yaml_scalar(value)})"
    if op == "has_link":
        return f"file.hasLink({_yaml_scalar(value)})"
    if op == "exists":
        return f"file.hasProperty({_yaml_scalar(prop)})"
    if op == "missing":
        return f"!file.hasProperty({_yaml_scalar(prop)})"
    operator_name = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(op)
    if operator_name is None:
        raise BaseDefinitionError(f"Unsupported filter edit operator: {op}", code="unsupported_edit")
    return f"{prop} {operator_name} {_yaml_scalar(value)}"


def _source_preserving_dump(definition: BaseDefinition) -> str:
    source = definition.source_text
    root = definition._source_node
    if not source or root is None:
        raise BaseDefinitionError("Edited Base has no safe source map", code="unsupported_edit")
    before = _edit_projection(definition._source_snapshot)
    after = _edit_projection(dict(definition))
    if before == after:
        return source
    if not isinstance(root, yaml.MappingNode):
        raise BaseDefinitionError("Edited Base source root cannot be mapped safely", code="unsupported_edit")
    old = definition._source_snapshot
    patches: list[tuple[int, int, str]] = []
    for field in ("version", "sourceVersion"):
        if old.get(field) != definition.get(field):
            raise BaseDefinitionError(f"Changing normalized {field} requires explicit conversion", path=f"$.{field}", code="unsupported_edit")

    def add_node(node: Any, value: Any, path: str) -> None:
        if node is None or not isinstance(node, (yaml.ScalarNode, yaml.SequenceNode, yaml.MappingNode)):
            raise BaseDefinitionError(f"Cannot safely map edit at {path}", path=path, code="unsupported_edit")
        replacement = _yaml_scalar(value)
        # PyYAML's block collection end mark stops before the following key's
        # indentation/newline.  Keep that structural separator when a
        # sequence/map is rewritten into flow syntax.
        if isinstance(node, (yaml.SequenceNode, yaml.MappingNode)) and source[node.end_mark.index:node.end_mark.index + 1] not in {"", "\n", "\r"}:
            last_newline = source.rfind("\n", node.start_mark.index, node.end_mark.index)
            trailing_indent = source[last_newline + 1:node.end_mark.index] if last_newline >= 0 else ""
            replacement += "\n" + (trailing_indent if trailing_indent.strip() == "" else "")
        patches.append((node.start_mark.index, node.end_mark.index, replacement))

    def source_key(node: Any, keys: tuple[str, ...], path: str) -> Any:
        for key in keys:
            candidate = _source_mapping_value(node, key)
            if candidate is not None:
                return candidate
        raise BaseDefinitionError(f"Cannot locate source field for edit at {path}", path=path, code="source_conflict")

    def source_column_nodes(view_node: Any) -> dict[str, Any]:
        try:
            columns_node = source_key(view_node, ("columns", "order"), "$.columns")
        except BaseDefinitionError:
            return {}
        if not isinstance(columns_node, yaml.SequenceNode):
            return {}
        result: dict[str, Any] = {}
        for item in columns_node.value:
            if isinstance(item, yaml.ScalarNode):
                property_name = str(item.value)
            elif isinstance(item, yaml.MappingNode):
                property_node = _source_mapping_value(item, "property") or _source_mapping_value(item, "id")
                property_name = str(property_node.value) if property_node is not None else ""
            else:
                property_name = ""
            if property_name:
                try:
                    result[canonical_base_property(property_name)] = item
                except BaseDefinitionError:
                    continue
        return result

    def source_columns_value(view_node: Any, columns: Any) -> Any:
        nodes = source_column_nodes(view_node)
        return [_source_column_value(column, nodes.get(column.get("property"))) for column in (columns or [])]

    def source_column_has_explicit_width(view_node: Any, columns: Any) -> bool:
        nodes = source_column_nodes(view_node)
        return any(
            isinstance(nodes.get(column.get("property")), yaml.MappingNode)
            and _source_mapping_value(nodes[column.get("property")], "width") is not None
            for column in (columns or [])
        )

    def patch_column_size(node: Any, old_sizes: Any, new_sizes: Any, path: str) -> None:
        """Patch a single existing size scalar, retaining aliases and style."""
        if isinstance(node, yaml.MappingNode) and isinstance(old_sizes, dict) and isinstance(new_sizes, dict):
            changed = [key for key in set(old_sizes) | set(new_sizes) if old_sizes.get(key) != new_sizes.get(key)]
            if len(changed) == 1 and changed[0] in new_sizes:
                target = changed[0]
                for key_node, value_node in node.value:
                    raw_key = str(getattr(key_node, "value", ""))
                    try:
                        canonical_key = canonical_base_property(raw_key)
                    except BaseDefinitionError:
                        canonical_key = raw_key
                    if canonical_key == target:
                        add_node(value_node, new_sizes[target], f"{path}.{raw_key}")
                        return
        add_node(node, new_sizes, path)

    def patch_filter_node(node: Any, old_rule: Any, new_rule: Any, path: str) -> None:
        """Patch changed scalar leaves in a YAML filter tree in place."""
        if isinstance(node, yaml.ScalarNode):
            add_node(node, _filter_source(new_rule), path)
            return
        if isinstance(node, yaml.SequenceNode) and isinstance(old_rule, list) and isinstance(new_rule, list):
            if len(old_rule) != len(new_rule) or len(node.value) != len(old_rule):
                raise BaseDefinitionError(f"Filter structure edit at {path} is unsupported", path=path, code="unsupported_edit")
            for item_index, (old_item, new_item) in enumerate(zip(old_rule, new_rule)):
                if old_item != new_item:
                    patch_filter_node(node.value[item_index], old_item, new_item, f"{path}[{item_index}]")
            return
        if isinstance(old_rule, dict) and isinstance(new_rule, dict) and set(old_rule) == set(new_rule):
            if isinstance(node, yaml.MappingNode):
                for key, old_value in old_rule.items():
                    new_value = new_rule[key]
                    child = _source_mapping_value(node, key)
                    if child is None:
                        raise BaseDefinitionError(f"Cannot locate filter field for edit at {path}.{key}", path=f"{path}.{key}", code="source_conflict")
                    if old_value != new_value:
                        if isinstance(old_value, dict) or isinstance(old_value, list):
                            patch_filter_node(child, old_value, new_value, f"{path}.{key}")
                        else:
                            add_node(child, new_value, f"{path}.{key}")
                return
        if isinstance(old_rule, dict) and "not" in old_rule and isinstance(new_rule, dict) and set(new_rule) == {"not"}:
            if isinstance(node, yaml.MappingNode):
                child = _source_mapping_value(node, "not")
                if child is not None and old_rule["not"] != new_rule["not"]:
                    patch_filter_node(child, old_rule["not"], new_rule["not"], f"{path}.not")
                    return
        raise BaseDefinitionError(f"Filter structure edit at {path} cannot preserve source safely", path=path, code="unsupported_edit")

    def patch_filter(source_node: Any, old_rule: Any, new_rule: Any, path: str) -> None:
        if isinstance(source_node, yaml.ScalarNode):
            add_node(source_node, _filter_source(new_rule) if new_rule else None, path)
        else:
            patch_filter_node(source_node, old_rule, new_rule, path)

    if old.get("globalFilters") != definition.get("globalFilters"):
        node = source_key(root, ("globalFilters", "filters", "filter"), "$.globalFilters")
        patch_filter(node, old.get("globalFilters"), definition.get("globalFilters"), "$.globalFilters")

    old_views = old.get("views", [])
    new_views = definition.get("views", [])
    if not isinstance(old_views, list) or not isinstance(new_views, list) or len(old_views) != len(new_views):
        if not definition._allow_structural_edits:
            raise BaseDefinitionError("Adding, removing, or reordering views requires an explicit command", path="$.views", code="unsupported_edit")
        # View lifecycle commands have an explicit conversion boundary. The
        # canonical renderer retains unknown extension keys and values while
        # changing only the requested view collection.
        return dump_base_definition(dict(definition))
    if [item.get("id") for item in old_views] != [item.get("id") for item in new_views]:
        if not definition._allow_structural_edits:
            raise BaseDefinitionError("Reordering views requires an explicit command", path="$.views", code="unsupported_edit")
        return dump_base_definition(dict(definition))
    for index, (old_view, new_view) in enumerate(zip(old_views, new_views)):
        path = f"$.views[{index}]"
        view_node = _source_view_node(root, index)
        if view_node is None:
            raise BaseDefinitionError(f"Cannot locate source view for edit at {path}", path=path, code="source_conflict")
        for field in ("id", "name", "type"):
            if old_view.get(field) != new_view.get(field):
                add_node(source_key(view_node, (field,), f"{path}.{field}"), new_view.get(field), f"{path}.{field}")
        if old_view.get("localFilters") != new_view.get("localFilters"):
            node = source_key(view_node, ("localFilters", "filters", "filter"), f"{path}.localFilters")
            patch_filter(node, old_view.get("localFilters"), new_view.get("localFilters"), f"{path}.localFilters")
        source_columns = source_column_nodes(view_node)
        columns_changed = old_view.get("columns") != new_view.get("columns")
        # Widths inherited from columnSize are represented on normalized
        # columns for consumers, but are not part of source order/columns.
        # Ignore that derived difference so resize patches only columnSize.
        if columns_changed and not source_column_has_explicit_width(view_node, old_view.get("columns")):
            columns_changed = [
                {key: value for key, value in item.items() if key != "width"}
                for item in old_view.get("columns", [])
            ] != [
                {key: value for key, value in item.items() if key != "width"}
                for item in new_view.get("columns", [])
            ]
        for field in ("columns", "columnSize", "sorts", "groupBy", "summaries", "limit", "extensions"):
            field_changed = columns_changed if field == "columns" else old_view.get(field) != new_view.get(field)
            if field_changed:
                if not definition._allow_structural_edits:
                    raise BaseDefinitionError(f"Edit at {path}.{field} cannot preserve the imported layout safely", path=f"{path}.{field}", code="unsupported_edit")
                aliases = {
                    "columns": ("columns", "order"), "columnSize": ("columnSize", "columnSizes"),
                    "sorts": ("sorts", "sort"), "groupBy": ("groupBy", "group_by"),
                    "summaries": ("summaries",), "limit": ("limit",), "extensions": (),
                }
                if field == "extensions":
                    raise BaseDefinitionError(f"Edit at {path}.{field} cannot preserve the imported layout safely", path=f"{path}.{field}", code="unsupported_edit")
                try:
                    node = source_key(view_node, aliases[field], f"{path}.{field}")
                except BaseDefinitionError:
                    if definition._allow_structural_edits:
                        return dump_base_definition(dict(definition))
                    raise
                if field == "columns":
                    add_node(node, source_columns_value(view_node, new_view.get(field)), f"{path}.{field}")
                elif field == "columnSize":
                    patch_column_size(node, old_view.get(field), new_view.get(field), f"{path}.{field}")
                else:
                    add_node(node, _source_view_field(field, new_view.get(field)), f"{path}.{field}")
    if old.get("extensions") != definition.get("extensions"):
        raise BaseDefinitionError("Root extension edits require explicit conversion", path="$.extensions", code="unsupported_edit")
    for start, end, replacement in sorted(patches, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def dump_base_definition(definition: dict[str, Any]) -> str:
    """Return original source when unchanged, otherwise a native YAML shape.

    The runtime keeps a typed DTO (including compatibility aliases) separate
    from persisted source. This avoids writing ``globalFilters`` plus duplicate
    ``filters`` keys back into a user's imported Base.
    """
    if isinstance(definition, BaseDefinition) and definition.source_text is not None:
        current = json.loads(json.dumps(dict(definition), ensure_ascii=False, default=str))
        if current == definition._source_snapshot:
            return definition.source_text
        return _source_preserving_dump(definition)

    def source_column(column: dict[str, Any]) -> Any:
        if isinstance(column, str):
            return column
        if set(column) <= {"property", "label"} and column.get("label") == column.get("property"):
            return column["property"]
        result = dict(column)
        if result.get("label") == result.get("property"):
            result.pop("label", None)
        return result

    root: dict[str, Any] = {"version": 1}
    if definition.get("globalFilters") is not None:
        root["filters"] = definition["globalFilters"]
    views = []
    for view in definition.get("views", []):
        item: dict[str, Any] = {
            "id": view.get("id"), "name": view.get("name"), "type": view.get("type", "table"),
            "columns": [source_column(column) for column in view.get("columns", [])],
        }
        if view.get("columnSize"):
            item["columnSize"] = dict(view["columnSize"])
        if view.get("localFilters") is not None:
            item["filters"] = view["localFilters"]
        if view.get("sorts"):
            item["sorts"] = view["sorts"]
        if view.get("groupBy"):
            item["groupBy"] = view["groupBy"]
        if view.get("summaries"):
            item["summaries"] = view["summaries"]
        if view.get("limit", 1000) != 1000:
            item["limit"] = view["limit"]
        item.update(view.get("extensions") or {})
        for key, value in view.items():
            if key not in {"id", "name", "type", "columns", "columnSize", "localFilters", "filters", "sorts", "groupBy", "summaries", "limit", "extensions"}:
                item[key] = value
        views.append(item)
    root["views"] = views
    root.update(definition.get("extensions") or {})
    for key, value in definition.items():
        if key not in {"version", "sourceVersion", "globalFilters", "views", "extensions"}:
            root[key] = value
    rendered = json.dumps(root, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    return rendered


def set_frontmatter_property(text: str, prop: str, value: Any, *, remove: bool = False) -> str:
    """Update one top-level property while preserving body and source style.

    ``remove`` implements an explicit Clear gesture.  A regular ``None``
    value remains a real YAML null and is therefore distinct from clearing a
    property.
    """
    prop = _property(prop, "$.property")
    if prop.startswith("file.") or "." in prop:
        raise BaseDefinitionError("Only top-level document properties are editable", path="$.property", code="read_only_property")
    rendered = json.dumps(value, ensure_ascii=False)
    lines = text.splitlines(keepends=True)

    def line_body(line: str) -> str:
        return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith(("\n", "\r")) else line

    def line_ending(line: str) -> str:
        body = line_body(line)
        return line[len(body):]

    def comment_start(body: str) -> int | None:
        quote: str | None = None
        escaped = False
        for index, char in enumerate(body):
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if quote and char == quote:
                quote = None
            elif not quote and char in {"'", '"'}:
                quote = char
            elif not quote and char == "#" and (index == 0 or body[index - 1].isspace()):
                return index
        return None

    if lines and line_body(lines[0]).strip() == "---":
        try:
            end = next(index for index in range(1, len(lines)) if line_body(lines[index]).strip() == "---")
        except StopIteration as exc:
            raise BaseDefinitionError("Document has an unterminated frontmatter block", code="invalid_frontmatter") from exc
        for index in range(1, end):
            body = line_body(lines[index])
            if ":" not in body or body.split(":", 1)[0].strip() != prop:
                continue
            if remove:
                del lines[index]
            else:
                colon = body.index(":")
                comment = comment_start(body)
                if comment is not None:
                    suffix_start = comment
                    while suffix_start > colon + 1 and body[suffix_start - 1].isspace():
                        suffix_start -= 1
                    suffix = body[suffix_start:]
                else:
                    suffix = ""
                lines[index] = f"{body[:colon + 1]} {rendered}{suffix}{line_ending(lines[index])}"
            return "".join(lines)
        if remove:
            return text
        lines.insert(end, f"{prop}: {rendered}{line_ending(lines[end - 1]) if end > 0 else '\n'}")
        return "".join(lines)
    if remove:
        return text
    separator = "\r\n" if "\r\n" in text else "\n"
    return f"---{separator}{prop}: {rendered}{separator}---{separator}" + text


_BASE_COMMANDS = {
    "set-sort", "cycle-sort", "reorder-sort", "resize-column", "reorder-column",
    "set-column-visibility", "label-column", "set-filter", "set-grouping",
    "set-summary", "set-formula", "set-limit", "add-view", "rename-view",
    "duplicate-view", "reorder-view", "remove-view",
}


def _command_view(definition: BaseDefinition, view_id: Any) -> dict[str, Any]:
    requested = str(view_id or "").strip()
    if requested and len(requested) > 64:
        raise BaseDefinitionError("View id is too long", path="$.command.view_id", code="invalid_view_id")
    view = next((item for item in definition["views"] if item["id"] == requested), None) if requested else definition["views"][0]
    if view is None:
        raise BaseDefinitionError(f"Unknown view: {requested}", path="$.command.view_id", code="unknown_view")
    return view


def _command_index(value: Any, size: int, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= size:
        raise BaseDefinitionError("Command index is out of bounds", path=path, code="invalid_command_index")
    return value


def _command_property(value: Any, path: str = "$.command.property") -> str:
    return canonical_base_property(value, path)


def _view_id(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text):
        raise BaseDefinitionError("View id is invalid", path=path, code="invalid_view_id")
    return text


def _unique_view_id(definition: BaseDefinition, requested: Any, fallback: str) -> str:
    base = _view_id(requested, "$.command.view_id") if requested else _slug(fallback, "view")
    used = {str(view["id"]) for view in definition["views"]}
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base[:max(1, 64 - len(str(suffix)) - 1)]}-{suffix}"
        suffix += 1
    return candidate


def _apply_base_command(definition: BaseDefinition, command: dict[str, Any]) -> None:
    action = str(command.get("action") or command.get("type") or "").strip().lower().replace("_", "-")
    if action not in _BASE_COMMANDS:
        raise BaseDefinitionError("Unsupported Base command", path="$.command.action", code="unsupported_command")
    view = _command_view(definition, command.get("view_id"))
    if action in {"set-sort", "cycle-sort"}:
        prop = _command_property(command.get("property"))
        existing_index = next((index for index, item in enumerate(view["sorts"]) if item["property"] == prop), None)
        existing = view["sorts"][existing_index] if existing_index is not None else None
        direction = str(command.get("direction") or "").strip().lower()
        if action == "cycle-sort":
            direction = {None: "asc", "asc": "desc", "desc": "none"}.get(existing["direction"] if existing else None, "asc")
        if direction not in {"asc", "desc", "none"}:
            raise BaseDefinitionError("Sort direction is invalid", path="$.command.direction", code="invalid_sort_direction")
        if command.get("replace") is True:
            view["sorts"] = []
        elif existing is not None:
            if direction == "none":
                view["sorts"].pop(existing_index)
                return
            # Updating an existing priority changes only its direction.  The
            # source order is semantic: removing/re-appending would silently
            # demote a primary sort behind every other sort.
            existing["direction"] = direction
            return
        if direction != "none":
            view["sorts"].append({"property": prop, "direction": direction})
        return
    if action == "reorder-sort":
        sorts = view["sorts"]
        source = _command_index(command.get("from_index"), len(sorts), "$.command.from_index")
        target = _command_index(command.get("to_index"), len(sorts), "$.command.to_index")
        item = sorts.pop(source)
        sorts.insert(target, item)
        return
    if action in {"resize-column", "set-column-visibility", "label-column", "set-formula"}:
        prop = _command_property(command.get("property"))
        column = next((item for item in view["columns"] if item["property"] == prop), None)
        if column is None:
            raise BaseDefinitionError(f"Unknown column: {prop}", path="$.command.property", code="unknown_column")
        if action == "resize-column":
            width = command.get("width")
            if isinstance(width, bool) or not isinstance(width, int) or not 48 <= width <= 800:
                raise BaseDefinitionError("Column width must be an integer between 48 and 800", path="$.command.width", code="invalid_column_width")
            column["width"] = width
            view["columnSize"][prop] = width
        elif action == "set-column-visibility":
            if not isinstance(command.get("visible"), bool):
                raise BaseDefinitionError("Column visibility must be boolean", path="$.command.visible", code="invalid_column_visibility")
            column["visible"] = command["visible"]
        elif action == "label-column":
            label = str(command.get("label") or "").strip()
            if not label or len(label) > 128:
                raise BaseDefinitionError("Column label is invalid", path="$.command.label", code="invalid_column_label")
            column["label"] = label
        else:
            formula = command.get("formula")
            if formula in (None, ""):
                column.pop("formula", None)
            else:
                column["formula"] = str(formula)
                compile_formula(column["formula"])
        return
    if action == "reorder-column":
        source = _command_index(command.get("from_index"), len(view["columns"]), "$.command.from_index")
        target = _command_index(command.get("to_index"), len(view["columns"]), "$.command.to_index")
        column = view["columns"].pop(source)
        view["columns"].insert(target, column)
        return
    if action == "set-filter":
        scope = str(command.get("scope") or "view").strip().lower()
        if scope not in {"root", "view"}:
            raise BaseDefinitionError("Filter scope is invalid", path="$.command.scope", code="invalid_filter_scope")
        parsed = _canonical_filter(command.get("filter", command.get("value")), "$.command.filter")
        if scope == "root":
            definition["globalFilters"] = parsed
        else:
            view["localFilters"] = parsed
            view["filters"] = parsed
        return
    if action == "set-grouping":
        value = command.get("property", command.get("group_by"))
        view["groupBy"] = None if value in (None, "") else _command_property(value, "$.command.property")
        return
    if action == "set-summary":
        prop = _command_property(command.get("property"))
        operation = str(command.get("operation") or "").strip().lower()
        if operation not in {"count", "sum", "avg", "min", "max", "distinct", "none"}:
            raise BaseDefinitionError("Summary operation is invalid", path="$.command.operation", code="invalid_summary")
        if operation == "none":
            view["summaries"].pop(prop, None)
        else:
            view["summaries"][prop] = operation
        return
    if action == "set-limit":
        value = command.get("limit")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SOURCE_ROWS:
            raise BaseDefinitionError("View limit is invalid", path="$.command.limit", code="invalid_limit")
        view["limit"] = value
        return
    if action == "rename-view":
        name = str(command.get("name") or "").strip()
        if not name or len(name) > 128:
            raise BaseDefinitionError("View name is invalid", path="$.command.name", code="invalid_view_name")
        view["name"] = name
        return
    if action == "duplicate-view":
        clone = copy.deepcopy(view)
        clone["id"] = _unique_view_id(definition, command.get("new_view_id"), f"{view['name']}-copy")
        clone["name"] = str(command.get("name") or f"{view['name']} copy")[:128]
        definition["views"].append(clone)
        return
    if action == "add-view":
        name = str(command.get("name") or "").strip()
        if not name or len(name) > 128:
            raise BaseDefinitionError("View name is invalid", path="$.command.name", code="invalid_view_name")
        view_type = str(command.get("view_type", command.get("type", "table"))).lower()
        if view_type not in {"table", "card", "list"}:
            raise BaseDefinitionError("View type is invalid", path="$.command.type", code="unsupported_view")
        template = copy.deepcopy(definition["views"][0])
        template.update({"id": _unique_view_id(definition, command.get("view_id"), name), "name": name, "type": view_type})
        definition["views"].append(template)
        return
    if action == "reorder-view":
        views = definition["views"]
        source = _command_index(command.get("from_index"), len(views), "$.command.from_index")
        target = _command_index(command.get("to_index"), len(views), "$.command.to_index")
        item = views.pop(source)
        views.insert(target, item)
        return
    if action == "remove-view":
        if len(definition["views"]) <= 1:
            raise BaseDefinitionError("The last view cannot be removed", path="$.command.view_id", code="last_view")
        definition["views"] = [item for item in definition["views"] if item is not view]


def transform_base_definition(
    source: str,
    command: dict[str, Any],
    *,
    revision: Any = None,
    expected_revision: Any = None,
) -> dict[str, Any]:
    """Apply one typed Base command to the newest source snapshot.

    This is a pure transformation. It never writes a resource; callers hand
    the returned source and input revision to the shared save queue.
    """
    if not isinstance(command, dict) or any(key not in {"action", "type", "view_id", "property", "direction", "from_index", "to_index", "width", "visible", "label", "formula", "scope", "filter", "value", "group_by", "operation", "limit", "name", "view_type", "new_view_id", "replace"} for key in command):
        raise BaseDefinitionError("Base command is not a bounded object", path="$.command", code="invalid_command")
    if expected_revision is not None and revision != expected_revision:
        raise BaseDefinitionError("Base source revision is stale", code="stale_revision")
    definition, diagnostics = parse_base_definition(source)
    definition._allow_structural_edits = True
    _apply_base_command(definition, command)
    rendered = dump_base_definition(definition)
    reparsed, parse_diagnostics = parse_base_definition(rendered)
    return {
        "source": rendered,
        "definition": reparsed,
        "diagnostics": [*diagnostics, *parse_diagnostics],
        "revision": revision,
        "changed": rendered != source,
    }


def apply_base_command(source: str, command: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """First-party alias used by command adapters and tests."""
    return transform_base_definition(source, command, **kwargs)


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


def _typed_relations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only bounded S01 typed link relation fields used by Base queries.

    Relation payloads are provider-owned input. Invalid or oversized entries
    are discarded as a whole so a valid-looking sibling field cannot smuggle
    an unbounded target into query output.
    """
    relations = doc.get("relations")
    if not isinstance(relations, list):
        return []
    normalized: list[dict[str, Any]] = []
    for relation in relations[:MAX_TYPED_RELATIONS]:
        if not isinstance(relation, dict) or relation.get("kind") not in {"link", "embed"}:
            continue
        target = relation.get("target")
        target_id = relation.get("targetDocumentId", relation.get("target_document_id"))
        if target is not None:
            if not isinstance(target, str) or not target or len(target) > MAX_RELATION_TARGET_BYTES:
                continue
            try:
                if len(target.encode("utf-8")) > MAX_RELATION_TARGET_BYTES:
                    continue
            except UnicodeEncodeError:
                continue
        if target_id is not None:
            if not isinstance(target_id, str) or not target_id or len(target_id) > MAX_RELATION_ID_BYTES:
                continue
            try:
                if len(target_id.encode("utf-8")) > MAX_RELATION_ID_BYTES:
                    continue
            except UnicodeEncodeError:
                continue
        item: dict[str, Any] = {"kind": relation["kind"]}
        if target is not None:
            item["target"] = target
        if target_id is not None:
            item["targetDocumentId"] = target_id
        if len(item) > 1:
            normalized.append(item)
    return normalized


def document_values(doc: dict[str, Any]) -> dict[str, Any]:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    properties = doc.get("properties") if isinstance(doc.get("properties"), dict) else {}
    name = str(doc.get("logical_path") or doc.get("name") or metadata.get("logical_path") or "")
    # String splitting is materially cheaper than constructing three pathlib
    # objects for every row in a warmed 5k-source query.
    basename = name.rsplit("/", 1)[-1]
    folder = name.rsplit("/", 1)[0] if "/" in name else ""
    dot = basename.rfind(".")
    extension = basename[dot + 1:] if dot > 0 else ""
    stem = basename[:dot] if dot > 0 else basename
    source_properties = doc.get("frontmatter") if isinstance(doc.get("frontmatter"), dict) else {}
    # Loose Markdown rows may arrive directly from the bridge with only their
    # source text. Project a valid YAML frontmatter header for query values,
    # while leaving the original source untouched for source-preserving edits.
    raw_text = doc.get("text")
    if not source_properties and isinstance(raw_text, str) and raw_text.startswith(("---\n", "---\r\n")):
        lines = raw_text.splitlines(keepends=True)
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is not None:
            try:
                parsed_frontmatter = yaml.safe_load("".join(lines[1:closing]))
                if isinstance(parsed_frontmatter, dict):
                    source_properties = parsed_frontmatter
            except yaml.YAMLError:
                pass
    frontmatter = {str(key): _coerce(value) for key, value in {**source_properties, **properties}.items()}
    tags = (doc.get("tags") if doc.get("tags") is not None else metadata.get("tags", frontmatter.get("tags", []))) or []
    links = (doc.get("links") if doc.get("links") is not None else metadata.get("links", frontmatter.get("links", []))) or []
    if not links:
        links = [item["target"] for item in _typed_relations(doc) if item["kind"] == "link" and item.get("target")]
    modified = doc.get("ts") or doc.get("mtime") or metadata.get("mtime", metadata.get("modified"))
    created = doc.get("ctime") or metadata.get("ctime", metadata.get("created"))
    size = doc.get("size") if doc.get("size") is not None else metadata.get("size")
    values = dict(frontmatter)
    values.update({
        "name": name,
        "kind": doc.get("kind", metadata.get("kind")),
        "tags": tags,
        "links": links,
        "file.name": basename,
        "file.basename": stem,
        "file.path": name,
        "file.folder": folder,
        "file.ext": extension,
        "file.kind": doc.get("kind", metadata.get("kind")),
        "file.size": size,
        "file.properties": frontmatter,
        "file.ctime": created,
        "file.mtime": modified,
        # Copal's old field remains available while native aliases are used
        # by new imports.
        "file.modified": modified,
        "file.tags": tags,
        "file.links": links,
        "file.link": links[0] if isinstance(links, list) and len(links) == 1 else None,
        "file.day": doc.get("day", metadata.get("day")),
    })
    values.update({
        "basename": stem, "path": name, "folder": folder, "ext": extension,
        "size": size, "properties": frontmatter, "ctime": created, "mtime": modified,
        "link": values["file.link"], "day": values["file.day"],
    })
    values.update({f"note.{key}": value for key, value in frontmatter.items()})
    return values


def get_property(values: dict[str, Any], prop: str) -> Any:
    if prop in values:
        return values[prop]
    if prop.startswith("properties."):
        return values.get(prop.removeprefix("properties."))
    if prop.startswith("note."):
        return values.get(prop)
    if prop.startswith("formula."):
        return values.get(prop)
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
    if op in {"has_tag", "has_link"}:
        if not isinstance(actual, (list, tuple, set)):
            return False
        expected_text = str(expected).casefold().lstrip("#") if op == "has_tag" else str(expected).casefold()
        return any(
            (str(item).casefold().lstrip("#") if op == "has_tag" else str(item).casefold()) == expected_text
            for item in actual
        )
    if op == "starts_with":
        return str(actual or "").casefold().startswith(str(expected).casefold())
    if op == "ends_with":
        return str(actual or "").casefold().endswith(str(expected).casefold())
    if op == "in":
        return any(_compare(actual, item, "eq") for item in (expected if isinstance(expected, list) else [expected]))
    if op == "in_folder":
        try:
            folder = _normalize_logical_path(expected, "$.filters.value")
            candidate = _normalize_logical_path(actual, "$.row.logical_path")
        except BaseDefinitionError:
            return False
        return candidate == folder or candidate.startswith(f"{folder}/")
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


def _and_filters(global_filter: dict[str, Any] | None, local_filter: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the one effective predicate used by a view evaluation."""
    if global_filter and local_filter:
        return {"and": [global_filter, local_filter]}
    return global_filter or local_filter


def _required_folder_rules(rule: dict[str, Any] | None) -> list[str]:
    """Extract safe positive folder clauses for a cheap row pre-check."""
    if not rule:
        return []
    if rule.get("operator") == "in_folder":
        return [str(rule.get("value") or "")]
    if "and" in rule:
        found: list[str] = []
        for child in rule["and"]:
            found.extend(_required_folder_rules(child))
        return found
    # A folder under OR/NOT cannot be used as a rejecting pre-check.
    return []


_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod}
_CMP_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le}


@dataclass(frozen=True)
class Formula:
    source: str
    tree: ast.Expression


def compile_formula(source: str) -> Formula:
    if len(source) > 512:
        raise BaseDefinitionError("Formula exceeds 512 characters", code="formula_too_large", source=source)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise BaseDefinitionError(f"Invalid formula: {exc.msg}", code="formula_syntax", source=source) from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_EXPRESSION_NODES:
        raise BaseDefinitionError("Formula is too complex", code="formula_too_complex", source=source)
    allowed = (
        ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.BinOp, ast.UnaryOp,
        ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Load,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd, ast.Not,
        ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
    )
    if any(not isinstance(node, allowed) for node in nodes):
        bad = next(node for node in nodes if not isinstance(node, allowed))
        raise BaseDefinitionError(f"Formula operation is not allowed: {type(bad).__name__}", code="formula_unsafe", source=source)
    for node in nodes:
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in {"lower", "upper", "length", "coalesce", "round", "abs"}):
            raise BaseDefinitionError("Formula call is not allowed", code="formula_unsafe", source=source)
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
    documents: Iterable[dict[str, Any]] | dict[str, Any],
    *,
    view_id: str | None = None,
    query: str = "",
    page: int = 1,
    page_size: int = 100,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    views = definition["views"]
    view = next((item for item in views if item["id"] == view_id), views[0]) if view_id else views[0]
    # S01 hands us an already-authorized snapshot. A dict input is accepted as
    # a convenient DTO form; it is never interpreted as a filesystem path.
    snapshot = documents if isinstance(documents, dict) else corpus
    if isinstance(documents, dict):
        documents = documents.get("rows", [])
    snapshot_complete = snapshot.get("complete", True) if isinstance(snapshot, dict) else True
    snapshot_status = snapshot.get("status") if isinstance(snapshot, dict) else None
    snapshot_generation = snapshot.get("generation") if isinstance(snapshot, dict) else None
    snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, dict) else None
    if snapshot_complete is False:
        return {
            "view": view, "rows": [], "groups": [], "summaries": {}, "page": max(1, page),
            "pageSize": max(1, min(MAX_PAGE_SIZE, page_size)), "total": 0, "pages": 1,
            "sourceCount": 0, "sourceTruncated": False, "queryComplete": False,
            "queryStatus": snapshot_status or "indexing_incomplete", "indexingStatus": snapshot_status or "incomplete",
            "snapshotId": snapshot_id, "generation": snapshot_generation,
            "matchedCount": 0, "resultLimit": view["limit"], "resultLimited": False, "summaryScope": "limitedResult",
        }
    effective_filter = _and_filters(definition.get("globalFilters"), view.get("localFilters", view.get("filters")))
    required_folders = _required_folder_rules(effective_filter)
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
        if required_folders:
            logical_path = doc.get("logical_path") or doc.get("name")
            try:
                normalized_row_path = _normalize_logical_path(logical_path, "$.row.logical_path")
            except BaseDefinitionError:
                normalized_row_path = None
            if normalized_row_path is None or any(
                not (normalized_row_path == folder or normalized_row_path.startswith(f"{folder}/"))
                for folder in required_folders
            ):
                continue
        values = document_values(doc)
        errors = []
        for prop, formula in formulas.items():
            try:
                values[prop] = evaluate_formula(formula, values)
            except Exception as exc:
                values[prop] = None
                errors.append({"property": prop, "message": str(exc)[:200]})
        needle = str(query or "").strip().casefold()
        searchable = " ".join([str(doc.get("name") or ""), *(str(value) for value in values.values())]).casefold()
        if matches_filter(values, effective_filter) and (not needle or needle in searchable):
            row = {
                "documentId": doc.get("id"),
                "head": doc.get("head"),
                "name": doc.get("name") or values.get("name"),
                "kind": doc.get("kind") or values.get("kind"),
                "values": values,
                "errors": errors,
            }
            # Preserve the S01 identity/revision handoff on result rows. These
            # are data descriptors only; this module never authorizes or opens
            # a provider resource.
            for key in ("resource_key", "resource_ref", "revision", "logical_path", "metadata", "properties"):
                if key in doc:
                    row[key] = doc[key]
            typed_relations = _typed_relations(doc)
            row["relations"] = typed_relations
            rows.append(row)

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        for sort in view["sorts"]:
            a = _comparable(left["values"].get(sort["property"]))
            b = _comparable(right["values"].get(sort["property"]))
            if a != b:
                result = -1 if a < b else 1
                return result if sort["direction"] == "asc" else -result
        return -1 if str(left["documentId"]) < str(right["documentId"]) else int(str(left["documentId"]) > str(right["documentId"]))

    rows.sort(key=cmp_to_key(compare))
    matched_count = len(rows)
    # A saved-view limit chooses the globally sorted top rows within the
    # scanned source. It must never short-circuit source enumeration.
    rows = rows[:view["limit"]]
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
        "queryComplete": not truncated_source,
        "matchedCount": matched_count,
        "resultLimit": view["limit"],
        "resultLimited": matched_count > view["limit"],
        "summaryScope": "limitedResult",
        "snapshotId": snapshot_id,
        "generation": snapshot_generation,
        "queryStatus": "bounded" if truncated_source else "complete",
        "indexingStatus": "incomplete" if truncated_source else "complete",
    }
