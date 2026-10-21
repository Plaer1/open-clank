//! App-only compact map contract. This is NOT an agent CodeQuery or evidence
//! authority. Native producer/transport are deliberately absent at contract freeze.
use serde::{Deserialize, Serialize};
use crate::code_query::QueryReceipt;

pub const SCHEMA: &str = "open-clank.code-map/v1";
pub const MAX_BYTES: usize = 256 * 1024;
pub const MAX_NODES: usize = 200;
pub const MAX_EDGES: usize = 800;
pub const DEFAULT_GROUPS: usize = 50;
pub const MAX_WORK: usize = 4_000_000;
pub const MAX_ID_BYTES: usize = 256;
pub const MAX_CURSOR_BYTES: usize = 4096;
pub const MAX_LABEL_BYTES: usize = 160;
pub const MAX_REQUEST_BYTES: usize = 16 * 1024;
pub const SUMMARY_ENTRIES: usize = 4;
pub const SUMMARY_BYTES: usize = 16 * 1024 * 1024;
pub const SUMMARY_ENTRY_BYTES: usize = 8 * 1024 * 1024;
pub const SUMMARY_NODES: usize = 32_768;
pub const SUMMARY_RELATIONS: usize = 65_536;
pub const PREVIEW_ENTRIES: usize = 100_000;
pub const PREVIEW_DIRECTORIES: usize = 10_000;
pub const PREVIEW_DEPTH: usize = 64;
pub const MAX_SELECTION_REFS: usize = 50;
pub const MAX_SELECTION_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum MapOperation { Overview, Children, Neighbors }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum MapDirection { Incoming, Outgoing, Both }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum RelationKind { Containment, Import, Call, ValueReference, TypeReference, Inherits, Implements, Render, Route, Overrides }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum RelationState { Structure, Exact, Inferred }
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum NodeKind { Repository, Folder, File, Symbol }

/// Trusted owner/subject/root stay in the request binding, never these DTOs.
/// Variant-specific fields make it impossible to invent a preview evidence head.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag="variant",rename_all="snake_case",deny_unknown_fields)]
pub enum MapAuthority {
    StructurePreview {
        workspace_id: String, registration_id: String, checkout_id: String,
        admission_epoch: i64, policy_generation: i64, workspace_revision: i64,
        selection_policy_digest: String, inventory_id: String,
    },
    Published {
        receipt: QueryReceipt, selection_policy_digest: String, summary_id: String,
    },
}

/// Geometry IDs may only be resolved in this authority's guarded summary.
/// Canonical resource selection permits inspecting a real search result that
/// was absent from the first map; its receipt must equal Published authority.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag="kind",rename_all="snake_case",deny_unknown_fields)]
pub enum MapRoot {
    Visual { id: String },
    Resource { resource_id: String, receipt: QueryReceipt },
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default,deny_unknown_fields)]
pub struct MapLimits { pub bytes: usize, pub nodes: usize, pub edges: usize, pub groups: usize, pub work: usize }
impl Default for MapLimits { fn default()->Self {Self {bytes:MAX_BYTES,nodes:MAX_NODES,edges:MAX_EDGES,groups:DEFAULT_GROUPS,work:MAX_WORK}} }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapRequest {
    pub schema: String, pub operation: MapOperation,
    #[serde(default)] pub expected_authority: Option<MapAuthority>,
    #[serde(default)] pub root: Option<MapRoot>,
    #[serde(default="both")] pub direction: MapDirection,
    #[serde(default="all_relations")] pub relations: Vec<RelationKind>,
    #[serde(default)] pub cursor: Option<String>,
    #[serde(default)] pub limits: MapLimits,
}
fn both()->MapDirection {MapDirection::Both}
fn all_relations()->Vec<RelationKind>{vec![RelationKind::Containment,RelationKind::Import,RelationKind::Call]}
fn bounded(value:&str,max:usize)->bool {!value.is_empty()&&value.len()<=max&&!value.chars().any(char::is_control)}
pub(crate) fn canonical_resource(value:&str)->bool{value.split_once(':').is_some_and(|(kind,id)|matches!(kind,"file"|"decl"|"occ")&&!id.is_empty()&&id.len()<=128&&id.bytes().all(|b|b.is_ascii_hexdigit()))}
const JS_INTEGER:u64=9_007_199_254_740_991;
fn safe_cost(cost:&MapCost)->bool{[cost.inspected_entries,cost.sql_vm_steps,cost.metadata_bytes,cost.retained_summary_bytes,cost.serialized_bytes,cost.source_bytes_read,cost.policy_bytes_read,cost.native_rpc_calls_completed].iter().all(|n|(*n as u128)<=JS_INTEGER as u128)}
impl MapRequest {
    /// Caller must bound raw JSON to MAX_REQUEST_BYTES BEFORE deserialize.
    pub fn validate(&self)->Result<(), &'static str>{
        if self.schema!=SCHEMA{return Err("invalid_schema");}
        let l=&self.limits;
        if l.bytes<512||l.bytes>MAX_BYTES||l.nodes==0||l.nodes>MAX_NODES||l.edges==0||l.edges>MAX_EDGES||l.groups==0||l.groups>DEFAULT_GROUPS||l.work==0||l.work>MAX_WORK{return Err("invalid_limits");}
        if self.relations.is_empty()||self.relations.len()>10{return Err("invalid_relations");}
        for(i,r)in self.relations.iter().enumerate(){if self.relations[..i].contains(r){return Err("invalid_relations");}}
        if self.cursor.as_ref().is_some_and(|c|!bounded(c,MAX_CURSOR_BYTES)){return Err("invalid_cursor");}
        if (self.operation!=MapOperation::Overview||self.cursor.is_some())&&self.expected_authority.is_none(){return Err("missing_authority");}
        match(&self.operation,&self.root){
            (MapOperation::Overview,Some(_))=>return Err("unexpected_root"),
            (MapOperation::Children|MapOperation::Neighbors,None)=>return Err("missing_root"),_=>{}
        }
        if let Some(authority)=&self.expected_authority {authority.validate()?;}
        match &self.root {
            Some(MapRoot::Visual{id}) if !bounded(id,MAX_ID_BYTES)=>return Err("invalid_root"),
            Some(MapRoot::Resource{resource_id,receipt})=>{
                if self.operation!=MapOperation::Neighbors||!canonical_resource(resource_id)||!matches!(&self.expected_authority,Some(MapAuthority::Published{receipt:expected,..}) if expected==receipt){return Err("invalid_resource_authority");}
            },_=>{}
        }
        Ok(())
    }
}
impl MapAuthority {
    pub fn validate(&self)->Result<(), &'static str>{
        let strings:Vec<&str>=match self {
            Self::StructurePreview{workspace_id,registration_id,checkout_id,admission_epoch,policy_generation,workspace_revision,selection_policy_digest,inventory_id}=>{
                if *admission_epoch<0||*policy_generation<0||*workspace_revision<0{return Err("invalid_authority");}
                vec![workspace_id,registration_id,checkout_id,selection_policy_digest,inventory_id]
            },
            Self::Published{receipt,selection_policy_digest,summary_id}=>{
                if receipt.evidence.admission_epoch<0||receipt.evidence.policy_generation<0{return Err("invalid_authority");}
                vec![&receipt.workspace_id,&receipt.registration_id,&receipt.source_snapshot_id,&receipt.evidence.checkout_id,&receipt.evidence.head_generation,&receipt.evidence.config_fingerprint,selection_policy_digest,summary_id]
            }
        };
        if strings.iter().any(|s|!bounded(s,MAX_ID_BYTES)){return Err("invalid_authority");}Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LanguageCount { pub language: String, pub count: u64 }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapNode {
    pub id: String, pub label: String, pub kind: NodeKind, pub parent: Option<String>,
    pub source_files: u64, pub asset_files: u64, pub languages: Vec<LanguageCount>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub resource_id:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub symbol_kind:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub qualified_name:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub file_resource_id:Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapEdge {
    pub id: String, pub from: String, pub to: String, pub relation: RelationKind,
    pub state: RelationState, pub count: u64,
    /// Opaque bounded lookup for witnessed dependencies, not an evidence ID.
    /// Guarded selection resolves it to original occurrence resources on demand.
    pub selection_id: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum DependencyCoverage { Indexing, Complete, Partial }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapCoverage {
    pub inventory_complete: bool, pub observed_entries: u64, pub total_entries: Option<u64>,
    /// Preview is None, not zero indexed; successful indexed includes zero units.
    pub indexed_files: Option<u64>, pub parse_failed_files: Option<u64>,
    pub ignored_entries: u64, pub unsupported_entries: u64, pub unavailable_entries: u64,
    pub dependencies: DependencyCoverage,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapPage {
    pub returned_nodes: usize, pub returned_edges: usize,
    pub remaining_nodes: Option<u64>, pub remaining_edges: Option<u64>,
    /// True only for this exact root/direction/relation/authority sequence.
    /// Does not certify adjacency of another fetched endpoint.
    pub complete: bool, pub next_cursor: Option<String>,
    pub truncated: bool,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapCost {
    pub inspected_entries: usize, pub sql_vm_steps: usize,
    pub metadata_bytes: usize, pub retained_summary_bytes: usize,
    pub serialized_bytes: usize, pub source_bytes_read: usize,
    /// Control bytes are allowed; never silently counted as source body reads.
    pub policy_bytes_read: usize, pub summary_cache_hit: bool,
    #[serde(default)] pub native_rpc_calls_completed:usize,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SelectionBinding { pub visual_id: String, pub resource_id: String, pub receipt: QueryReceipt }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapResult {
    pub schema: String, pub operation: MapOperation, pub authority: MapAuthority,
    pub root: Option<String>, pub direction: MapDirection, pub relations: Vec<RelationKind>,
    pub nodes: Vec<MapNode>, pub edges: Vec<MapEdge>, pub coverage: MapCoverage,
    pub page: MapPage, pub selection: Option<SelectionBinding>, pub cost: MapCost,
}

/// Separate app-only mode on the same owned visual lane. It is NOT a fourth
/// CodeQuery operation; preview and arbitrary resource/path lookups are denied.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapSelectionRequest {
    pub schema:String, pub authority:MapAuthority, pub selection_id:String,
    #[serde(default)] pub cursor:Option<String>,
    #[serde(default="selection_rows")] pub max_refs:usize,
}
#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
#[serde(tag="mode",content="request",rename_all="snake_case",deny_unknown_fields)]
pub enum VisualRequest {Map(MapRequest),Selection(MapSelectionRequest)}
#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
#[serde(tag="mode",content="result",rename_all="snake_case",deny_unknown_fields)]
pub enum VisualResult {Map(MapResult),Selection(MapSelectionResult)}
fn selection_rows()->usize {MAX_SELECTION_REFS}
impl MapSelectionRequest {
    pub fn validate(&self)->Result<(),&'static str>{
        if self.schema!=SCHEMA{return Err("invalid_schema");}
        if !matches!(self.authority,MapAuthority::Published{..}){return Err("published_required");}
        self.authority.validate()?;
        if !bounded(&self.selection_id,MAX_ID_BYTES)||self.max_refs==0||self.max_refs>MAX_SELECTION_REFS||self.cursor.as_ref().is_some_and(|v|!bounded(v,MAX_CURSOR_BYTES)){return Err("invalid_selection");}Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapOccurrenceRef {
    pub occurrence_id:String, pub caller_id:Option<String>, pub target_id:String,
    pub relation:RelationKind, pub state:RelationState,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MapSelectionResult {
    pub schema:String, pub authority:MapAuthority, pub selection_id:String,
    /// Original evidence authority unchanged, repeated explicitly for actions.
    pub receipt:QueryReceipt, pub references:Vec<MapOccurrenceRef>,
    pub remaining:u64, pub complete:bool, pub next_cursor:Option<String>,pub cost:MapCost,
}
impl MapSelectionResult {
    pub fn validate(&self,request:&MapSelectionRequest)->Result<(),&'static str>{
        request.validate()?;
        if self.schema!=SCHEMA||self.authority!=request.authority||self.selection_id!=request.selection_id||!matches!(&self.authority,MapAuthority::Published{receipt,..}if receipt==&self.receipt){return Err("selection_authority_changed");}
        if self.references.len()>request.max_refs||self.remaining>JS_INTEGER||!safe_cost(&self.cost)||self.cost.source_bytes_read!=0||self.complete!=(self.next_cursor.is_none()&&self.remaining==0){return Err("invalid_selection_result");}
        let mut seen=std::collections::BTreeSet::new();
        for r in &self.references {
            if !canonical_resource(&r.occurrence_id)||!r.occurrence_id.starts_with("occ:")||!seen.insert(&r.occurrence_id)||!canonical_resource(&r.target_id)||!r.target_id.starts_with("decl:")||r.caller_id.as_ref().is_some_and(|s|!canonical_resource(s)||!s.starts_with("decl:"))||r.relation==RelationKind::Containment||r.state==RelationState::Structure{return Err("invalid_occurrence_reference");}
        }
        if self.next_cursor.as_ref().is_some_and(|s|!bounded(s,MAX_CURSOR_BYTES))||(!self.complete&&(self.next_cursor.is_none()||self.references.is_empty()||self.remaining==0)){return Err("invalid_selection_progress");}
        let bytes=serde_json::to_vec(self).map_err(|_|"invalid_selection_result")?.len();if bytes>MAX_SELECTION_BYTES{return Err("budget_exceeded");}if self.cost.serialized_bytes!=bytes{return Err("invalid_measured_size");}Ok(())
    }
}
impl MapResult {
    pub fn validate(&self,request:&MapRequest)->Result<(),&'static str>{
        request.validate()?;self.authority.validate()?;
        if !safe_cost(&self.cost)||[Some(self.coverage.observed_entries),self.coverage.total_entries,self.coverage.indexed_files,self.coverage.parse_failed_files,Some(self.coverage.ignored_entries),Some(self.coverage.unsupported_entries),Some(self.coverage.unavailable_entries),self.page.remaining_nodes,self.page.remaining_edges].iter().flatten().any(|n|*n>JS_INTEGER){return Err("invalid_numeric_count");}
        if self.schema!=SCHEMA||self.operation!=request.operation||self.direction!=request.direction||self.relations!=request.relations||request.expected_authority.as_ref().is_some_and(|a|a!=&self.authority){return Err("map_authority_changed");}
        if self.nodes.len()>request.limits.nodes||self.nodes.iter().filter(|n|!matches!(n.kind,NodeKind::File|NodeKind::Symbol)).count()>request.limits.groups||self.edges.len()>request.limits.edges||self.page.returned_nodes!=self.nodes.len()||self.page.returned_edges!=self.edges.len()||self.cost.source_bytes_read!=0{return Err("invalid_map_bounds");}
        if self.page.complete&&(self.page.truncated||self.page.next_cursor.is_some()||self.page.remaining_nodes!=Some(0)||self.page.remaining_edges!=Some(0)){return Err("invalid_completion");}
        if self.page.next_cursor.as_ref().is_some_and(|s|!bounded(s,MAX_CURSOR_BYTES))||(!self.page.complete&&!self.page.truncated&&self.page.next_cursor.is_none()){return Err("invalid_progress");}
        if self.operation!=MapOperation::Overview&&self.root.is_none(){return Err("missing_selected_root");}
        if let Some(MapRoot::Visual{id})=&request.root{if self.root.as_ref()!=Some(id){return Err("selected_root_changed");}}
        let preview=matches!(self.authority,MapAuthority::StructurePreview{..});
        if preview&&(self.coverage.indexed_files.is_some()||self.coverage.parse_failed_files.is_some()||self.coverage.dependencies!=DependencyCoverage::Indexing||self.selection.is_some()){return Err("preview_evidence_forbidden");}
        let mut ids=std::collections::BTreeSet::new();
        for node in &self.nodes {
            if node.resource_id.as_ref().is_some_and(|s|!canonical_resource(s))||node.file_resource_id.as_ref().is_some_and(|s|!canonical_resource(s)||!s.starts_with("file:"))||node.symbol_kind.as_ref().is_some_and(|s|!bounded(s,64))||node.qualified_name.as_ref().is_some_and(|s|!bounded(s,1024)){return Err("invalid_symbol_metadata");}
            if node.kind!=NodeKind::Symbol&&(node.symbol_kind.is_some()||node.qualified_name.is_some()||node.file_resource_id.is_some()){return Err("invalid_symbol_metadata");}
            if node.resource_id.as_ref().is_some_and(|s|node.kind==NodeKind::File&&!s.starts_with("file:")||!matches!(node.kind,NodeKind::File|NodeKind::Symbol)){return Err("invalid_symbol_metadata");}
            if node.kind==NodeKind::Symbol&&(preview||node.resource_id.as_ref().is_none_or(|s|!s.starts_with("decl:"))||node.symbol_kind.is_none()||node.file_resource_id.is_none()){return Err("invalid_symbol_metadata");}
            if !bounded(&node.id,MAX_ID_BYTES)||!bounded(&node.label,MAX_LABEL_BYTES)||!ids.insert(node.id.clone())||node.languages.len()>8||node.languages.iter().any(|l|!bounded(&l.language,32)){return Err("invalid_map_node");}
            let mut languages=std::collections::BTreeSet::new();let mut count=0u64;for language in &node.languages{if !languages.insert(&language.language)||language.count>JS_INTEGER{return Err("invalid_language_count");}count=count.checked_add(language.count).ok_or("invalid_language_count")?;}if count>node.source_files||node.source_files>JS_INTEGER||node.asset_files>JS_INTEGER{return Err("invalid_language_count");}
        }
        if self.root.as_ref().is_some_and(|root|!ids.contains(root)){return Err("missing_selected_root");}
        let mut edge_ids=std::collections::BTreeSet::new();
        for edge in &self.edges {
            if !bounded(&edge.id,MAX_ID_BYTES)||!edge_ids.insert(&edge.id)||!ids.contains(&edge.from)||!ids.contains(&edge.to)||edge.count==0||edge.count>JS_INTEGER||!self.relations.contains(&edge.relation){return Err("invalid_map_edge");}
            if edge.relation==RelationKind::Containment {if edge.state!=RelationState::Structure||edge.selection_id.is_some(){return Err("invalid_containment");}}
            else if preview||edge.state==RelationState::Structure||edge.selection_id.as_ref().is_none_or(|s|!bounded(s,MAX_ID_BYTES)){return Err("missing_guarded_selection");}
        }
        if let Some(selection)=&self.selection {if !ids.contains(&selection.visual_id)||!canonical_resource(&selection.resource_id)||!matches!(&self.authority,MapAuthority::Published{receipt,..}if receipt==&selection.receipt){return Err("invalid_selection_binding");}}
        if let Some(MapRoot::Resource{receipt,..})=&request.root{if self.selection.as_ref().is_none_or(|s|&s.receipt!=receipt||Some(&s.visual_id)!=self.root.as_ref()){return Err("selected_root_changed");}}
        let bytes=serde_json::to_vec(self).map_err(|_|"invalid_map_result")?.len();if bytes>request.limits.bytes{return Err("budget_exceeded");}if self.cost.serialized_bytes!=bytes{return Err("invalid_measured_size");}Ok(())
    }
}

#[cfg(test)] mod tests {
    use super::*;
    fn measured(result:&mut MapResult){for _ in 0..8{let n=serde_json::to_vec(result).unwrap().len();if n==result.cost.serialized_bytes{return;}result.cost.serialized_bytes=n;}panic!("fixture size did not stabilize");}
    fn measured_selection(result:&mut MapSelectionResult){for _ in 0..8{let n=serde_json::to_vec(result).unwrap().len();if n==result.cost.serialized_bytes{return;}result.cost.serialized_bytes=n;}panic!("fixture size did not stabilize");}
    #[test] fn strict_request_rejects_agent_fields_roots_and_oversized_limits(){
        let mut request:MapRequest=serde_json::from_value(serde_json::json!({"schema":SCHEMA,"operation":"overview"})).unwrap();request.validate().unwrap();
        assert!(serde_json::from_value::<MapRequest>(serde_json::json!({"schema":SCHEMA,"operation":"overview","path":"/private"})).is_err());
        request.limits.nodes=MAX_NODES+1;assert_eq!(request.validate(),Err("invalid_limits"));
        request.limits=MapLimits::default();request.operation=MapOperation::Neighbors;assert_eq!(request.validate(),Err("missing_authority"));
    }
    #[test] fn golden_variants_do_not_mix_preview_and_evidence_authority(){
        let preview:MapResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_preview.json")).unwrap();
        let published:MapResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_published.json")).unwrap();
        assert!(matches!(preview.authority,MapAuthority::StructurePreview{..}));assert!(preview.coverage.indexed_files.is_none());assert!(preview.selection.is_none());
        assert!(preview.edges.iter().all(|e|e.relation==RelationKind::Containment&&e.state==RelationState::Structure&&e.selection_id.is_none()));
        assert!(matches!(published.authority,MapAuthority::Published{..}));assert!(published.edges.iter().any(|e|e.relation==RelationKind::Call&&e.state==RelationState::Exact&&e.count==1&&e.selection_id.is_some()));
        for result in [preview,published]{assert_eq!(result.schema,SCHEMA);result.authority.validate().unwrap();assert_eq!(result.page.returned_nodes,result.nodes.len());assert_eq!(result.page.returned_edges,result.edges.len());assert!(serde_json::to_vec(&result).unwrap().len()<=MAX_BYTES);}
    }
    #[test] fn selection_is_published_receipt_bound_and_never_an_agent_operation(){
        let mut result:MapSelectionResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_selection.json")).unwrap();measured_selection(&mut result);
        let mut request=MapSelectionRequest{schema:SCHEMA.into(),authority:result.authority.clone(),selection_id:result.selection_id.clone(),cursor:None,max_refs:50};result.validate(&request).unwrap();
        let wire=VisualRequest::Selection(request.clone());let value=serde_json::to_value(&wire).unwrap();assert_eq!(value["mode"],"selection");assert!(value.get("operation").is_none());
        let preview:MapResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_preview.json")).unwrap();request.authority=preview.authority;assert_eq!(request.validate(),Err("published_required"));
        request.authority=result.authority.clone();request.max_refs=51;assert_eq!(request.validate(),Err("invalid_selection"));request.max_refs=50;
        let mut wrong=result.clone();wrong.receipt.evidence.head_generation="another-head".into();assert!(wrong.validate(&request).is_err());
        let mut wrong=result.clone();wrong.references[0].state=RelationState::Structure;assert!(wrong.validate(&request).is_err());
        let mut wrong=result.clone();wrong.references.push(wrong.references[0].clone());assert!(wrong.validate(&request).is_err());
    }
    #[test] fn golden_result_invariants_reject_dangling_or_preview_evidence_actions(){
        let request:MapRequest=serde_json::from_value(serde_json::json!({"schema":SCHEMA,"operation":"overview"})).unwrap();
        let mut result:MapResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_published.json")).unwrap();measured(&mut result);result.validate(&request).unwrap();
        result.edges.last_mut().unwrap().selection_id=None;assert_eq!(result.validate(&request),Err("missing_guarded_selection"));
        let mut result:MapResult=serde_json::from_str(include_str!("../tests/fixtures/code_map_preview.json")).unwrap();measured(&mut result);result.validate(&request).unwrap();
        result.coverage.indexed_files=Some(0);assert_eq!(result.validate(&request),Err("preview_evidence_forbidden"));
    }
}
