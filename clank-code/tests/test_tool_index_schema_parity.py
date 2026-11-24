"""Every FUNCTION_TOOL_SCHEMAS tool must have a ToolIndex description.

Agent mode selects tools by embedding BUILTIN_TOOL_DESCRIPTIONS and
retrieving the top-K per message. A tool that exists in tool_schemas but has
no description entry can never be retrieved, so the agent advertises the
capability (e.g. API integrations in the system prompt) while the schema is
never actually sent to the model. api_call was missing exactly this way.

The schema module is leaf-safe and includes generated/appended schemas.
Only the embedding-backed ToolIndex is parsed with ast instead of imported.
"""
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _assigned_value(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
    raise AssertionError(f"{name} assignment not found")


def _schema_tool_names():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    return {item["function"]["name"] for item in FUNCTION_TOOL_SCHEMAS}


def _indexed_tool_names():
    src = open(os.path.join(ROOT, "src", "tool_index.py"), encoding="utf-8").read()
    value = _assigned_value(ast.parse(src), "BUILTIN_TOOL_DESCRIPTIONS")
    return {ast.literal_eval(key) for key in value.keys}


def test_every_schema_tool_has_an_index_description():
    missing = _schema_tool_names() - _indexed_tool_names()
    assert not missing, (
        "Tools defined in FUNCTION_TOOL_SCHEMAS but absent from "
        f"BUILTIN_TOOL_DESCRIPTIONS (RAG can never select them): {sorted(missing)}"
    )


def test_generated_and_appended_schemas_are_included_without_embedding_imports():
    from src.openclank.code_query import search_code_schema
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert {"search_code", "read_copal", "manage_copal"} <= _schema_tool_names()
    assert [item for item in FUNCTION_TOOL_SCHEMAS
            if item["function"]["name"] == "search_code"] == [search_code_schema()]
    # A fresh interpreter prevents prior pytest imports from concealing a
    # newly introduced dependency in this intentionally lightweight seam.
    result = subprocess.run([sys.executable, "-B", "-c", """
import sys
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
assert FUNCTION_TOOL_SCHEMAS
heavy = ('src.tool_index', 'src.embedding_lanes', 'chromadb', 'fastembed', 'onnxruntime')
assert not [name for name in sys.modules
            if any(name == prefix or name.startswith(prefix + '.') for prefix in heavy)]
"""], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_api_call_is_indexed_with_a_real_description():
    src = open(os.path.join(ROOT, "src", "tool_index.py"), encoding="utf-8").read()
    value = _assigned_value(ast.parse(src), "BUILTIN_TOOL_DESCRIPTIONS")
    descriptions = {
        ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(value.keys, value.values)
    }
    assert "api_call" in descriptions
    assert len(descriptions["api_call"]) > 50
