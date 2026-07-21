from datetime import date
from pathlib import Path

import pytest

from src.openclank.copal_bases import (
    BaseDefinitionError,
    document_values,
    dump_base_definition,
    parse_base_definition,
    query_base,
    set_frontmatter_property,
)


ROOT = Path(__file__).resolve().parents[1]


def _doc(document_id, name, **properties):
    return {
        "id": document_id,
        "head": f"head-{document_id}",
        "name": name,
        "kind": "markdown",
        "ts": "2026-07-10T12:00:00Z",
        "frontmatter": {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in properties.items()},
        "tags": properties.get("tags", []),
        "links": [],
    }


def test_bundled_legacy_fixture_migrates_with_explicit_diagnostics():
    content = (ROOT / "packages/Copal/sample-vault/Projects/To Watch.base").read_text()
    definition, diagnostics = parse_base_definition(content)
    view = definition["views"][0]

    assert definition["version"] == 1
    assert [column["property"] for column in view["columns"]] == ["file.name", "status", "tags"]
    assert view["sorts"] == [{"property": "file.tags", "direction": "asc"}]
    assert any(item["code"] == "legacy_nested_view_fields" for item in diagnostics)
    assert any(item["code"] == "legacy_version" for item in diagnostics)


def test_canonical_round_trip_preserves_extensions_without_nesting():
    source = {
        "version": 1,
        "views": [{
            "id": "table", "name": "Table", "type": "table",
            "columns": ["file.name"], "mysteryViewSetting": {"future": True},
        }],
        "futureRootSetting": [1, 2, 3],
    }
    first, _ = parse_base_definition(dump_base_definition(source))
    second, _ = parse_base_definition(dump_base_definition(first))
    assert second == first
    assert first["extensions"]["futureRootSetting"] == [1, 2, 3]
    assert first["views"][0]["extensions"]["mysteryViewSetting"] == {"future": True}


def test_live_query_filters_formulas_multisorts_groups_summaries_and_pages():
    definition, _ = parse_base_definition("""
version: 1
views:
  - id: inventory
    name: Inventory
    type: table
    columns:
      - file.name
      - category
      - property: total
        label: Total
        formula: price * quantity
    filters:
      and:
        - property: active
          operator: eq
          value: true
        - property: category
          operator: contains
          value: tool
    sorts:
      - property: total
        direction: desc
      - property: file.name
        direction: asc
    groupBy: category
    summaries:
      total: sum
""")
    documents = [
        _doc("A", "Hammer.md", category="Tools", price=4, quantity=3, active=True),
        _doc("B", "Saw.md", category="Tools", price=8, quantity=2, active=True),
        _doc("C", "Paint.md", category="Supplies", price=9, quantity=9, active=True),
        _doc("D", "Old.md", category="Tools", price=100, quantity=1, active=False),
        {"id": "BASE", "kind": "base", "name": "Inventory.base", "frontmatter": {}},
    ]
    result = query_base(definition, documents, view_id="inventory", page=1, page_size=1)

    assert result["total"] == 2
    assert result["pages"] == 2
    assert result["rows"][0]["name"] == "Saw.md"
    assert result["rows"][0]["values"]["total"] == 16
    assert result["groups"][0]["key"] == "Tools"
    assert result["summaries"]["total"] == 28


def test_document_values_fast_scalar_coercion_preserves_yaml_semantics():
    values = document_values({
        "name": "Scalars.md",
        "kind": "markdown",
        "frontmatter": {
            "plain": "ordinary prose",
            "truthy": "YES",
            "falsey": "off",
            "nothing": "null",
            "integer": "1_024",
            "decimal": "-3.5e2",
            "date": "2026-07-10",
            "quoted": '"quoted value"',
            "items": '["one", 2]',
            "mapping": "{one: 1}",
        },
    })

    assert values["plain"] == "ordinary prose"
    assert values["truthy"] is True
    assert values["falsey"] is False
    assert values["nothing"] is None
    assert values["integer"] == 1024
    assert values["decimal"] == -350.0
    assert values["date"] == date(2026, 7, 10)
    assert values["quoted"] == "quoted value"
    assert values["items"] == ["one", 2]
    assert values["mapping"] == {"one": 1}


def test_formula_language_rejects_calls_and_attribute_tricks():
    with pytest.raises(BaseDefinitionError) as exc:
        parse_base_definition("""
version: 1
views:
  - name: Unsafe
    columns:
      - property: bad
        formula: __import__('os').system('id')
""")
    assert exc.value.diagnostics[0]["code"] == "formula_unsafe"


def test_frontmatter_edit_is_revision_payload_friendly_and_rejects_file_fields():
    updated = set_frontmatter_property("---\nstatus: old\n---\n# Note\n", "status", "new")
    assert 'status: "new"' in updated
    assert updated.endswith("# Note\n")
    inserted = set_frontmatter_property("# Note", "score", 7)
    assert inserted.startswith("---\nscore: 7\n---\n")
    with pytest.raises(BaseDefinitionError):
        set_frontmatter_property("# Note", "file.name", "nope")


def test_card_and_list_view_types_are_accepted():
    definition, _ = parse_base_definition("""
version: 1
views:
  - name: Cards
    type: card
    columns:
      - file.name
      - status
  - name: List
    type: list
    columns:
      - file.name
""")
    assert definition["views"][0]["type"] == "card"
    assert definition["views"][1]["type"] == "list"


def test_unsupported_view_type_rejected():
    with pytest.raises(BaseDefinitionError) as exc:
        parse_base_definition("""
version: 1
views:
  - name: Board
    type: board
    columns:
      - file.name
""")
    assert exc.value.diagnostics[0]["code"] == "unsupported_view"


def test_view_type_preserved_through_round_trip():
    source = {
        "version": 1,
        "views": [
            {"name": "Cards", "type": "card", "columns": ["file.name"]},
            {"name": "List", "type": "list", "columns": ["file.name"]},
        ],
    }
    first, _ = parse_base_definition(dump_base_definition(source))
    assert first["views"][0]["type"] == "card"
    assert first["views"][1]["type"] == "list"
    second, _ = parse_base_definition(dump_base_definition(first))
    assert second == first


def test_nested_filter_preserved_through_canonicalization():
    definition, _ = parse_base_definition("""
version: 1
views:
  - name: Nested
    columns:
      - file.name
    filters:
      and:
        - property: status
          operator: eq
          value: active
        - or:
          - property: category
            operator: eq
            value: tools
          - property: category
            operator: eq
            value: supplies
""")
    filters = definition["views"][0]["filters"]
    assert "and" in filters
    assert len(filters["and"]) == 2
    assert "or" in filters["and"][1]
    assert len(filters["and"][1]["or"]) == 2


def test_query_base_works_with_card_view():
    definition, _ = parse_base_definition("""
version: 1
views:
  - name: Cards
    type: card
    columns:
      - file.name
      - status
""")
    documents = [
        _doc("A", "Task1.md", status="active"),
        _doc("B", "Task2.md", status="done"),
    ]
    result = query_base(definition, documents, page=1, page_size=10)
    assert result["total"] == 2
    assert result["view"]["type"] == "card"
