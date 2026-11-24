//! Same-file EventTarget dispatch and named registration witnesses.
use super::{children, text, Builder, EvidenceDiagnostic, EvidenceError, OccurrenceKind};
use std::collections::{BTreeMap, HashMap, HashSet};
use tree_sitter::Node;

const MAX_REGISTRATIONS: usize = 512;
const MAX_HANDLERS: usize = 16;

struct NamedCall<'tree> {
    receiver: Node<'tree>,
    method: Node<'tree>,
    arguments: Node<'tree>,
}

fn named_call(node: Node<'_>) -> Option<NamedCall<'_>> {
    if node.kind() != "call_expression" || node.child_by_field_name("optional_chain").is_some() {
        return None;
    }
    let target = node.child_by_field_name("function")?;
    if target.kind() != "member_expression"
        || target.child_by_field_name("optional_chain").is_some()
    {
        return None;
    }
    let receiver = target.child_by_field_name("object")?;
    let method = target.child_by_field_name("property")?;
    let arguments = node.child_by_field_name("arguments")?;
    (receiver.kind() == "identifier"
        && method.kind() == "property_identifier"
        && arguments.kind() == "arguments")
        .then_some(NamedCall {
            receiver,
            method,
            arguments,
        })
}

fn arguments(node: Node<'_>) -> impl Iterator<Item = Node<'_>> {
    children(node).into_iter().filter(|n| n.kind() != "comment")
}

fn constructor<'tree>(node: Node<'tree>, name: &str, source: &str) -> Option<Node<'tree>> {
    if node.kind() != "new_expression" {
        return None;
    }
    let class = node.child_by_field_name("constructor")?;
    let args = node.child_by_field_name("arguments")?;
    (class.kind() == "identifier" && text(class, source) == name).then_some(args)
}

fn literal<'s>(node: Node<'_>, source: &'s str) -> Option<&'s str> {
    let raw = text(node, source);
    let bytes = raw.as_bytes();
    if node.kind() != "string"
        || !(3..=130).contains(&bytes.len())
        || !matches!(bytes[0], b'"' | b'\'')
        || bytes[0] != bytes[bytes.len() - 1]
        || !bytes[1..bytes.len() - 1]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'-' | b'_' | b':' | b'.'))
    {
        return None;
    }
    Some(&raw[1..raw.len() - 1])
}

fn unsupported(builder: &mut Builder<'_>, node: Node<'_>) -> Result<(), EvidenceError> {
    let diagnostic = EvidenceDiagnostic {
        code: "unsupported_js_event_dispatch".into(),
        message: "JavaScript dispatch exceeds the bounded same-file EventTarget profile".into(),
        source: Some(builder.locator(node)),
    };
    builder.account(&diagnostic)?;
    builder.file.diagnostics.push(diagnostic);
    Ok(())
}

pub(super) fn extract(builder: &mut Builder<'_>, root: Node<'_>) -> Result<(), EvidenceError> {
    if !["EventTarget", "addEventListener", "dispatchEvent"]
        .iter()
        .all(|name| builder.source.contains(name))
    {
        return Ok(());
    }
    let root_scope = builder.file.root_scope_id.clone();

    // Unknown runtime mutations remain outside the lexical profile.
    let constructor_shadowed = |name: &str| {
        builder
            .file
            .bindings
            .iter()
            .any(|binding| binding.scope_id == root_scope && binding.name == name)
    };
    if constructor_shadowed("EventTarget") || constructor_shadowed("Event") {
        return Ok(());
    }
    let top = children(root);
    let mut buses = BTreeMap::new();
    for declaration in &top {
        builder.tick(1)?;
        if declaration.kind() != "lexical_declaration"
            || declaration
                .child_by_field_name("kind")
                .is_none_or(|kind| kind.kind() != "const")
        {
            continue;
        }
        for variable in children(*declaration) {
            builder.tick(2)?;
            let Some(name) = variable
                .child_by_field_name("name")
                .filter(|node| node.kind() == "identifier")
            else {
                continue;
            };
            let Some(args) = variable
                .child_by_field_name("value")
                .and_then(|node| constructor(node, "EventTarget", builder.source))
            else {
                continue;
            };
            if arguments(args).next().is_none() {
                buses.insert(text(name, builder.source).to_owned(), variable.end_byte());
            }
        }
    }
    if buses.is_empty() {
        return Ok(());
    }
    let mut registrations = BTreeMap::<(String, String), BTreeMap<String, Node<'_>>>::new();
    let mut registration_count = 0;
    for statement in top {
        builder.tick(1)?;
        if statement.kind() != "expression_statement" {
            continue;
        }
        let Some(call) = statement.named_child(0) else {
            continue;
        };
        let Some(parts) = named_call(call) else {
            continue;
        };
        let receiver = text(parts.receiver, builder.source);
        if text(parts.method, builder.source) != "addEventListener"
            || !buses
                .get(receiver)
                .is_some_and(|start| *start < call.start_byte())
        {
            continue;
        }
        let args: Vec<_> = arguments(parts.arguments).collect();
        if let [event, handler] = args.as_slice() {
            if let Some(event) = literal(*event, builder.source) {
                if handler.kind() == "identifier" {
                    let handler = text(*handler, builder.source);
                    let event_bindings = registrations
                        .entry((receiver.into(), event.into()))
                        .or_default();
                    event_bindings.entry(handler.into()).or_insert(call);
                    registration_count += 1;
                    if registration_count > MAX_REGISTRATIONS || event_bindings.len() > MAX_HANDLERS
                    {
                        unsupported(builder, call)?;
                        return Ok(());
                    }
                }
            }
        }
    }
    if registrations.is_empty() {
        return Ok(());
    }

    let mut relevant_bindings = HashMap::<String, HashSet<String>>::new();
    for binding in &builder.file.bindings {
        if binding.name == "Event" || buses.contains_key(&binding.name) {
            relevant_bindings
                .entry(binding.scope_id.clone())
                .or_default()
                .insert(binding.name.clone());
        }
    }
    let parents: HashMap<_, _> = builder
        .file
        .scopes
        .iter()
        .filter_map(|scope| {
            scope
                .parent_id
                .as_ref()
                .map(|parent| (scope.id.clone(), parent.clone()))
        })
        .collect();
    let mut root_binding_counts = HashMap::new();
    for binding in &builder.file.bindings {
        if binding.scope_id == root_scope && buses.contains_key(&binding.name) {
            *root_binding_counts
                .entry(binding.name.clone())
                .or_insert(0usize) += 1;
        }
    }

    let mut pending = vec![(root, 0)];
    while let Some((node, depth)) = pending.pop() {
        builder.tick(depth)?;
        pending.extend(
            children(node)
                .into_iter()
                .rev()
                .map(|child| (child, depth + 1)),
        );
        let Some(parts) = named_call(node) else {
            continue;
        };
        if text(parts.method, builder.source) != "dispatchEvent" {
            continue;
        }
        let receiver = text(parts.receiver, builder.source);
        if root_binding_counts.get(receiver) != Some(&1) {
            continue;
        }
        let args: Vec<_> = arguments(parts.arguments).collect();
        let [new_event] = args.as_slice() else {
            continue;
        };
        let Some(event_args) = constructor(*new_event, "Event", builder.source) else {
            continue;
        };
        let args: Vec<_> = arguments(event_args).collect();
        let [event_node] = args.as_slice() else {
            continue;
        };
        let Some(event_name) = literal(*event_node, builder.source) else {
            continue;
        };
        let Some(handlers) = registrations.get(&(receiver.to_owned(), event_name.to_owned()))
        else {
            continue;
        };
        let Some((scope, caller)) = builder.contexts.get(&node.id()).cloned() else {
            continue;
        };
        let mut current = scope.as_str();
        while current != root_scope.as_str() {
            builder.tick(depth)?;
            if relevant_bindings
                .get(current)
                .is_some_and(|names| names.contains(receiver) || names.contains("Event"))
            {
                break;
            }
            let Some(parent) = parents.get(current) else {
                break;
            };
            current = parent;
        }
        if current != root_scope.as_str()
            || scope == root_scope
                && buses
                    .get(receiver)
                    .is_none_or(|start| *start >= node.start_byte())
        {
            continue;
        }
        for (handler, registration) in handlers {
            builder.tick(depth)?;
            builder.occurrence(
                *event_node,
                OccurrenceKind::ValueReference,
                handler.clone(),
                scope.clone(),
                caller.clone(),
            )?;
            let dispatch = builder.locator(node);
            let registration = builder.locator(*registration);
            let occurrence = builder.file.occurrences.last_mut().unwrap();
            occurrence.context_source = Some(dispatch);
            occurrence.related_source = Some(registration);
            occurrence.syntax_kind = "js_event_dispatch".into();
        }
    }
    Ok(())
}
