"""One host-owned formatter for private recall output and its evidence."""
import copy
import json


def render_search_identity(result, handler_label):
    """Modify a result as the existing lifetools presentation does."""
    from services.memory.principal_context import render_identity_template
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return ()
    unchanged = []
    for hit in result["results"]:
        record = hit.get("record") if isinstance(hit, dict) else None
        content = record.get("content") if isinstance(record, dict) else None
        if not isinstance(content, str):
            unchanged.append(False)
            continue
        rendered = render_identity_template(content, handler_label=handler_label) if "%" in content else content
        if rendered != content:
            record["raw_content"] = content
            record["content"] = rendered
        unchanged.append(rendered == content)
    return tuple(unchanged)


def format_qualified_search(result, handler_label, *, render_identity=True):
    """Return the exact model-facing JSON and indexes unchanged by rendering."""
    output = copy.deepcopy(result)
    if not isinstance(output, dict) or not isinstance(output.get("results"), list):
        raise ValueError("qualified recall result unavailable")
    unchanged = render_search_identity(output, handler_label) if render_identity else tuple(
        isinstance(hit, dict) and isinstance(hit.get("record"), dict)
        and isinstance(hit["record"].get("content"), str) for hit in output["results"])
    return json.dumps(output), unchanged
