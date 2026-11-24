//! Bounded syntax-only Python local value dependencies.
use super::*;
use tree_sitter::Node;
const MAX_DEFS:usize=64;
const MAX_USES_PER_DEF:usize=16;
const MAX_USES:usize=256;

#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalUse {pub occurrence_id:String,pub span:SourceSpan,pub definition_id:String}
#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalDefinition {pub id:String,pub kind:String,pub name:String,pub target_span:SourceSpan,#[serde(skip_serializing_if="Option::is_none")]pub statement_span:Option<SourceSpan>,pub uses:Vec<LocalUse>}
#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalValueSlice {pub version:u32,pub profile:String,pub function_id:String,pub status:String,#[serde(skip_serializing_if="Option::is_none")]pub reason:Option<String>,#[serde(skip_serializing_if="Option::is_none")]pub return_occurrence_id:Option<String>,#[serde(skip_serializing_if="Option::is_none")]pub root_definition_id:Option<String>,pub definitions:Vec<LocalDefinition>}

fn unsupported(function_id:&str, reason:&str)->LocalValueSlice{LocalValueSlice{version:1,profile:"python_straight_line_v1".into(),function_id:function_id.into(),status:"unsupported".into(),reason:Some(reason.into()),return_occurrence_id:None,root_definition_id:None,definitions:vec![]}}
fn profile_tick(builder:&mut Builder<'_>,depth:usize)->Result<(),&'static str>{if builder.work>=builder.budget.max_ast_nodes || depth>builder.budget.max_depth{return Err("profile_limit");}builder.tick(depth).map_err(|_|"profile_limit")}
fn identifier(node:Node<'_>, source:&str)->bool{let value=text(node,source);node.kind()=="identifier"&&!value.is_empty()&&value.len()<=128&&value.is_ascii()}
fn find_occurrence(builder:&mut Builder<'_>,node:Node<'_>,function_id:&str)->Result<Option<String>,&'static str>{let work=builder.work.checked_add(builder.file.occurrences.len()).ok_or("profile_limit")?;if work>builder.budget.max_ast_nodes{return Err("profile_limit");}builder.work=work;let a=node.start_byte() as u64;let b=node.end_byte() as u64;let mut matches=builder.file.occurrences.iter().filter(|o|o.caller_id.as_deref()==Some(function_id)&&o.kind==OccurrenceKind::ValueReference&&o.source.span.start_byte==a&&o.source.span.end_byte==b);let Some(o)=matches.next()else{return Ok(None)};if matches.next().is_some(){Ok(None)}else{Ok(Some(o.id.clone()))}}
fn definition_id(builder:&Builder<'_>,func:Node<'_>,node:Node<'_>,kind:&str)->String{digest(&["python_slice_def_v1",&builder.file.file_id,&builder.file.source_hash,&func.start_byte().to_string(),&node.start_byte().to_string(),&node.end_byte().to_string(),kind])}
fn expression(builder:&mut Builder<'_>,node:Node<'_>,func_id:&str,env:&HashMap<String,String>,uses:&mut Vec<LocalUse>,depth:usize,total:&mut usize)->Result<(),&'static str>{
 profile_tick(builder,depth)?;
 match node.kind(){
  "identifier"=>{if !identifier(node,builder.source){return Err("unsupported_syntax");}let def=env.get(text(node,builder.source)).ok_or("unbound_name")?;let occ=find_occurrence(builder,node,func_id)?.ok_or("missing_evidence")?;*total+=1;if uses.len()>=MAX_USES_PER_DEF||*total>MAX_USES{return Err("profile_limit");}uses.push(LocalUse{occurrence_id:occ,span:span(node),definition_id:def.clone()});Ok(())},
  "integer"=>Ok(()),
  "parenthesized_expression"=>{let kids=children(node);if kids.len()!=1{return Err("unsupported_syntax");}expression(builder,kids[0],func_id,env,uses,depth+1,total)},
  "binary_operator"=>{let operator=node.child_by_field_name("operator").ok_or("unsupported_syntax")?;if !matches!(text(operator,builder.source),"+"|"-"|"*"){return Err("unsupported_syntax");}let l=node.child_by_field_name("left").ok_or("unsupported_syntax")?;let r=node.child_by_field_name("right").ok_or("unsupported_syntax")?;expression(builder,l,func_id,env,uses,depth+1,total)?;expression(builder,r,func_id,env,uses,depth+1,total)},
  _=>Err("unsupported_syntax")
 }
}
fn build_one(builder:&mut Builder<'_>,node:Node<'_>,func_id:&str)->Result<LocalValueSlice,&'static str>{
 if node.parent().is_some_and(|p|p.kind()=="decorated_definition")||text(node,builder.source).trim_start().starts_with("async ")||node.child_by_field_name("return_type").is_some()||node.child_by_field_name("type_parameters").is_some(){return Err("unsupported_syntax");}
 let params=node.child_by_field_name("parameters").ok_or("unsupported_syntax")?;let body=node.child_by_field_name("body").ok_or("unsupported_syntax")?;
 let mut env=HashMap::<String,String>::new();let mut defs=Vec::new();let mut total=0usize;
 for param in children(params).into_iter().filter(|p|p.kind()!="comment"){profile_tick(builder,1)?;if !identifier(param,builder.source){return Err("unsupported_syntax");}let name=text(param,builder.source).to_owned();if env.contains_key(&name){return Err("ambiguous_binding");}if defs.len()>=MAX_DEFS{return Err("profile_limit");}let id=definition_id(builder,node,param,"parameter");env.insert(name.clone(),id.clone());defs.push(LocalDefinition{id,kind:"parameter".into(),name,target_span:span(param),statement_span:None,uses:vec![]});}
 let mut stmts:Vec<_>=children(body).into_iter().filter(|n|n.kind()!="comment").collect();
 if let Some(first)=stmts.first().copied(){if first.kind()=="expression_statement" {let cs=children(first);if cs.len()==1 && cs[0].kind()=="string" && text(cs[0],builder.source).starts_with(['\'','"']) {stmts.remove(0);}}}
 let ret=stmts.pop().ok_or("unsupported_syntax")?;if ret.kind()!="return_statement"{return Err("unsupported_syntax");}let values=children(ret);if values.len()!=1||!identifier(values[0],builder.source){return Err("unsupported_syntax");}
 for statement in stmts {profile_tick(builder,1)?;if statement.kind()!="expression_statement"{return Err("unsupported_syntax");}let parts=children(statement);if parts.len()!=1||parts[0].kind()!="assignment"{return Err("unsupported_syntax");}let assign=parts[0];if assign.child_by_field_name("type").is_some(){return Err("unsupported_syntax");}let left=assign.child_by_field_name("left").ok_or("unsupported_syntax")?;let right=assign.child_by_field_name("right").ok_or("unsupported_syntax")?;if !identifier(left,builder.source)||defs.len()>=MAX_DEFS{return Err(if defs.len()>=MAX_DEFS{"profile_limit"}else{"unsupported_syntax"});}let mut uses=vec![];expression(builder,right,func_id,&env,&mut uses,1,&mut total)?;let name=text(left,builder.source).to_owned();let id=definition_id(builder,node,left,"assignment");env.insert(name.clone(),id.clone());defs.push(LocalDefinition{id,kind:"assignment".into(),name,target_span:span(left),statement_span:Some(span(statement)),uses});}
 let root=env.get(text(values[0],builder.source)).ok_or("unbound_name")?.clone();let seed=find_occurrence(builder,values[0],func_id)?.ok_or("missing_evidence")?;
 Ok(LocalValueSlice{version:1,profile:"python_straight_line_v1".into(),function_id:func_id.into(),status:"complete".into(),reason:None,return_occurrence_id:Some(seed),root_definition_id:Some(root),definitions:defs})
}
pub(super) fn extract(builder:&mut Builder<'_>,root:Node<'_>)->Result<Vec<LocalValueSlice>,EvidenceError>{
 let mut stack=vec![root];let mut out=Vec::new();while let Some(node)=stack.pop(){if profile_tick(builder,0).is_err(){break;}if node.kind()=="function_definition" {if let Some(index)=builder.declaration_nodes.get(&node.id()).copied().filter(|&i| matches!(builder.file.declarations[i].kind.as_str(), "function" | "method")){let id=builder.file.declarations[index].id.clone();let value=if builder.file.declarations[index].kind!="function" {unsupported(&id,"unsupported_syntax")}else{match build_one(builder,node,&id){Ok(v)=>v,Err(reason)=>unsupported(&id,reason)}};let before=builder.output_bytes;if builder.account(&value).is_err(){builder.output_bytes=before;break;}out.push(value);}}for child in children(node).into_iter().rev(){stack.push(child);}}
 Ok(out)
}

pub(super) fn validate(file:&FileEvidence,budget:&ExtractionBudget)->Result<(),EvidenceError>{
 let Some(profiles)=&file.local_value_slices else{return Ok(());};
 if file.language!="python"||profiles.len()>budget.max_declarations{return Err(error(EvidenceErrorCode::InvalidEvidence,"invalid local slice profile"));}
 limit(file.declarations.len().saturating_add(file.bindings.len()).saturating_add(file.scopes.len()).saturating_add(file.occurrences.len())<=budget.max_ast_nodes,"local slice validation limit")?;
 let declaration_index:HashMap<_,_>=file.declarations.iter().map(|v|(v.id.as_str(),v)).collect();
 let binding_index:HashMap<_,_>=file.bindings.iter().map(|v|((v.source.span.start_byte,v.source.span.end_byte),v)).collect();
 let scope_index:HashMap<_,_>=file.scopes.iter().map(|v|(v.id.as_str(),v)).collect();
 let occurrence_index:HashMap<_,_>=file.occurrences.iter().map(|v|(v.id.as_str(),v)).collect();
 let bad=||error(EvidenceErrorCode::InvalidEvidence,"invalid local slice evidence");let mut functions=HashSet::new();
 for profile in profiles{
  if profile.version!=1||profile.profile!="python_straight_line_v1"||!functions.insert(&profile.function_id){return Err(bad());}
  let function=declaration_index.get(profile.function_id.as_str()).copied().filter(|d|matches!(d.kind.as_str(),"function"|"method")).ok_or_else(bad)?;
  if profile.status=="unsupported" {if !matches!(profile.reason.as_deref(),Some("unsupported_syntax"|"profile_limit"|"unbound_name"|"missing_evidence"|"ambiguous_binding"))||profile.return_occurrence_id.is_some()||profile.root_definition_id.is_some()||!profile.definitions.is_empty(){return Err(bad());}continue;}
  if profile.status!="complete"||profile.reason.is_some()||profile.definitions.len()>MAX_DEFS||function.kind!="function"||function.identity_state!=IdentityState::Stable{return Err(bad());}
  let mut ids=HashSet::new();let mut used_occurrences=HashSet::new();let mut env=HashMap::<String,String>::new();let(mut total,mut previous,mut assignments)=(0usize,function.source.span.start_byte,false);
  for def in &profile.definitions{
   if !matches!(def.kind.as_str(),"parameter"|"assignment")||!ids.insert(def.id.clone())||def.name.is_empty()||!def.name.is_ascii()||def.name.len()>128||def.target_span.start_byte<function.source.span.start_byte||def.target_span.end_byte>function.source.span.end_byte||def.target_span.start_byte<previous||def.target_span.start_byte>=def.target_span.end_byte||def.uses.len()>MAX_USES_PER_DEF{return Err(bad());}
   let expected=digest(&["python_slice_def_v1",&file.file_id,&file.source_hash,&function.source.span.start_byte.to_string(),&def.target_span.start_byte.to_string(),&def.target_span.end_byte.to_string(),&def.kind]);if expected!=def.id{return Err(bad());}
   let binding=binding_index.get(&(def.target_span.start_byte,def.target_span.end_byte)).copied().filter(|b|b.name==def.name && b.source.span==def.target_span && matches!((&b.kind,def.kind.as_str()),(BindingKind::Parameter,"parameter")|(BindingKind::Local,"assignment"))).ok_or_else(bad)?;
   let scope=scope_index.get(binding.scope_id.as_str()).copied().ok_or_else(bad)?;if scope.declaration_id.as_deref()!=Some(function.id.as_str()){return Err(bad());}
   if def.kind=="parameter"{if assignments||def.statement_span.is_some()||!def.uses.is_empty()||env.contains_key(&def.name){return Err(bad());}}else{assignments=true;let st=def.statement_span.as_ref().ok_or_else(bad)?;if st.start_byte>def.target_span.start_byte||st.end_byte<def.target_span.end_byte||st.end_byte>function.source.span.end_byte{return Err(bad());}for u in &def.uses{total+=1;if !used_occurrences.insert(&u.occurrence_id)||total>MAX_USES||u.span.start_byte<def.target_span.end_byte||u.span.end_byte>st.end_byte{return Err(bad());}let occ=occurrence_index.get(u.occurrence_id.as_str()).copied().filter(|o|o.kind==OccurrenceKind::ValueReference && o.caller_id.as_deref()==Some(function.id.as_str()) && o.source.span==u.span).ok_or_else(bad)?;if env.get(&occ.symbol_text)!=Some(&u.definition_id){return Err(bad());}}}
   previous=def.statement_span.as_ref().map_or(def.target_span.end_byte,|s|s.end_byte);env.insert(def.name.clone(),def.id.clone());
  }
  let seed=profile.return_occurrence_id.as_ref().ok_or_else(bad)?;let occ=occurrence_index.get(seed.as_str()).copied().filter(|o|o.kind==OccurrenceKind::ValueReference && o.caller_id.as_deref()==Some(function.id.as_str())).ok_or_else(bad)?;
  if occ.source.span.start_byte<previous||occ.source.span.end_byte>function.source.span.end_byte||env.get(&occ.symbol_text)!=profile.root_definition_id.as_ref(){return Err(bad());}
 }
 Ok(())
}

#[cfg(test)]
mod tests {
 use super::*;
 #[test] fn straight_line_reassignment_and_unsupported_are_distinct(){
  let source=b"def calc(a, b):\n    \"note\"\n    x = a + 1\n    x = x * b\n    return x\n\ndef bad(a):\n    if a:\n        return a\n    return a\n";
  let f=extract_evidence("checkout","sample.py",source,&ExtractionBudget::default()).unwrap();
  let profiles=f.local_value_slices.as_ref().unwrap();assert_eq!(profiles.len(),2);
  let good=profiles.iter().find(|p|p.status=="complete").unwrap();assert_eq!(good.definitions.len(),4);
  assert_eq!(good.definitions[3].uses[0].definition_id,good.definitions[2].id);
  assert_eq!(good.definitions[3].uses[1].definition_id,good.definitions[1].id);
  assert_eq!(good.root_definition_id.as_deref(),Some(good.definitions[3].id.as_str()));
  assert!(profiles.iter().any(|p|p.status=="unsupported"&&p.reason.as_deref()==Some("unsupported_syntax")));
 }
}
