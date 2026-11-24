//! A bounded lexical control-flow profile for Python function bodies.
use super::{span, Builder, DeclarationEvidence, SourceSpan};
use serde::{Deserialize, Serialize};
use tree_sitter::Node;

const PROFILE: &str = "python_lexical_flow_v1";
const MAX_BLOCKS: usize = 24;
const MAX_NESTING: usize = 12;
const MAX_PROFILE_BYTES_PER_FILE: usize = 96 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum BlockKind {
    Statement,
    Condition,
    Return,
    Raise,
    Exit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum EdgeKind {
    Next,
    True,
    False,
    Return,
    Raise,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FlowBlock {
    id: usize,
    kind: BlockKind,
    span: SourceSpan,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FlowEdge {
    from: usize,
    to: usize,
    kind: EdgeKind,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FunctionFlow {
    version: u32,
    profile: String,
    status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    entry: Option<usize>,
    blocks: Vec<FlowBlock>,
    edges: Vec<FlowEdge>,
}

impl FunctionFlow {
    fn empty(status: &str, reason: Option<&str>) -> Self {
        Self {
            version: 1,
            profile: PROFILE.into(),
            status: status.into(),
            reason: reason.map(str::to_owned),
            entry: None,
            blocks: vec![],
            edges: vec![],
        }
    }

    pub(super) fn ambiguous() -> Self {
        Self::empty("unsupported", Some("ambiguous_binding"))
    }

    pub(crate) fn unavailable(reason: &str) -> Self {
        Self::empty("unavailable", Some(reason))
    }

    pub(crate) fn records(&self) -> usize {
        self.blocks.len() + self.edges.len() + 1
    }
}

struct Graph<'a, 's> {
    builder: &'a mut Builder<'s>,
    flow: FunctionFlow,
}

impl Graph<'_, '_> {
    fn add_block(&mut self, kind: BlockKind, span: SourceSpan) -> Result<usize, &'static str> {
        let id = self.flow.blocks.len();
        if id >= MAX_BLOCKS {
            return Err("profile_limit");
        }
        self.flow.blocks.push(FlowBlock { id, kind, span });
        Ok(id)
    }

    fn edge(&mut self, from: usize, to: usize, kind: EdgeKind) {
        self.flow.edges.push(FlowEdge { from, to, kind });
    }

    fn tick(&mut self, depth: usize) -> Result<(), &'static str> {
        self.builder.tick(depth).map_err(|_| "profile_limit")
    }

    // Expression evaluation, exceptions from calls and short-circuit branches
    // are outside this profile. Suppress even the statement graph for generators.
    fn expression(&mut self, node: Node<'_>, depth: usize) -> Result<(), &'static str> {
        self.tick(depth)?;
        if matches!(node.kind(), "yield" | "await") {
            return Err("unsupported_syntax");
        }
        if node.kind() != "lambda" {
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                self.expression(child, depth + 1)?;
            }
        }
        Ok(())
    }

    fn field<'tree>(node: Node<'tree>, name: &str) -> Result<Node<'tree>, &'static str> {
        node.child_by_field_name(name).ok_or("unsupported_syntax")
    }

    fn condition(&mut self, node: Node<'_>, depth: usize) -> Result<usize, &'static str> {
        let condition = Self::field(node, "condition")?;
        self.expression(condition, depth)?;
        self.add_block(BlockKind::Condition, span(condition))
    }

    fn suite(&mut self, suite: Node<'_>, next: usize, depth: usize) -> Result<usize, &'static str> {
        if depth > MAX_NESTING || suite.kind() != "block" {
            return Err(if depth > MAX_NESTING {
                "profile_limit"
            } else {
                "unsupported_syntax"
            });
        }
        let mut entry = next;
        for index in (0..suite.named_child_count()).rev() {
            let statement = suite
                .named_child(index as u32)
                .ok_or("unsupported_syntax")?;
            self.tick(depth)?;
            entry = self.statement(statement, entry, depth)?;
        }
        Ok(entry)
    }

    fn statement(
        &mut self,
        node: Node<'_>,
        next: usize,
        depth: usize,
    ) -> Result<usize, &'static str> {
        match node.kind() {
            "comment" => Ok(next),
            "if_statement" => {
                let condition = self.condition(node, depth)?;
                let yes = self.suite(Self::field(node, "consequence")?, next, depth + 1)?;
                let mut no = next;
                // Python records each elif and the optional else in source order.
                for index in (0..node.named_child_count()).rev() {
                    let clause = node.named_child(index as u32).ok_or("unsupported_syntax")?;
                    match clause.kind() {
                        "else_clause" => {
                            no = self.suite(Self::field(clause, "body")?, next, depth + 1)?;
                        }
                        "elif_clause" => {
                            let branch = self.condition(clause, depth)?;
                            let branch_yes =
                                self.suite(Self::field(clause, "consequence")?, next, depth + 1)?;
                            self.edge(branch, branch_yes, EdgeKind::True);
                            self.edge(branch, no, EdgeKind::False);
                            no = branch;
                        }
                        _ => {}
                    }
                }
                self.edge(condition, yes, EdgeKind::True);
                self.edge(condition, no, EdgeKind::False);
                Ok(condition)
            }
            "while_statement" => {
                let condition = self.condition(node, depth)?;
                let yes = self.suite(Self::field(node, "body")?, condition, depth + 1)?;
                let no = match node.child_by_field_name("alternative") {
                    Some(clause) => self.suite(Self::field(clause, "body")?, next, depth + 1)?,
                    None => next,
                };
                self.edge(condition, yes, EdgeKind::True);
                self.edge(condition, no, EdgeKind::False);
                Ok(condition)
            }
            "return_statement" | "raise_statement" => {
                let is_return = node.kind() == "return_statement";
                self.expression(node, depth)?;
                let block_kind = if is_return {
                    BlockKind::Return
                } else {
                    BlockKind::Raise
                };
                let edge_kind = if is_return {
                    EdgeKind::Return
                } else {
                    EdgeKind::Raise
                };
                let id = self.add_block(block_kind, span(node))?;
                self.edge(id, 0, edge_kind);
                Ok(id)
            }
            "expression_statement"
            | "pass_statement"
            | "delete_statement"
            | "global_statement"
            | "nonlocal_statement"
            | "import_statement"
            | "import_from_statement"
            | "future_import_statement"
            | "type_alias_statement" => {
                self.expression(node, depth)?;
                let id = self.add_block(BlockKind::Statement, span(node))?;
                self.edge(id, next, EdgeKind::Next);
                Ok(id)
            }
            _ => Err("unsupported_syntax"),
        }
    }

    fn finish(mut self, entry: usize) -> FunctionFlow {
        self.flow
            .blocks
            .sort_by_key(|block| (block.span.start_byte, block.span.end_byte));
        let mut remap = vec![0; self.flow.blocks.len()];
        for (id, block) in self.flow.blocks.iter_mut().enumerate() {
            remap[block.id] = id;
            block.id = id;
        }
        self.flow.entry = Some(remap[entry]);
        for edge in &mut self.flow.edges {
            edge.from = remap[edge.from];
            edge.to = remap[edge.to];
        }
        self.flow
            .edges
            .sort_by_key(|edge| (edge.from, edge.kind, edge.to));
        self.flow
    }
}

fn build(builder: &mut Builder<'_>, function: Node<'_>) -> Result<FunctionFlow, &'static str> {
    if function
        .child(0)
        .is_some_and(|token| token.kind() == "async")
    {
        return Err("unsupported_syntax");
    }
    let body = Graph::field(function, "body")?;
    let mut exit = span(function);
    exit.start_byte = exit.end_byte;
    exit.start_line = exit.end_line;
    exit.start_column = exit.end_column;
    let mut graph = Graph {
        builder,
        flow: FunctionFlow::empty("complete", None),
    };
    let end = graph.add_block(BlockKind::Exit, exit)?;
    let entry = graph.suite(body, end, 0)?;
    Ok(graph.finish(entry))
}

pub(super) fn extract(builder: &mut Builder<'_>, tree: Node<'_>) {
    let mut node = tree;
    let mut cursor = tree.walk();
    let baseline = builder.output_bytes;
    loop {
        if builder.tick(0).is_err() {
            break;
        }
        if node.kind() == "function_definition" {
            if let Some(index) = builder.declaration_nodes.get(&node.id()).copied() {
                let profile = build(builder, node)
                    .unwrap_or_else(|reason| FunctionFlow::empty("unsupported", Some(reason)));
                let previous = builder.output_bytes;
                if builder.account(&profile).is_err()
                    || builder.output_bytes - baseline > MAX_PROFILE_BYTES_PER_FILE
                {
                    builder.output_bytes = previous;
                    break;
                }
                builder.file.declarations[index].function_flow = Some(profile);
            }
        }
        if cursor.goto_first_child() {
            node = cursor.node();
            continue;
        }
        while !cursor.goto_next_sibling() {
            if !cursor.goto_parent() {
                return;
            }
        }
        node = cursor.node();
    }
}

pub(super) fn is_valid(declaration: &DeclarationEvidence, flow: &FunctionFlow) -> bool {
    if flow.version != 1
        || flow.profile != PROFILE
        || !matches!(declaration.kind.as_str(), "function" | "method" | "route")
    {
        return false;
    }
    if flow.status == "unsupported" {
        return flow.entry.is_none()
            && flow.blocks.is_empty()
            && flow.edges.is_empty()
            && matches!(
                flow.reason.as_deref(),
                Some("unsupported_syntax" | "profile_limit" | "ambiguous_binding")
            );
    }
    if flow.status != "complete"
        || flow.reason.is_some()
        || declaration.identity_state != super::IdentityState::Stable
        || flow.blocks.len() > MAX_BLOCKS
        || !flow.entry.is_some_and(|entry| entry < flow.blocks.len())
        || flow.edges.len() > MAX_BLOCKS * 2
    {
        return false;
    }
    let outer = &declaration.source.span;
    let mut previous_end = outer.start_byte;
    let mut exits = 0;
    for (id, block) in flow.blocks.iter().enumerate() {
        let witness = &block.span;
        if block.id != id
            || witness.start_byte < previous_end
            || witness.end_byte > outer.end_byte
            || witness.start_byte > witness.end_byte
            || witness.start_line < outer.start_line
            || witness.end_line > outer.end_line
            || (witness.start_line, witness.start_column) > (witness.end_line, witness.end_column)
            || (witness.start_byte == witness.end_byte) != (block.kind == BlockKind::Exit)
        {
            return false;
        }
        previous_end = witness.end_byte;
        if block.kind == BlockKind::Exit {
            exits += 1;
            if id + 1 != flow.blocks.len() || witness.end_byte != outer.end_byte {
                return false;
            }
        }
    }
    if exits != 1 {
        return false;
    }
    let mut edges = flow.edges.iter();
    let exit = flow.blocks.len() - 1;
    for block in &flow.blocks {
        let expected: &[EdgeKind] = match block.kind {
            BlockKind::Statement => &[EdgeKind::Next],
            BlockKind::Condition => &[EdgeKind::True, EdgeKind::False],
            BlockKind::Return => &[EdgeKind::Return],
            BlockKind::Raise => &[EdgeKind::Raise],
            BlockKind::Exit => &[],
        };
        for kind in expected {
            let Some(edge) = edges.next() else {
                return false;
            };
            if edge.from != block.id
                || edge.kind != *kind
                || edge.to >= flow.blocks.len()
                || (matches!(edge.kind, EdgeKind::Return | EdgeKind::Raise) && edge.to != exit)
            {
                return false;
            }
        }
    }
    edges.next().is_none()
}
