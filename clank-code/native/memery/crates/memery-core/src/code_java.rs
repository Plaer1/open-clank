//! Qualified same-file Spring event syntax. Actual framework dispatch and
//! constructor/method overload binding remain compiler/runtime observations.
use super::{
    children, profile_declaration, text, Builder, EvidenceDiagnostic, EvidenceError, OccurrenceKind,
};
use std::collections::HashMap;
use tree_sitter::Node;

const EVENT_LISTENER: &str = "org.springframework.context.event.EventListener";
const EVENT_PUBLISHER: &str = "org.springframework.context.ApplicationEventPublisher";

fn modifiers(node: Node<'_>) -> Option<Node<'_>> {
    children(node)
        .into_iter()
        .find(|child| child.kind() == "modifiers")
}

fn exported(node: Node<'_>) -> bool {
    let Some(modifiers) = modifiers(node) else {
        return false;
    };
    let mut cursor = modifiers.walk();
    let public = modifiers
        .children(&mut cursor)
        .any(|child| child.kind() == "public");
    public
}

fn marker<'t>(node: Node<'t>, source: &str) -> Option<Node<'t>> {
    children(modifiers(node)?).into_iter().find(|child| {
        child.kind() == "marker_annotation"
            && child
                .child_by_field_name("name")
                .is_some_and(|name| text(name, source) == EVENT_LISTENER)
    })
}

fn event_kind(node: Node<'_>) -> Option<&'static str> {
    match node.kind() {
        "class_declaration" | "record_declaration" => Some("class"),
        "enum_declaration" => Some("enum"),
        "interface_declaration" => Some("interface"),
        _ => None,
    }
}

fn only_child(node: Node<'_>) -> Option<Node<'_>> {
    let mut cursor = node.walk();
    let mut values = node
        .named_children(&mut cursor)
        .filter(|child| child.kind() != "line_comment" && child.kind() != "block_comment");
    let first = values.next()?;
    values.next().is_none().then_some(first)
}

fn unsupported(builder: &mut Builder<'_>, witness: Node<'_>) -> Result<(), EvidenceError> {
    let diagnostic = EvidenceDiagnostic {
        code: "unsupported_java_event".into(),
        message: "Spring event profile requires a qualified same-file event type, explicit framework marker or publisher parameter, and a direct statement".into(),
        source: Some(builder.locator(witness)),
    };
    builder.account(&diagnostic)?;
    builder.file.diagnostics.push(diagnostic);
    Ok(())
}

fn relation(
    builder: &mut Builder<'_>,
    witness: Node<'_>,
    context: Node<'_>,
    from: usize,
    target: String,
    syntax: &str,
) -> Result<(), EvidenceError> {
    let from = builder.file.declarations[from].id.clone();
    builder.occurrence(
        witness,
        OccurrenceKind::TypeReference,
        target,
        builder.file.root_scope_id.clone(),
        Some(from),
    )?;
    let context = builder.locator(context);
    let occurrence = builder.file.occurrences.last_mut().unwrap();
    occurrence.context_source = Some(context);
    occurrence.syntax_kind = syntax.into();
    Ok(())
}

pub(super) fn extract(builder: &mut Builder<'_>, root: Node<'_>) -> Result<(), EvidenceError> {
    let top = children(root);
    let Some(package) = top
        .iter()
        .find(|node| node.kind() == "package_declaration")
        .and_then(|node| {
            children(*node)
                .into_iter()
                .find(|child| matches!(child.kind(), "scoped_identifier" | "identifier"))
        })
        .map(|node| text(node, builder.source).to_owned())
    else {
        return Ok(());
    };

    let mut types = Vec::new();
    let mut declarations: HashMap<String, Vec<usize>> = HashMap::new();
    for node in top {
        builder.tick(1)?;
        let Some(kind) = event_kind(node) else {
            continue;
        };
        let Some(name) = node.child_by_field_name("name") else {
            continue;
        };
        let index = profile_declaration(
            builder,
            node,
            name,
            kind,
            None,
            &package,
            exported(node),
            "java",
        )?;
        if kind != "interface" && node.child_by_field_name("type_parameters").is_none() {
            let spelling = format!("{package}.{}", text(name, builder.source));
            declarations.entry(spelling).or_default().push(index);
        }
        types.push((index, node));
    }

    let unique_event = |name: &str| {
        declarations
            .get(name)
            .and_then(|matches| match matches.as_slice() {
                [index] => Some(*index),
                _ => None,
            })
    };
    for (owner, typ) in types {
        let Some(class) = typ.child_by_field_name("body") else {
            continue;
        };
        let qualifier = builder.file.declarations[owner].qualified_name.clone();
        for method in children(class)
            .into_iter()
            .filter(|child| child.kind() == "method_declaration")
        {
            builder.tick(2)?;
            let Some(name) = method.child_by_field_name("name") else {
                continue;
            };
            let index = profile_declaration(
                builder,
                method,
                name,
                "method",
                Some(owner),
                &qualifier,
                exported(method),
                "java",
            )?;
            let Some(parameters) = method.child_by_field_name("parameters") else {
                continue;
            };
            if let Some(annotation) = marker(method, builder.source) {
                let event = only_child(parameters)
                    .filter(|param| {
                        param.kind() == "formal_parameter"
                            && param.child_by_field_name("dimensions").is_none()
                    })
                    .and_then(|param| param.child_by_field_name("type"))
                    .and_then(|typ| unique_event(text(typ, builder.source)));
                if let Some(event) = event {
                    let name = builder.file.declarations[index].qualified_name.clone();
                    relation(
                        builder,
                        annotation,
                        method,
                        event,
                        name,
                        "java_event_consume",
                    )?;
                } else {
                    unsupported(builder, annotation)?;
                }
            }
            let mut publishers = Vec::new();
            for param in children(parameters) {
                builder.tick(3)?;
                let Some(typ) = param.child_by_field_name("type") else {
                    continue;
                };
                if param.kind() == "formal_parameter"
                    && param.child_by_field_name("dimensions").is_none()
                    && text(typ, builder.source) == EVENT_PUBLISHER
                {
                    if let Some(name) = param.child_by_field_name("name") {
                        publishers.push(text(name, builder.source));
                    }
                }
            }
            let Some(body) = method.child_by_field_name("body") else {
                continue;
            };
            for statement in children(body) {
                builder.tick(3)?;
                if statement.kind() != "expression_statement" {
                    continue;
                }
                let Some(call) =
                    only_child(statement).filter(|child| child.kind() == "method_invocation")
                else {
                    continue;
                };
                let Some(name) = call.child_by_field_name("name") else {
                    continue;
                };
                if text(name, builder.source) != "publishEvent" {
                    continue;
                }
                let object = call
                    .child_by_field_name("object")
                    .filter(|object| object.kind() == "identifier");
                let creation = call
                    .child_by_field_name("arguments")
                    .and_then(only_child)
                    .filter(|arg| {
                        arg.kind() == "object_creation_expression"
                            && children(*arg)
                                .into_iter()
                                .all(|node| node.kind() != "class_body")
                    });
                let target_type = creation.and_then(|arg| arg.child_by_field_name("type"));
                if object.is_some_and(|object| publishers.contains(&text(object, builder.source))) {
                    if let Some(target) = target_type {
                        if let Some(event) = unique_event(text(target, builder.source)) {
                            let name = builder.file.declarations[event].qualified_name.clone();
                            relation(builder, target, call, index, name, "java_event_publish")?;
                            continue;
                        }
                    }
                }
                unsupported(builder, call)?;
            }
        }
    }
    Ok(())
}
