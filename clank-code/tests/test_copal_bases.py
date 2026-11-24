from datetime import date
from pathlib import Path

import pytest

from src.openclank.copal_bases import (
    BaseDefinitionError,
    apply_base_command,
    _typed_relations,
    document_values,
    dump_base_definition,
    parse_base_definition,
    query_base,
    set_frontmatter_property,
    transform_base_definition,
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
    # An untouched legacy source remains byte-for-byte exportable.  Explicit
    # commands may cross its malformed-layout migration boundary, but retain
    # every recovered predicate and source column identity in the parsed
    # result.
    assert dump_base_definition(definition) == content
    sorted_source = transform_base_definition(content, {
        "type": "set-sort", "view_id": "table", "property": "file.tags", "direction": "desc",
    })["source"]
    sorted_definition, _ = parse_base_definition(sorted_source)
    assert sorted_definition["views"][0]["sorts"] == [{"property": "file.tags", "direction": "desc"}]
    assert sorted_definition["views"][0]["localFilters"] == {
        "and": [
            {"property": "status", "operator": "exists"},
            {"property": "status", "operator": "eq", "value": "unwatched"},
            {"property": "status", "operator": "exists"},
            {"property": "status", "operator": "eq", "value": "unwatched"},
        ]
    }
    reordered_source = transform_base_definition(sorted_source, {
        "action": "reorder-column", "view_id": "table", "from_index": 2, "to_index": 0,
    })["source"]
    reordered_definition, _ = parse_base_definition(reordered_source)
    assert [column["property"] for column in reordered_definition["views"][0]["columns"]] == ["tags", "file.name", "status"]
    resized_source = transform_base_definition(reordered_source, {
        "action": "resize-column", "view_id": "table", "property": "status", "width": 220,
    })["source"]
    resized_definition, _ = parse_base_definition(resized_source)
    assert resized_definition["views"][0]["columnSize"] == {"status": 220}


def test_saved_limit_selects_sorted_top_rows_before_paging():
    definition, _ = parse_base_definition("""
version: 1
views:
  - id: ranked
    type: table
    columns: [file.name, score]
    sorts: [{property: score, direction: desc}]
    summaries: {score: sum}
    limit: 2
""")
    documents = [_doc("low", "Low.md", score=1), _doc("b", "B.md", score=9), _doc("a", "A.md", score=9)]
    result = query_base(definition, documents, page=2, page_size=1)
    assert [row["documentId"] for row in result["rows"]] == ["b"]
    assert result["total"] == 2
    assert result["matchedCount"] == result["sourceCount"] == 3
    assert result["summaries"] == {"score": 18}
    assert result["summaryScope"] == "limitedResult"
    assert result["resultLimited"] is True
    assert result["resultLimit"] == 2
    assert result["queryComplete"] is True


def test_scan_cap_is_distinct_from_saved_view_limit(monkeypatch):
    monkeypatch.setattr("src.openclank.copal_bases.MAX_SOURCE_ROWS", 2)
    definition, _ = parse_base_definition("""
version: 1
views:
  - id: ranked
    type: table
    columns: [score]
    sorts: [{property: score, direction: desc}]
    limit: 1
""")
    result = query_base(definition, [_doc("low", "Low", score=1), _doc("high", "High", score=9), _doc("unscanned", "Unscanned", score=99)])
    assert result["rows"][0]["documentId"] == "high"
    assert result["sourceCount"] == 2
    assert result["sourceTruncated"] is True
    assert result["queryComplete"] is False
    assert result["queryStatus"] == "bounded"
    assert result["indexingStatus"] == "incomplete"
    assert result["resultLimited"] is True


def test_large_source_query_is_complete_past_five_thousand_documents():
    definition, _ = parse_base_definition("""
version: 1
views:
  - id: all
    type: table
    columns: [file.name, score]
    sorts: [{property: score, direction: asc}]
    limit: 10000
""")
    documents = [_doc(f"source-{index}", f"Source {index}.md", score=index) for index in range(5_001)]
    result = query_base(definition, documents, page=11, page_size=500)
    assert result["sourceCount"] == result["matchedCount"] == 5_001
    assert result["queryComplete"] is True
    assert result["sourceTruncated"] is False
    assert len(result["rows"]) == 1
    assert result["rows"][0]["documentId"] == "source-5000"


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
    commented = set_frontmatter_property("---\r\nstatus: old # retain\r\ntags: [one]\r\n---\r\nBody\r\n", "status", "new")
    assert commented == "---\r\nstatus: \"new\" # retain\r\ntags: [one]\r\n---\r\nBody\r\n"
    assert set_frontmatter_property(commented, "status", None).splitlines()[1] == 'status: null # retain'
    cleared = set_frontmatter_property(commented, "status", None, remove=True)
    assert cleared == "---\r\ntags: [one]\r\n---\r\nBody\r\n"
    assert set_frontmatter_property(cleared, "status", None, remove=True) == cleared
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


def test_native_in_folder_uses_exact_and_descendant_boundaries():
    definition, diagnostics = parse_base_definition('''
version: 1
filters: file.inFolder("Projects/Obsidian/Templates")
views:
  - name: Markdown
    columns: [file.name]
    filters: kind == "markdown"
''')
    assert not diagnostics
    assert definition["globalFilters"] == {
        "property": "file.path", "operator": "in_folder", "value": "Projects/Obsidian/Templates"
    }
    rows = [
        {"id": "folder", "name": "Projects/Obsidian/Templates", "logical_path": "Projects/Obsidian/Templates", "kind": "folder"},
        {"id": "child", "name": "child.md", "logical_path": "Projects/Obsidian/Templates/child.md", "kind": "markdown"},
        {"id": "sibling", "name": "old.md", "logical_path": "Projects/Obsidian/Templates-old/old.md", "kind": "markdown"},
    ]
    assert [row["documentId"] for row in query_base(definition, rows)["rows"]] == ["child"]


def test_native_boolean_expression_quotes_unicode_and_scope_are_preserved():
    definition, _ = parse_base_definition('''
version: 1
filters: status == "global" && (kind == "markdown" || kind == "note")
views:
  - name: Local
    columns: [file.name]
    filters: "!file.hasProperty('missing_quoted')"
''')
    assert definition["globalFilters"]["and"][1]["or"][0]["value"] == "markdown"
    assert definition["views"][0]["localFilters"]["not"]["operator"] == "exists"
    docs = [
        {"id": "ok", "name": "日本語.md", "logical_path": "日本語/日本語.md", "kind": "markdown", "properties": {"status": "global"}},
        {"id": "bad", "name": "bad.md", "logical_path": "日本語/bad.md", "kind": "markdown", "properties": {"status": "local"}},
    ]
    assert [row["documentId"] for row in query_base(definition, docs)["rows"]] == ["ok"]


@pytest.mark.parametrize("folder", ["/Projects/Templates", "../Templates", "Projects/../Templates", "C:/Templates", "Projects\\Templates"])
def test_in_folder_rejects_authority_or_traversal_syntax(folder):
    with pytest.raises(BaseDefinitionError) as exc:
        parse_base_definition(f'''version: 1\nfilters: file.inFolder("{folder}")\nviews: [{{columns: [file.name]}}]\n''')
    assert exc.value.diagnostics[0]["code"] == "invalid_folder"


def test_authorized_row_dto_identity_and_incomplete_snapshot_are_truthful():
    definition, _ = parse_base_definition("version: 1\nviews: [{columns: [file.path, file.size, note.status]}]\n")
    row = {"resource_key": "rk", "resource_ref": "rr", "revision": {"kind": "hostFingerprint", "value": "v1"},
           "logical_path": "Folder/Note.md", "kind": "markdown", "properties": {"status": "ready"}, "size": 17}
    result = query_base(definition, {"snapshot_id": "snap", "generation": 3, "complete": True, "rows": [row]})
    assert result["queryComplete"] is True
    assert result["rows"][0]["resource_ref"] == "rr"
    assert result["rows"][0]["values"]["file.basename"] == "Note"
    incomplete = query_base(definition, {"snapshot_id": "snap", "generation": 3, "complete": False, "status": "indexing"})
    assert incomplete["queryComplete"] is False
    assert incomplete["indexingStatus"] == "indexing"
    assert incomplete["rows"] == []


def test_native_view_order_and_column_sizes_map_to_typed_columns():
    definition, _ = parse_base_definition('''
version: 1
views:
  - order: [file.name, status]
    columnSize: {file.name: 220, status: 12}
''')
    view = definition["views"][0]
    assert [column["property"] for column in view["columns"]] == ["file.name", "status"]
    assert view["columnSize"] == {"file.name": 220, "status": 48}
    aliased, _ = parse_base_definition("version: 1\nviews: [{columns: [note.status, properties.tags, formula.total, file.tags], columnSize: {note.status: 82, properties.tags: 90}}]\n")
    aliased_view = aliased["views"][0]
    assert [column["property"] for column in aliased_view["columns"]] == ["status", "tags", "formula.total", "file.tags"]
    assert aliased_view["columnSize"] == {"status": 82, "tags": 90}
    assert parse_base_definition(dump_base_definition(definition))[0] == definition


def test_actual_to_watch_source_is_noop_exact_and_structural_commands_reparse():
    fixture = ROOT / ".references/copal/brainvault-20260719-095420/Projects/To Watch.base"
    source = fixture.read_text() if fixture.exists() else '''views:
  - type: table
    name: Table
    filters:
      and:
        - file.hasProperty("status")
        - status == "unwatched"
        - not:
            - file.inFolder("Projects/Obsidian/Templates")
    order:
      - file.name
      - file.tags
      - status
    sort:
      - property: file.tags
        direction: ASC
      - property: file.name
        direction: ASC
    columnSize:
      note.status: 82
'''
    definition, diagnostics = parse_base_definition(source)
    assert diagnostics == []
    assert dump_base_definition(definition) == source
    view = definition["views"][0]
    assert view["columnSize"] == {"status": 82}
    source_order = source.split("    order:\n", 1)[1].split("    sort:\n", 1)[0]
    resized_raw = transform_base_definition(source, {
        "action": "resize-column", "view_id": view["id"], "property": "status", "width": 220,
    })["source"]
    assert resized_raw.split("    order:\n", 1)[1].split("    sort:\n", 1)[0] == source_order
    assert "note.status: 220" in resized_raw and "width:" not in resized_raw
    reordered_raw = transform_base_definition(source, {
        "action": "reorder-column", "view_id": view["id"], "from_index": 1, "to_index": 0,
    })["source"]
    reordered_order = reordered_raw.split("    order:\n", 1)[1].split("    sort:\n", 1)[0]
    assert "[file.tags, file.name, status]" in reordered_order and "width:" not in reordered_order
    labeled_raw = transform_base_definition(source, {
        "action": "label-column", "view_id": view["id"], "property": "status", "label": "State",
    })["source"]
    assert "{label: State, property: status}" in labeled_raw and "width:" not in labeled_raw
    hidden_raw = transform_base_definition(source, {
        "action": "set-column-visibility", "view_id": view["id"], "property": "status", "visible": False,
    })["source"]
    assert "{property: status, visible: false}" in hidden_raw and "width:" not in hidden_raw
    direction_only = transform_base_definition(source, {
        "action": "set-sort", "view_id": view["id"], "property": "file.tags", "direction": "desc",
    })
    direction_view = direction_only["definition"]["views"][0]
    assert [item["property"] for item in direction_view["sorts"]] == ["file.tags", "file.name"]
    assert [item["direction"] for item in direction_view["sorts"]] == ["desc", "asc"]
    changed = source
    for command in (
        {"action": "set-sort", "view_id": view["id"], "property": "status", "direction": "desc"},
        {"action": "reorder-column", "view_id": view["id"], "from_index": 1, "to_index": 0},
        {"action": "resize-column", "view_id": view["id"], "property": "status", "width": 220},
    ):
        changed = transform_base_definition(changed, command)["source"]
        transformed, transformed_diagnostics = parse_base_definition(changed)
        assert transformed_diagnostics == []
        assert all(not diagnostic.get("code") == "parse_error" for diagnostic in transformed_diagnostics)
    transformed = parse_base_definition(changed)[0]["views"][0]
    assert transformed["localFilters"]["and"][0]["operator"] == "exists"
    assert transformed["localFilters"]["and"][1] == {"property": "status", "operator": "eq", "value": "unwatched"}
    assert transformed["localFilters"]["and"][2]["not"]["and"][0]["value"] == "Projects/Obsidian/Templates"
    assert [item["property"] for item in transformed["columns"]] == ["file.tags", "file.name", "status"]
    assert [item["property"] for item in transformed["sorts"]] == ["file.tags", "file.name", "status"]
    assert [item["direction"] for item in transformed["sorts"]] == ["asc", "asc", "desc"]
    assert transformed["columnSize"] == {"status": 220}


def test_no_version_native_shape_uses_global_and_local_filters():
    definition, _ = parse_base_definition('''
filters: status == "global"
views:
  - name: Markdown
    columns: [file.name]
    filters: kind == "markdown"
''')
    assert definition["sourceVersion"] == 1
    assert definition["globalFilters"]["property"] == "status"
    assert definition["views"][0]["localFilters"]["property"] == "kind"
    docs = [
        {"id": "g-markdown", "name": "g-markdown.md", "kind": "markdown", "properties": {"status": "global"}},
        {"id": "only-kind", "name": "only-kind.md", "kind": "markdown", "properties": {"status": "local"}},
        {"id": "g-note", "name": "g-note.md", "kind": "note", "properties": {"status": "global"}},
    ]
    assert [row["documentId"] for row in query_base(definition, docs)["rows"]] == ["g-markdown"]


def test_separate_column_size_source_stays_separate_for_all_column_commands():
    source = '''views:
  - id: sheet
    columns: [note.status, file.name]
    columnSize: {note.status: 82, file.name: 180}
'''
    definition, diagnostics = parse_base_definition(source)
    assert diagnostics == []
    view_id = definition["views"][0]["id"]
    for command in (
        {"action": "resize-column", "view_id": view_id, "property": "note.status", "width": 220},
        {"action": "reorder-column", "view_id": view_id, "from_index": 1, "to_index": 0},
        {"action": "label-column", "view_id": view_id, "property": "status", "label": "State"},
        {"action": "set-column-visibility", "view_id": view_id, "property": "status", "visible": False},
    ):
        transformed = transform_base_definition(source, command)["source"]
        reparsed, parse_diagnostics = parse_base_definition(transformed)
        assert parse_diagnostics == []
        assert "width:" not in transformed
        assert reparsed["views"][0]["columnSize"]["status"] in {82, 220}
    resized = transform_base_definition(source, {
        "action": "resize-column", "view_id": view_id, "property": "note.status", "width": 220,
    })["source"]
    assert "columnSize: {note.status: 220, file.name: 180}" in resized


def test_unchanged_native_source_is_exact_and_supported_edit_keeps_comments_and_extensions():
    source = '''# preserve this comment
filters: status == "global"
futureRoot: {enabled: true}
views:
  - name: Markdown
    columns: [file.name]
    futureView: "keep me"
    filters: kind == "markdown"
'''
    definition, _ = parse_base_definition(source)
    assert dump_base_definition(definition) == source
    definition["views"][0]["name"] = "Renamed"
    edited = dump_base_definition(definition)
    assert edited.startswith("# preserve this comment\n")
    assert "futureRoot" in edited and "futureView" in edited
    assert "globalFilters" not in edited and "localFilters" not in edited
    assert parse_base_definition(edited)[0]["globalFilters"]["property"] == "status"


def test_native_collection_predicates_match_tags_and_links_safely():
    definition, _ = parse_base_definition('''
filters: file.hasTag("#work") && file.hasLink("日本語.md")
views: [{columns: [file.name]}]
''')
    row = {"id": "match", "name": "match.md", "kind": "markdown", "tags": ["work", "other"], "links": ["日本語.md"]}
    other = {"id": "miss", "name": "miss.md", "kind": "markdown", "tags": ["work"], "links": []}
    assert [item["documentId"] for item in query_base(definition, [row, other])["rows"]] == ["match"]


def test_legacy_parser_decodes_unicode_escapes_and_bounds_source_work():
    definition, diagnostics = parse_base_definition(
        r'''filters: file.inFolder("Projects/\u65e5\u672c語/Templates")
views: [{columns: [file.name]}]
'''.replace(r'\"', '"')
    )
    assert not diagnostics
    assert definition["globalFilters"]["value"] == "Projects/日本語/Templates"
    astral, _ = parse_base_definition(r'''filters: file.inFolder("Projects/\uD83D\uDE00")
views: [{columns: [file.name]}]
'''.replace(r'\"', '"'))
    assert astral["globalFilters"]["value"] == "Projects/😀"
    with pytest.raises(BaseDefinitionError) as exc:
        parse_base_definition(f"filters: {'status == 1 && ' * 200}status == 1\nviews: [{{columns: [file.name]}}]\n")
    assert exc.value.diagnostics[0]["code"] in {"filter_too_large", "filter_too_complex"}


def test_folder_prefilter_normalizes_authorized_logical_paths():
    definition, _ = parse_base_definition('filters: file.inFolder("Projects/Templates")\nviews: [{columns: [file.name]}]\n')
    row = {"id": "normalized", "name": "child.md", "logical_path": "Projects//Templates/child.md"}
    assert [item["documentId"] for item in query_base(definition, [row])["rows"]] == ["normalized"]


def test_unsupported_filter_has_precise_source_diagnostic_without_widening():
    source = 'version: 1\nfilters: file.unknown == "x"\nviews: [{columns: [file.name]}]\n'
    with pytest.raises(BaseDefinitionError) as exc:
        parse_base_definition(source)
    diagnostic = exc.value.diagnostics[0]
    assert diagnostic["path"] == "$.filters"
    assert diagnostic["code"] == "unsupported_property"
    assert "file.unknown" in str(diagnostic["message"])


def test_source_map_edits_root_and_local_filters_without_losing_inline_comments():
    source = '''# root comment
filters: status == "global" # root filter comment
futureRoot: {enabled: true} # root extension
views:
  - name: Markdown # view comment
    columns: [file.name] # columns comment
    futureView: keep # view extension
    filters: kind == "markdown" # local filter comment
'''
    definition, _ = parse_base_definition(source)
    definition["globalFilters"]["value"] = "changed"
    definition["views"][0]["localFilters"]["value"] = "note"
    definition["views"][0]["name"] = "Renamed"
    saved = dump_base_definition(definition)
    assert '# root comment' in saved and '# root filter comment' in saved
    assert '# view comment' in saved and '# local filter comment' in saved
    assert 'futureRoot: {enabled: true}' in saved and 'futureView: keep' in saved
    assert 'globalFilters:' not in saved and 'localFilters:' not in saved and 'sourceVersion:' not in saved
    reparsed, _ = parse_base_definition(saved)
    assert reparsed["globalFilters"]["value"] == "changed"
    assert reparsed["views"][0]["localFilters"]["value"] == "note"


def test_source_map_rejects_unsafe_structural_edits_instead_of_rewriting():
    definition, _ = parse_base_definition('filters: status == "global"\nviews: [{columns: [file.name]}]\n')
    definition["views"][0]["columns"].append({"property": "status", "label": "status"})
    with pytest.raises(BaseDefinitionError) as exc:
        dump_base_definition(definition)
    assert exc.value.diagnostics[0]["code"] == "unsupported_edit"


def test_typed_relations_feed_has_link_without_untrusted_relation_fields():
    definition, _ = parse_base_definition('filters: file.hasLink("日本語.md")\nviews: [{columns: [file.name]}]\n')
    result = query_base(definition, [{"id": "ok", "name": "ok.md", "relations": [
        {"kind": "link", "target": "日本語.md", "providerUrl": "file:///secret"},
        {"kind": "unknown", "target": "bad.md"},
    ]}])
    assert result["rows"][0]["relations"] == [{"kind": "link", "target": "日本語.md"}]


def test_source_projection_does_not_hide_nested_extension_filter_edits():
    definition, _ = parse_base_definition('''
filters: status == "global"
extensions:
  plugin:
    filters: original extension payload
views: [{columns: [file.name]}]
''')
    definition["extensions"]["plugin"]["filters"] = "changed extension payload"
    with pytest.raises(BaseDefinitionError) as exc:
        dump_base_definition(definition)
    assert exc.value.diagnostics[0]["path"] == "$.extensions"
    assert exc.value.diagnostics[0]["code"] == "unsupported_edit"


@pytest.mark.parametrize("value", ["x # y", "x: y", 'embedded "quote"', "日本語"])
def test_scalar_filter_edits_are_quoted_yaml_scalars_and_roundtrip(value):
    definition, _ = parse_base_definition('filters: status == "old" # retain this comment\nviews: [{columns: [file.name]}]\n')
    definition["globalFilters"]["value"] = value
    saved = dump_base_definition(definition)
    assert "# retain this comment" in saved
    reparsed, _ = parse_base_definition(saved)
    assert reparsed["globalFilters"]["value"] == value


def test_structured_not_filter_edit_is_quoted_and_roundtrips():
    definition, _ = parse_base_definition('filters: status == "old" # retain this comment\nviews: [{columns: [file.name]}]\n')
    definition["globalFilters"] = {"not": {"property": "status", "operator": "eq", "value": "x"}}
    saved = dump_base_definition(definition)
    assert 'filters: "!' in saved
    reparsed, _ = parse_base_definition(saved)
    assert reparsed["globalFilters"] == definition["globalFilters"]


def test_filter_structure_edits_reject_when_source_shape_cannot_be_preserved():
    definition, _ = parse_base_definition('''
filters:
  and:
    - property: status
      operator: eq
      value: global
views: [{columns: [file.name]}]
''')
    definition["globalFilters"]["and"].append({"property": "kind", "operator": "eq", "value": "markdown"})
    with pytest.raises(BaseDefinitionError) as exc:
        dump_base_definition(definition)
    assert exc.value.diagnostics[0]["path"] == "$.globalFilters.and"
    assert exc.value.diagnostics[0]["code"] == "unsupported_edit"


def test_typed_base_command_preserves_source_comments_extensions_and_revision():
    source = '''# keep root comment
filters: status == "global" # keep filter comment
futureRoot: {enabled: true}
views:
  - id: watch
    name: To Watch # keep view comment
    columns: [file.name, status] # keep columns comment
    sorts: [{property: status, direction: asc}]
    futureView: "preserve me"
'''
    result = transform_base_definition(source, {
        "action": "set-sort", "view_id": "watch", "property": "status", "direction": "desc",
    }, revision={"head": "h1"}, expected_revision={"head": "h1"})
    assert result["revision"] == {"head": "h1"}
    assert result["changed"] is True
    assert "# keep root comment" in result["source"]
    assert "# keep filter comment" in result["source"]
    assert "# keep view comment" in result["source"]
    assert "futureRoot" in result["source"] and "futureView" in result["source"]
    parsed, _ = parse_base_definition(result["source"])
    assert parsed["views"][0]["sorts"] == [{"property": "status", "direction": "desc"}]
    noop = apply_base_command(source, {"action": "set-limit", "view_id": "watch", "limit": 1000})
    assert noop["source"] == source
    with pytest.raises(BaseDefinitionError) as exc:
        transform_base_definition(source, {"action": "set-limit", "limit": 10}, revision={"head": "h2"}, expected_revision={"head": "h1"})
    assert exc.value.diagnostics[0]["code"] == "stale_revision"


def test_base_commands_cover_columns_filters_summaries_formula_and_view_lifecycle():
    source = '''version: 1
filters: status == "global"
views:
  - id: watch
    name: To Watch
    columns: [file.name, status]
    filters: kind == "markdown"
    sorts: [{property: status, direction: asc}, {property: file.name, direction: desc}]
    groupBy: status
    summaries: {status: count}
    limit: 100
'''
    commands = [
        {"action": "resize-column", "view_id": "watch", "property": "status", "width": 220},
        {"action": "label-column", "view_id": "watch", "property": "status", "label": "Status label"},
        {"action": "set-column-visibility", "view_id": "watch", "property": "status", "visible": False},
        {"action": "set-formula", "view_id": "watch", "property": "status", "formula": "upper(status)"},
        {"action": "set-filter", "view_id": "watch", "scope": "view", "filter": {"property": "status", "operator": "eq", "value": "ready"}},
        {"action": "set-grouping", "view_id": "watch", "property": "status"},
        {"action": "set-summary", "view_id": "watch", "property": "status", "operation": "distinct"},
        {"action": "set-limit", "view_id": "watch", "limit": 25},
        {"action": "reorder-column", "view_id": "watch", "from_index": 1, "to_index": 0},
    ]
    current = source
    for command in commands:
        current = transform_base_definition(current, command)["source"]
    parsed, _ = parse_base_definition(current)
    view = parsed["views"][0]
    assert view["columns"][0]["property"] == "status"
    assert view["columns"][0]["label"] == "Status label"
    assert view["columns"][0]["visible"] is False
    assert view["columns"][0]["formula"] == "upper(status)"
    assert view["localFilters"] == {"property": "status", "operator": "eq", "value": "ready"}
    assert view["summaries"] == {"status": "distinct"} and view["limit"] == 25
    none_sort = transform_base_definition(current, {
        "action": "cycle-sort", "view_id": "watch", "property": "status",
    })["definition"]
    assert none_sort["views"][0]["sorts"] == [
        {"property": "status", "direction": "desc"},
        {"property": "file.name", "direction": "desc"},
    ]
    none_sort = transform_base_definition(dump_base_definition(none_sort), {
        "action": "cycle-sort", "view_id": "watch", "property": "status",
    })["definition"]
    assert none_sort["views"][0]["sorts"] == [{"property": "file.name", "direction": "desc"}]
    # ``type`` selects the command when ``view_type`` is omitted; it must not
    # accidentally be interpreted as the new view's presentation type.
    added = transform_base_definition(current, {"type": "add-view", "name": "Cards", "view_type": "card"})["definition"]
    assert added["views"][-1]["type"] == "card"
    duplicated = transform_base_definition(dump_base_definition(added), {"action": "duplicate-view", "view_id": "watch"})["definition"]
    assert len(duplicated["views"]) == 3
    renamed = transform_base_definition(dump_base_definition(duplicated), {"action": "rename-view", "view_id": "watch", "name": "Renamed"})["definition"]
    assert renamed["views"][0]["name"] == "Renamed"
    reordered = transform_base_definition(dump_base_definition(renamed), {"action": "reorder-view", "from_index": 2, "to_index": 0})["definition"]
    assert len(reordered["views"]) == 3
    removed = transform_base_definition(dump_base_definition(reordered), {"action": "remove-view", "view_id": reordered["views"][0]["id"]})["definition"]
    assert len(removed["views"]) == 2


def test_typed_relations_cap_work_and_utf8_output():
    huge = "日本語" * 10_000
    relations = [{"kind": "link", "target": huge}, {"kind": "link", "target": "kept.md"}]
    relations.extend({"kind": "link", "target": "discarded.md"} for _ in range(10_000))
    assert _typed_relations({"relations": relations}) == [
        {"kind": "link", "target": "kept.md"},
        *([{"kind": "link", "target": "discarded.md"}] * 254),
    ]
    capped = [{"kind": "link", "target": "after-cap.md"} for _ in range(257)]
    assert _typed_relations({"relations": capped}) == [{"kind": "link", "target": "after-cap.md"}] * 256
    assert _typed_relations({"relations": [{"kind": "link", "target": "ok", "targetDocumentId": huge}]}) == []
