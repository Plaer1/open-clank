//! Bounded Go syntax witnesses. Package loading and implicit method-set
//! satisfaction require qualified compiler evidence.
use super::{
    children, profile_declaration, text, Builder, EvidenceDiagnostic, EvidenceError, OccurrenceKind,
};
use std::collections::HashMap;
use tree_sitter::Node;

fn append(
    builder: &mut Builder<'_>,
    node: Node<'_>,
    name_node: Node<'_>,
    kind: &str,
    owner: Option<usize>,
    qualifier: &str,
) -> Result<usize, EvidenceError> {
    let exported = text(name_node, builder.source).chars().next().is_some_and(char::is_uppercase);
    profile_declaration(builder, node, name_node, kind, owner, qualifier, exported, "go")
}

fn receiver_name(receiver: Node<'_>) -> Option<Node<'_>> {
    let mut types = children(receiver);
    let typ = types.pop()?.child_by_field_name("type")?;
    match typ.kind() {
        "type_identifier" => Some(typ),
        "pointer_type" => children(typ)
            .into_iter()
            .find(|node| node.kind() == "type_identifier"),
        _ => None,
    }
}

// Exact literal shapes are purposely isolated from the ordinary value
// resolver, which doesn't know Go package identity or method sets.
fn pointer_assertion(value: &str) -> Option<String> {
    if value.len() > 160 || !value.is_ascii() {
        return None;
    }
    // Strip whitespace only around punctuation. Do not join two identifiers
    // into a spelling that never appeared in the assertion.
    let mut rest = value.trim();
    for token in ["(", "*"] {
        rest = rest.strip_prefix(token)?.trim_start();
    }
    let end = rest.find(|ch: char| !ch.is_ascii_alphanumeric() && ch != '_')?;
    let candidate = &rest[..end];
    if !candidate.as_bytes().first()?.is_ascii_alphabetic() && !candidate.starts_with('_') {
        return None;
    }
    rest = &rest[end..];
    for token in [")", "(", "nil", ")"] {
        rest = rest.trim_start().strip_prefix(token)?;
    }
    rest.trim().is_empty().then(|| candidate.to_owned())
}

pub(super) fn extract(builder: &mut Builder<'_>, root: Node<'_>) -> Result<(), EvidenceError> {
    let top = children(root);
    let Some(package) = top
        .iter()
        .find(|node| node.kind() == "package_clause")
        .and_then(|node| node.named_child(0))
        .map(|node| text(node, builder.source).to_owned())
    else {
        return Ok(());
    };
    let mut definitions: HashMap<String, Vec<usize>> = HashMap::new();
    for node in &top {
        builder.tick(1)?;
        if node.kind() != "type_declaration" {
            continue;
        }
        for spec in children(*node) {
            builder.tick(2)?;
            let Some(name) = spec.child_by_field_name("name") else {
                continue;
            };
            let kind = match spec.child_by_field_name("type").map(|typ| typ.kind()) {
                Some("interface_type") if spec.kind() == "type_spec" => "interface",
                Some("struct_type") if spec.kind() == "type_spec" => "struct",
                Some(_) => "type_alias",
                None => continue,
            };
            let index = append(builder, spec, name, kind, None, &package)?;
            if spec.kind() == "type_spec" && spec.child_by_field_name("type_parameters").is_none() {
                definitions
                    .entry(text(name, builder.source).to_owned())
                    .or_default()
                    .push(index);
            }
        }
    }

    let unique = |name: &str| {
        definitions
            .get(name)
            .and_then(|matches| match matches.as_slice() {
                [index] => Some(*index),
                _ => None,
            })
    };
    for node in top {
        builder.tick(1)?;
        match node.kind() {
            "function_declaration" => {
                if let Some(name) = node.child_by_field_name("name") {
                    append(builder, node, name, "function", None, &package)?;
                }
            }
            "method_declaration" => {
                let Some(name) = node.child_by_field_name("name") else {
                    continue;
                };
                let Some(receiver) = node.child_by_field_name("receiver").and_then(receiver_name)
                else {
                    continue;
                };
                let receiver = text(receiver, builder.source);
                append(
                    builder,
                    node,
                    name,
                    "method",
                    unique(receiver),
                    &format!("{package}::{receiver}"),
                )?;
            }
            "var_declaration" => {
                let mut specs = children(node);
                while let Some(spec) = specs.pop() {
                    builder.tick(2)?;
                    if spec.kind() == "var_spec_list" {
                        specs.extend(children(spec).into_iter().rev());
                    } else if spec.kind() == "var_spec" {
                        let Some(name) = spec.child_by_field_name("name") else {
                            continue;
                        };
                        if text(name, builder.source) != "_" {
                            continue;
                        }
                        let mut cursor = spec.walk();
                        if spec.children_by_field_name("name", &mut cursor).count() != 1 {
                            continue;
                        }
                        let Some(target) = spec.child_by_field_name("type") else {
                            continue;
                        };
                        let Some(value) = spec.child_by_field_name("value") else {
                            continue;
                        };
                        let Some(candidate) = pointer_assertion(text(value, builder.source)) else {
                            continue;
                        };
                        let owner = unique(&candidate)
                            .filter(|index| builder.file.declarations[*index].kind != "interface");
                        if target.kind() != "type_identifier" || owner.is_none() {
                            let diagnostic = EvidenceDiagnostic {
                                code: "unsupported_go_interface_assertion".into(),
                                message: "Go conformance requires an unqualified same-file concrete declaration and interface".into(),
                                source: Some(builder.locator(spec)),
                            };
                            builder.account(&diagnostic)?;
                            builder.file.diagnostics.push(diagnostic);
                            continue;
                        }
                        let id = builder.file.declarations[owner.unwrap()].id.clone();
                        builder.occurrence(
                            target,
                            OccurrenceKind::TypeReference,
                            text(target, builder.source).into(),
                            builder.file.root_scope_id.clone(),
                            Some(id),
                        )?;
                        let context = builder.locator(spec);
                        let occurrence = builder.file.occurrences.last_mut().unwrap();
                        occurrence.context_source = Some(context);
                        occurrence.syntax_kind = "go_interface_assertion".into();
                    }
                }
            }
            _ => (),
        }
    }
    Ok(())
}
