import sqlite3


class _McpManager:
    _generation = 1

    @staticmethod
    def get_tool_descriptions_for_prompt(_disabled):
        return "**External:**\n  - mcp__ext__lookup: Look up an external record"


def test_canonical_tool_index_round_trip_and_prunes_stale_builtins(tmp_path):
    from src.tool_index import FrankenmemoryToolIndex

    index = FrankenmemoryToolIndex(str(tmp_path / "frankenmemory.db"))
    index.index_builtin_tools()
    assert index.healthy
    assert "manage_memory" in index.retrieve("save a memory", k=10)
    with sqlite3.connect(index.db_path) as conn:
        conn.execute(
            "INSERT INTO fm_v2_tool_catalog(owner_id,tool_name,tool_type,description,updated_at) VALUES (?,?,?,?,?)",
            (index._owner, "stale_tool", "builtin", "stale", "now"),
        )
    index.index_builtin_tools()
    with sqlite3.connect(index.db_path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM fm_v2_tool_catalog WHERE owner_id=? AND tool_name=?",
            (index._owner, "stale_tool"),
        ).fetchone()


def test_tool_index_singleton_defaults_to_frankenmemory(monkeypatch, tmp_path):
    import src.tool_index as module

    monkeypatch.setenv("TOOL_INDEX_BACKEND", "chroma")
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(tmp_path / "fm.db"))
    module.reset_tool_index()
    index = module.get_tool_index()
    assert index is not None
    assert module.ToolIndex is module.FrankenmemoryToolIndex
    assert index.backend == "frankenmemory"
    assert "web_search" in index.get_tools_for_query("look up the latest news")
    module.reset_tool_index()


def test_mcp_tool_identity_matches_agent_loop_schema_filter(tmp_path):
    from src.tool_index import FrankenmemoryToolIndex

    index = FrankenmemoryToolIndex(str(tmp_path / "frankenmemory.db"))
    index.index_mcp_tools(_McpManager())

    selected = set(index.retrieve("external record lookup", k=8))
    schemas = [
        {"type": "function", "function": {"name": "mcp__ext__lookup"}},
        {"type": "function", "function": {"name": "mcp__other__ignore"}},
    ]
    filtered = [
        schema
        for schema in schemas
        if schema.get("function", {}).get("name") in selected
    ]

    assert selected == {"mcp__ext__lookup"}
    assert [schema["function"]["name"] for schema in filtered] == ["mcp__ext__lookup"]
