//! Bounded syntactic Python try/except regions; no exception-flow inference.
use super::*;
use tree_sitter::Node;
const MAX_REGIONS: usize = 64;
const MAX_HANDLERS: usize = 128;
const MAX_DEPTH: usize = 32;
const MAX_CALLS: usize = 256;
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorCall { pub id:String, pub span:SourceSpan }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorRegion { pub region: SourceSpan, pub body: SourceSpan, pub handlers: Vec<SourceSpan>, #[serde(skip_serializing_if="Option::is_none")] pub else_clause: Option<SourceSpan>, #[serde(skip_serializing_if="Option::is_none")] pub finally_clause: Option<SourceSpan>, pub calls: Vec<ErrorCall> }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorProfile { pub version:u32, pub function_id:String, pub status:String, #[serde(skip_serializing_if="Option::is_none")] pub reason:Option<String>, pub regions:Vec<ErrorRegion> }
fn unsupported(id:&str,reason:&str)->ErrorProfile { ErrorProfile{version:1,function_id:id.into(),status:"unsupported".into(),reason:Some(reason.into()),regions:vec![]} }
fn build(builder:&mut Builder<'_>, node:Node<'_>, id:&str)->Result<ErrorProfile,&'static str> {
 if node.has_error(){return Err("unsupported_syntax")}
 let mut regions=Vec::new();let mut handlers_count=0usize;let mut calls_count=0usize;
 let body=node.child_by_field_name("body").ok_or("unsupported_syntax")?;
 let mut stack=vec![(body,0usize)];
 while let Some((n,depth))=stack.pop(){
  if depth>MAX_DEPTH || builder.tick(depth).is_err(){return Err("profile_limit")}
  if n.id()!=body.id() && matches!(n.kind(),"function_definition"|"class_definition"|"lambda"){continue;}
  if n.kind()=="try_statement" {
   let body=n.child_by_field_name("body").ok_or("unsupported_syntax")?;
   let mut handlers=Vec::new();let(mut else_clause,mut finally_clause)=(None,None);
   for child in children(n){match child.kind(){
    "except_clause"=>{let t=text(child,builder.source).trim_start();if !t.starts_with("except") || t.starts_with("except*") || t.starts_with("except *"){return Err("unsupported_syntax")} handlers.push(span(child));},
    "else_clause"=>{if else_clause.replace(span(child)).is_some(){return Err("unsupported_syntax")}},
    "finally_clause"=>{if finally_clause.replace(span(child)).is_some(){return Err("unsupported_syntax")}}, _=>{}
   }}
   if handlers.is_empty(){return Err("unsupported_syntax")}
   handlers_count+=handlers.len();if handlers_count>MAX_HANDLERS || regions.len()>=MAX_REGIONS{return Err("profile_limit")}
   let list_len=builder.file.occurrences.len();if builder.work.checked_add(list_len).filter(|v|*v<=builder.budget.max_ast_nodes).is_none(){return Err("profile_limit")};builder.work+=list_len;
   let calls=builder.file.occurrences.iter().filter(|o|o.kind==OccurrenceKind::Call && o.caller_id.as_deref()==Some(id) && o.source.span.start_byte>=body.start_byte() as u64 && o.source.span.end_byte<=body.end_byte() as u64).map(|o|ErrorCall{id:o.id.clone(),span:o.source.span.clone()}).collect::<Vec<_>>();
   calls_count+=calls.len();if calls_count>MAX_CALLS{return Err("profile_limit")}
   regions.push(ErrorRegion{region:span(n),body:span(body),handlers,else_clause,finally_clause,calls});
  }
  for child in children(n).into_iter().rev(){stack.push((child,depth+1));}
 }
 Ok(ErrorProfile{version:1,function_id:id.into(),status:"complete".into(),reason:None,regions})
}
pub(super) fn extract(builder:&mut Builder<'_>,root:Node<'_>)->Result<Vec<ErrorProfile>,EvidenceError>{
 let mut result=Vec::new();let mut stack=vec![root];while let Some(node)=stack.pop(){if node.kind()=="function_definition" {if let Some(index)=builder.declaration_nodes.get(&node.id()).copied().filter(|&i| matches!(builder.file.declarations[i].kind.as_str(), "function" | "method")){let id=builder.file.declarations[index].id.clone();let profile=build(builder,node,&id).unwrap_or_else(|reason|unsupported(&id,reason));let before=builder.output_bytes;if builder.account(&profile).is_err(){builder.output_bytes=before;break;}result.push(profile);}} for child in children(node).into_iter().rev(){stack.push(child)}} Ok(result)
}
pub(super) fn validate(file:&FileEvidence,budget:&ExtractionBudget)->Result<(),EvidenceError>{
 let Some(profiles)=&file.error_boundaries else{return Ok(())};
 let bad=||error(EvidenceErrorCode::InvalidEvidence,"invalid error boundary evidence");
 if file.language!="python"||profiles.len()>budget.max_declarations{return Err(bad())}
 let declarations:HashMap<_,_>=file.declarations.iter().map(|d|(d.id.as_str(),d)).collect();
 let occurrences:HashMap<_,_>=file.occurrences.iter().map(|o|(o.id.as_str(),o)).collect();let mut seen=HashSet::new();
 for profile in profiles {
  let function=declarations.get(profile.function_id.as_str()).copied().filter(|d|matches!(d.kind.as_str(),"function"|"method")).ok_or_else(bad)?;
  if profile.version!=1||!seen.insert(&profile.function_id){return Err(bad())}
  if profile.status=="unsupported" {if !matches!(profile.reason.as_deref(),Some("unsupported_syntax"|"profile_limit"|"ambiguous_binding"))||!profile.regions.is_empty(){return Err(bad())}continue}
  if profile.status!="complete"||profile.reason.is_some()||profile.regions.len()>MAX_REGIONS||function.identity_state!=IdentityState::Stable{return Err(bad())}
  let(mut handlers,mut calls)=(0usize,0usize);let mut starts=HashSet::new();
  let inside=|outer:&SourceSpan,inner:&SourceSpan|inner.start_byte>=outer.start_byte&&inner.end_byte<=outer.end_byte&&inner.start_byte<inner.end_byte;
  for region in &profile.regions {
   if !inside(&function.source.span,&region.region)||!inside(&region.region,&region.body)||!starts.insert(region.region.start_byte)||region.handlers.is_empty(){return Err(bad())}
   handlers+=region.handlers.len();calls+=region.calls.len();if handlers>MAX_HANDLERS||calls>MAX_CALLS{return Err(bad())}
   let mut previous=region.body.end_byte;for h in &region.handlers{if !inside(&region.region,h)||h.start_byte<previous{return Err(bad())}previous=h.end_byte;}
   for optional in [&region.else_clause,&region.finally_clause].into_iter().flatten(){if !inside(&region.region,optional)||optional.start_byte<previous{return Err(bad())}previous=optional.end_byte;}
   let mut ids=HashSet::new();for call in &region.calls{let occurrence=occurrences.get(call.id.as_str()).copied().filter(|o|o.kind==OccurrenceKind::Call&&o.caller_id.as_deref()==Some(function.id.as_str())).ok_or_else(bad)?;if !ids.insert(&call.id)||call.span!=occurrence.source.span||!inside(&region.body,&call.span){return Err(bad())}}
  }
 }
 Ok(())
}
