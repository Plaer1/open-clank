//! Deterministic SCC and degree calculation over already-admitted import edges.
//! It does not read files, resolve names, or imply absent dependencies.
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportCycle {
    pub nodes: Vec<String>,
    pub occurrences: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportHub {
    pub node: String,
    pub incoming: usize,
    pub outgoing: usize,
}

/// Duplicate imports share one directed neighbor for degree, retaining every
/// occurrence as evidence. Work accounting and cancellation belong to caller.
pub fn analyze<E>(
    nodes: &BTreeSet<String>,
    edges: &BTreeMap<(String, String), BTreeSet<String>>,
    mut charge: impl FnMut(usize) -> Result<(), E>,
) -> Result<(Vec<ImportCycle>, Vec<ImportHub>), E> {
    let mut forward: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut reverse: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for ((from, to), witnesses) in edges {
        charge(1 + witnesses.len())?;
        forward.entry(from).or_default().push(to);
        reverse.entry(to).or_default().push(from);
    }
    let mut visited = BTreeSet::new();
    let mut order = Vec::new();
    for node in nodes {
        if visited.contains(node.as_str()) { continue; }
        let mut stack = vec![(node.as_str(), false)];
        while let Some((current, finishing)) = stack.pop() {
            charge(1)?;
            if finishing { order.push(current); continue; }
            if !visited.insert(current) { continue; }
            stack.push((current, true));
            if let Some(next) = forward.get(current) {
                for target in next.iter().rev() { charge(1)?; if !visited.contains(target) { stack.push((target, false)); } }
            }
        }
    }
    let mut assigned = BTreeSet::new();
    let mut groups = Vec::<Vec<String>>::new();
    let mut membership = BTreeMap::new();
    for node in order.into_iter().rev() {
        if assigned.contains(node) { continue; }
        let mut group = Vec::new();
        let mut stack = vec![node];
        while let Some(current) = stack.pop() {
            charge(1)?;
            if !assigned.insert(current) { continue; }
            membership.insert(current, groups.len());
            group.push(current.to_string());
            if let Some(next) = reverse.get(current) {
                for target in next { charge(1)?; if !assigned.contains(target) { stack.push(target); } }
            }
        }
        group.sort();
        groups.push(group);
    }
    let mut witnesses = vec![BTreeSet::<String>::new(); groups.len()];
    let mut self_loop = vec![false; groups.len()];
    for ((from, to), occurrences) in edges {
        charge(1 + occurrences.len())?;
        if let (Some(a), Some(b)) = (membership.get(from.as_str()), membership.get(to.as_str())) {
            if a == b {
                if from == to { self_loop[*a] = true; }
                witnesses[*a].extend(occurrences.iter().cloned());
            }
        }
    }
    let mut cycles = Vec::new();
    for (i, group) in groups.into_iter().enumerate() {
        charge(1)?;
        if group.len() > 1 || self_loop[i] {
            cycles.push(ImportCycle { nodes: group, occurrences: witnesses[i].iter().cloned().collect() });
        }
    }
    cycles.sort_by(|a, b| a.nodes.cmp(&b.nodes));
    let mut hubs = Vec::new();
    for node in nodes {
        charge(1)?;
        let incoming = reverse.get(node.as_str()).map_or(0, Vec::len);
        let outgoing = forward.get(node.as_str()).map_or(0, Vec::len);
        if incoming + outgoing > 0 { hubs.push(ImportHub { node: node.clone(), incoming, outgoing }); }
    }
    hubs.sort_by(|a, b| (b.incoming + b.outgoing).cmp(&(a.incoming + a.outgoing)).then(a.node.cmp(&b.node)));
    Ok((cycles, hubs))
}
/// Bridges in the undirected simple projection. Parallel occurrence
/// witnesses and reciprocal relations share one adjacency, and loops cannot
/// disconnect anything. Use an explicit DFS stack for large admitted graphs.
pub fn bridges<'a, E>(
    edges: impl IntoIterator<Item = (&'a str, &'a str)>,
    mut charge: impl FnMut(usize) -> Result<(), E>,
) -> Result<Vec<(&'a str, &'a str)>, E> {
    let mut neighbors = BTreeMap::<&str, BTreeSet<&str>>::new();
    for (a, b) in edges {
        charge(1)?;
        if a != b {
            neighbors.entry(a).or_default().insert(b);
            neighbors.entry(b).or_default().insert(a);
        }
    }
    let mut times = BTreeMap::<&str, (usize, usize)>::new();
    let mut bridges = Vec::new();
    for &root in neighbors.keys() {
        if times.contains_key(root) {
            continue;
        }
        let time = times.len();
        times.insert(root, (time, time));
        let mut stack = vec![(root, None, neighbors[root].iter())];
        while let Some((node, parent, successors)) = stack.last_mut() {
            charge(1)?;
            if let Some(&neighbor) = successors.next() {
                if Some(neighbor) == *parent {
                    continue;
                }
                if let Some(&(arrival, _)) = times.get(neighbor) {
                    let current = times.get_mut(*node).unwrap();
                    current.1 = current.1.min(arrival);
                } else {
                    let time = times.len();
                    times.insert(neighbor, (time, time));
                    let parent = *node;
                    stack.push((neighbor, Some(parent), neighbors[neighbor].iter()));
                }
            } else {
                let (node, parent, _) = stack.pop().unwrap();
                if let Some(parent) = parent {
                    let low = times[node].1;
                    let (parent_arrival, parent_low) = times.get_mut(parent).unwrap();
                    if low > *parent_arrival {
                        bridges.push((node.min(parent), node.max(parent)));
                    }
                    *parent_low = (*parent_low).min(low);
                }
            }
        }
    }
    bridges.sort_unstable();
    Ok(bridges)
}
