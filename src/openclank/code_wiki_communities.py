"""Bounded Markdown pages derived from receipt-pinned Code Graph communities."""

import re

from src.openclank.code_wiki import _bounded, _check_receipt, _citation_markdown, _good, _hash, _safe

_MEMBER = re.compile(r"(?:decl|file):[0-9a-f]{64}")
_WITNESS = re.compile(r"occ:[0-9a-f]{64}")
_REGION = re.compile(r"community:[0-9a-f]{64}")
_ALGORITHMS = {None:"exact_witness_label_propagation_v1", "label_propagation":"exact_witness_label_propagation_v1",
               "modularity_refinement":"exact_witness_modularity_refinement_v1"}
GROUP_LIMIT = 4
MEMBER_LIMIT = 8
EDGE_LIMIT = 6


async def render_communities(receipt, *, query, citations=None, algorithm=None):
    _check_receipt(receipt)
    base = {"expected_receipt": receipt, "channels": []}
    operation = {**base, "operation": "graph_communities", "fields": ["source", "witnesses"],
                 "budgets": {"rows": GROUP_LIMIT, "bytes": 131072}}
    if algorithm is not None:
        operation["community_algorithm"] = algorithm
    result = await query(operation)
    if not isinstance(result, dict) or result.get("error"):
        return "### Code communities\n\nCommunity evidence is unavailable for this snapshot.\n"
    _good(result, "graph_communities", receipt)
    analysis = result.get("community_analysis")
    if not isinstance(analysis, dict) or analysis.get("algorithm") != _ALGORITHMS[algorithm]:
        raise ValueError("Community evidence unavailable")
    groups = analysis.get("groups")
    if not isinstance(groups, list) or len(groups) > GROUP_LIMIT:
        raise ValueError("Invalid bounded community result")
    intro = ["### Code communities", "", "Groups are derived from indexed, witnessed relationships.",
             f"Snapshot {_safe(receipt['source_snapshot_id'], 128)} · {len(groups)} groups shown."]
    coverage = ("cross_group_edges", "isolates", "ignored_uncertain", "missing_resolutions", "incomplete_file_coverage")
    if any(type(analysis.get(key)) is not int or analysis[key] < 0 for key in coverage):
        raise ValueError("Invalid community coverage")
    intro.append(f"{analysis['cross_group_edges']} cross-group edges, {analysis['isolates']} isolated declarations, "
                 f"{analysis['ignored_uncertain']} uncertain resolutions excluded, {analysis['missing_resolutions']} missing resolutions "
                 f"and {analysis['incomplete_file_coverage']} incomplete coverage records.")
    method = "Modularity refinement" if algorithm == "modularity_refinement" else "Label propagation"
    intro.append(f"Grouping method · {method}.")
    if analysis.get("converged") is not True:
        intro.append(f"{method} reached its iteration limit.")
    if _bounded(result, "Communities") or analysis.get("omitted_groups"):
        intro.append("Group coverage is bounded and may omit additional evidence.")
    parts = ["\n".join(intro)]
    for group in groups:
        members, edges = group.get("members"), group.get("edges")
        if (not isinstance(members, list) or not 1 <= len(members) <= 200
                or any(not isinstance(item, str) or not _MEMBER.fullmatch(item) for item in members)
                or len(set(members)) != len(members) or not isinstance(edges, list) or len(edges) > 200):
            raise ValueError("Invalid community membership")
        member_ids = set(members)
        sampled = members[:MEMBER_LIMIT]
        inspected = _good(await query({**base, "operation":"inspect", "seed_ids": sampled,
            "fields":["label","properties"], "budgets":{"rows":MEMBER_LIMIT,"bytes":65536}}), "inspect", receipt)
        nodes = {item["id"]: item for item in inspected["hits"] if item.get("id") in sampled}
        # Graph snapshot and complete membership make the section identity stable.
        region = group.get("region_id")
        if region is not None and (not isinstance(region,str) or not _REGION.fullmatch(region)):
            raise ValueError("Invalid community identity")
        identity = region.removeprefix("community:") if region is not None else _hash([receipt, sorted(member_ids)])
        labels = {key: _safe(node.get("label"),160) for key, node in nodes.items()}
        lines = [f"<!-- openclank-wiki-community:v1:{identity} -->", f"#### Community · {len(members)} members", "",
                 f"{len(edges)} witnessed relationships inside this group.", ""]
        for ident in sampled:
            node = nodes.get(ident) or {}
            path = (node.get("properties") or {}).get("rel_path")
            context = f" · {_safe(path,160)}" if path and path != node.get("label") else ""
            lines.append(f"- {labels.get(ident, ident)}{context}")
        omitted = len(members) - len(sampled)
        if omitted:
            lines.append(f"- {omitted} more members in the indexed group.")
        if len(nodes) != len(sampled):
            lines.append("- Some member labels were unavailable.")
        for edge in edges[:EDGE_LIMIT]:
            source, target, witness = edge.get("from"), edge.get("to"), edge.get("occurrence_id")
            if (source not in member_ids or target not in member_ids or edge.get("resolution_state") != "exact"
                    or not isinstance(witness,str) or not _WITNESS.fullmatch(witness)):
                raise ValueError("Invalid community relationship witness")
            lines.append(f"- {_safe(edge.get('relation_kind'),48)} · {labels.get(source, source)} → "
                         f"{labels.get(target, target)} · {witness}")
        if len(edges) > EDGE_LIMIT:
            lines.append(f"- {len(edges)-EDGE_LIMIT} additional relationship witnesses.")
        if citations is not None and nodes:
            sources = await citations(receipt, list(nodes.values()))
            cited, incomplete = _citation_markdown(sources, limit=4)
            if cited:
                lines.extend(["", "##### Passages citing a sampled member", "", *cited])
            if incomplete:
                lines.extend(["", "Citing-passage coverage was partial or unavailable."])
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"
