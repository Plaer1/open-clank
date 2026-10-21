//! Lazy, receipt-bound declaration projection over the existing generation.
//! Rich source remains on demand; no read-side DDL or second index owner.
use super::*;

pub(super) fn opaque(tag:&str,resource:&str,ns:&str,secret:&[u8;32])->String {
 let message=format!("semantic-map-v2:{ns}:{tag}:{resource}");
 format!("{tag}:{resource}:{}",blake3::keyed_hash(secret,message.as_bytes()).to_hex())
}
pub(super) fn decode(value:&str,tag:&str,ns:&str,secret:&[u8;32])->Result<String,QueryError>{
 let rest=value.strip_prefix(&format!("{tag}:")).ok_or_else(||fail("stale_cursor"))?;
 let(resource,mac)=rest.rsplit_once(':').ok_or_else(||fail("stale_cursor"))?;
 if !canonical_resource(resource){return Err(fail("stale_cursor"));}
 let expected=opaque(tag,resource,ns,secret);let expected=expected.rsplit(':').next().unwrap();
 if mac.len()!=expected.len()||mac.bytes().zip(expected.bytes()).fold(0u8,|d,(a,b)|d|(a^b))!=0{return Err(fail("stale_cursor"));}
 Ok(resource.into())
}
pub(super) fn relation(value:i64)->Result<RelationKind,QueryError>{Ok(match value {
 1=>RelationKind::Call,2=>RelationKind::Import,3=>RelationKind::ValueReference,4=>RelationKind::TypeReference,
 5=>RelationKind::Inherits,6=>RelationKind::Implements,7=>RelationKind::Render,8=>RelationKind::Route,9=>RelationKind::Overrides,_=>return Err(fail("invalid_evidence"))})}
#[derive(Clone)]
struct Symbol{resource:String,file:String,parent:String,label:String,kind:String,qualified:String}
fn symbols_sql(partition:bool)->&'static str{if partition {
 "SELECT resource,file_resource,parent,label,kind,qualified FROM visual_symbols"
}else{
 "SELECT 'decl:'||json_extract(d.value,'$.id') resource,'file:'||json_extract(f.payload_json,'$.file_id') file_resource,coalesce('decl:'||json_extract(d.value,'$.lexical_parent_id'),'file:'||json_extract(f.payload_json,'$.file_id')) parent,json_extract(d.value,'$.name') label,json_extract(d.value,'$.kind') kind,json_extract(d.value,'$.qualified_name') qualified FROM code_file_evidence f,json_each(f.payload_json,'$.declarations') d WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3"
}}
fn read_symbols(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,field:&str,value:&str,offset:usize,limit:usize,work:&mut Work)->Result<Vec<Symbol>,QueryError>{
 let sql=format!("SELECT resource,file_resource,parent,label,kind,qualified FROM ({}) WHERE {field}=?4 ORDER BY resource LIMIT ?5 OFFSET ?6",symbols_sql(partition));
 let mut stmt=db.prepare_cached(&sql).map_err(bad)?;
 let rows=stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout,value,limit as i64,offset as i64],|r|Ok(Symbol{resource:r.get(0)?,file:r.get(1)?,parent:r.get(2)?,label:r.get(3)?,kind:r.get(4)?,qualified:r.get(5)?})).map_err(bad)?;
 let mut result=Vec::new();for row in rows{work.charge(1)?;let mut s=row.map_err(bad)?;
  if !canonical_resource(&s.resource)||!s.resource.starts_with("decl:")||!canonical_resource(&s.file)||!s.file.starts_with("file:")||!canonical_resource(&s.parent)||s.kind.len()>64||s.qualified.len()>4096||s.label.len()>4096{return Err(fail("invalid_evidence"));}
  let bytes=s.resource.len()+s.file.len()+s.parent.len()+s.label.len()+s.kind.len()+s.qualified.len();work.charge(bytes)?;context.metadata_bytes.set(context.metadata_bytes.get().saturating_add(bytes));
  while s.label.len()>MAX_LABEL_BYTES{s.label.pop();}while s.qualified.len()>1024{s.qualified.pop();}result.push(s);
 }Ok(result)
}
fn node_for(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,summary:&Summary,resource:&str,ns:&str,secret:&[u8;32],work:&mut Work)->Result<MapNode,QueryError>{
 if resource.starts_with("file:"){let i=summary.nodes.iter().position(|n|n.resource.as_deref()==Some(resource)).ok_or_else(||fail("missing"))?;return Ok(node(summary,i,ns,secret));}
 let s=read_symbols(db,partition,scope,context,"resource",resource,0,1,work)?.pop().ok_or_else(||fail("missing"))?;
 let parent=if s.parent.starts_with("file:"){summary.nodes.iter().position(|n|n.resource.as_deref()==Some(&s.parent)).map(|i|token("vn1",&[i],ns,secret)).ok_or_else(||fail("invalid_evidence"))?}else{opaque("vd2",&s.parent,ns,secret)};
 Ok(MapNode{id:opaque("vd2",&s.resource,ns,secret),label:s.label,kind:NodeKind::Symbol,parent:Some(parent),source_files:0,asset_files:0,languages:vec![],resource_id:Some(s.resource),symbol_kind:Some(s.kind),qualified_name:Some(s.qualified),file_resource_id:Some(s.file)})
}
#[derive(Clone)]
struct Reference{occurrence:String,caller:Option<String>,target:String,kind:i64,state:String,file:String}
fn references_sql(partition:bool)->&'static str{if partition{
 "SELECT e.occurrence,e.caller,e.target,k.kind,e.state,'file:'||json_extract(f.header,'$.file_id') file_resource FROM edges e JOIN files f ON f.id=e.file JOIN nodes o ON o.id='occ:'||e.occurrence JOIN occurrence_kinds k ON k.occurrence=o.row WHERE e.target IS NOT NULL AND e.state IN('exact','inferred')"
}else{
 "SELECT json_extract(r.payload_json,'$.occurrence_id') occurrence,json_extract(r.payload_json,'$.caller_id') caller,json_extract(r.payload_json,'$.target_id') target,CASE json_extract(o.value,'$.syntax_kind') WHEN 'inherits' THEN 5 WHEN 'implements' THEN 6 WHEN 'jsx_render' THEN 7 WHEN 'fastapi_route_registration' THEN 8 WHEN 'overrides' THEN 9 ELSE CASE json_extract(o.value,'$.kind') WHEN 'call' THEN 1 WHEN 'import_reference' THEN 2 WHEN 'value_reference' THEN 3 WHEN 'type_reference' THEN 4 END END kind,json_extract(r.payload_json,'$.state') state,'file:'||json_extract(f.payload_json,'$.file_id') file_resource FROM code_resolutions r JOIN code_file_evidence f ON f.owner=r.owner AND f.workspace_id=r.workspace_id AND f.checkout_id=r.checkout_id,json_each(f.payload_json,'$.occurrences') o WHERE r.owner=?1 AND r.workspace_id=?2 AND r.checkout_id=?3 AND json_extract(o.value,'$.id')=json_extract(r.payload_json,'$.occurrence_id') AND json_extract(r.payload_json,'$.target_id') IS NOT NULL AND json_extract(r.payload_json,'$.state') IN('exact','inferred')"
}}
fn read_refs(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,predicate:&str,value:&str,offset:usize,limit:usize,work:&mut Work)->Result<Vec<Reference>,QueryError>{
 let sql=format!("SELECT occurrence,caller,target,kind,state,file_resource FROM ({}) WHERE {predicate} ORDER BY occurrence LIMIT ?5 OFFSET ?6",references_sql(partition));
 let mut stmt=db.prepare_cached(&sql).map_err(bad)?;let rows=stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout,value,limit as i64,offset as i64],|r|Ok(Reference{occurrence:r.get(0)?,caller:r.get(1)?,target:r.get(2)?,kind:r.get(3)?,state:r.get(4)?,file:r.get(5)?})).map_err(bad)?;
 let mut out=Vec::new();for row in rows{work.charge(1)?;let r=row.map_err(bad)?;let bytes=r.occurrence.len()+r.caller.as_ref().map_or(0,String::len)+r.target.len()+r.state.len()+r.file.len()+8;work.charge(bytes)?;context.metadata_bytes.set(context.metadata_bytes.get().saturating_add(bytes));if !canonical_resource(&format!("occ:{}",r.occurrence))||!canonical_resource(&format!("decl:{}",r.target))||r.caller.as_ref().is_some_and(|s|!canonical_resource(&format!("decl:{s}"))){return Err(fail("invalid_evidence"));}out.push(r);}Ok(out)
}
fn include_chain(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,summary:&Summary,node:MapNode,ns:&str,secret:&[u8;32],work:&mut Work,nodes:&mut Vec<MapNode>)->Result<(),QueryError>{
 let mut current=Some(node);let mut seen=BTreeSet::new();
 for _ in 0..=PREVIEW_DEPTH {
  let Some(node)=current else{return Ok(());};work.charge(1)?;
  if !seen.insert(node.id.clone()){return Err(fail("invalid_evidence"));}
  let parent=node.parent.clone();if !nodes.iter().any(|n|n.id==node.id){nodes.push(node);}
  current=if let Some(parent)=parent {
   if parent.starts_with("vd2:"){let resource=decode(&parent,"vd2",ns,secret)?;Some(node_for(db,partition,scope,context,summary,&resource,ns,secret,work)?)}else{let i=untoken(&parent,"vn1",1,ns,secret)?[0];if i>=summary.nodes.len(){return Err(fail("invalid_evidence"));}Some(super::node(summary,i,ns,secret))}
  }else{None};
 }Err(fail("budget_exceeded"))
}
pub(super) fn page(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,summary:&Summary,authority:&MapAuthority,request:&MapRequest,resource:&str,ns:&str,secret:&[u8;32],work:&mut Work)->Result<MapResult,QueryError>{
 let root=node_for(db,partition,scope,context,summary,resource,ns,secret,work)?;
 let page_ns=format!("{ns}:{}",blake3::hash(&serde_json::to_vec(&(resource,request.operation,request.direction,&request.relations,&request.limits)).map_err(bad)?).to_hex());
 let offset=request.cursor.as_ref().map(|c|untoken(c,"sc2",1,&page_ns,secret).map(|v|v[0])).transpose()?.unwrap_or(0);
 let children=request.operation==MapOperation::Children;
 let child_rows=if children {read_symbols(db,partition,scope,context,"parent",resource,offset,request.limits.nodes+1,work)?}else{vec![]};
 let predicate=match request.direction{MapDirection::Incoming=>"target=?4",MapDirection::Outgoing=>"caller=?4",MapDirection::Both=>"(caller=?4 OR target=?4)"};
 let requested_kinds=(1..=9).filter(|k|relation(*k).is_ok_and(|r|request.relations.contains(&r))).map(|k|k.to_string()).collect::<Vec<_>>().join(",");
 let predicate=if requested_kinds.is_empty(){format!("{predicate} AND 0")}else{format!("{predicate} AND kind IN({requested_kinds})")};
 let refs=if children{vec![]}else{read_refs(db,partition,scope,context,&predicate,resource.strip_prefix("decl:").ok_or_else(||fail("invalid"))?,offset,request.limits.edges+1,work)?};
 let mut result=MapResult{schema:SCHEMA.into(),operation:request.operation,authority:authority.clone(),root:Some(root.id.clone()),direction:request.direction,relations:request.relations.clone(),nodes:vec![root.clone()],edges:vec![],coverage:summary.coverage.clone(),page:MapPage{returned_nodes:0,returned_edges:0,remaining_nodes:None,remaining_edges:None,complete:false,next_cursor:None,truncated:false},selection:Some(SelectionBinding{visual_id:root.id,resource_id:resource.into(),receipt:context.receipt.clone().unwrap()}),cost:MapCost::default()};
 let mut initial_nodes=Vec::new();include_chain(db,partition,scope,context,summary,result.nodes.remove(0),ns,secret,work,&mut initial_nodes)?;result.nodes=initial_nodes;
 if result.nodes.len()>request.limits.nodes||result.nodes.iter().filter(|n|matches!(n.kind,NodeKind::Repository|NodeKind::Folder)).count()>request.limits.groups||serde_json::to_vec(&result).map_err(bad)?.len()+1024>request.limits.bytes{return Err(fail("budget_exceeded"));}
 let available=if children{child_rows.len()}else{refs.len()};let mut consumed=0;
 for i in 0..available {
  let previous_nodes=result.nodes.len();let previous_edges=result.edges.len();
  if children {
   let n=node_for(db,partition,scope,context,summary,&child_rows[i].resource,ns,secret,work)?;
   if request.relations.contains(&RelationKind::Containment){result.edges.push(MapEdge{id:opaque("vc2",&child_rows[i].resource,ns,secret),from:result.root.clone().unwrap(),to:n.id.clone(),relation:RelationKind::Containment,state:RelationState::Structure,count:1,selection_id:None});}include_chain(db,partition,scope,context,summary,n,ns,secret,work,&mut result.nodes)?;
  }else{
   let r=&refs[i];let relation=relation(r.kind)?;
   if !request.relations.contains(&relation){consumed=i+1;continue;}
   let caller=r.caller.as_ref().map(|s|format!("decl:{s}")).unwrap_or_else(||r.file.clone());
   let from=node_for(db,partition,scope,context,summary,&caller,ns,secret,work)?;let to=node_for(db,partition,scope,context,summary,&format!("decl:{}",r.target),ns,secret,work)?;
   for n in [&from,&to]{include_chain(db,partition,scope,context,summary,n.clone(),ns,secret,work,&mut result.nodes)?;}
   result.edges.push(MapEdge{id:opaque("vr2",&format!("occ:{}",r.occurrence),ns,secret),from:from.id,to:to.id,relation,state:if r.state=="exact"{RelationState::Exact}else{RelationState::Inferred},count:1,selection_id:Some(opaque("vw2",&format!("occ:{}",r.occurrence),ns,secret))});
  }
  if result.nodes.len()>request.limits.nodes||result.nodes.iter().filter(|n|matches!(n.kind,NodeKind::Repository|NodeKind::Folder)).count()>request.limits.groups||result.edges.len()>request.limits.edges||serde_json::to_vec(&result).map_err(bad)?.len()+1024>request.limits.bytes{result.nodes.truncate(previous_nodes);result.edges.truncate(previous_edges);if consumed==0{return Err(fail("budget_exceeded"));}break;}
  consumed=i+1;
 }
 result.page.complete=consumed==available&&available<if children{request.limits.nodes+1}else{request.limits.edges+1};
 result.page.returned_nodes=result.nodes.len();result.page.returned_edges=result.edges.len();
 if result.page.complete{result.page.remaining_nodes=Some(0);result.page.remaining_edges=Some(0);}else{result.page.next_cursor=Some(token("sc2",&[offset+consumed],&page_ns,secret));}
 Ok(result)
}

pub(super) fn witness(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,resource:&str,work:&mut Work)->Result<MapOccurrenceRef,QueryError>{
 let raw=resource.strip_prefix("occ:").ok_or_else(||fail("invalid"))?;
 let row=read_refs(db,partition,scope,context,"occurrence=?4",raw,0,1,work)?.pop().ok_or_else(||fail("missing"))?;
 Ok(MapOccurrenceRef{occurrence_id:resource.into(),caller_id:row.caller.map(|s|format!("decl:{s}")),target_id:format!("decl:{}",row.target),relation:relation(row.kind)?,state:if row.state=="exact"{RelationState::Exact}else{RelationState::Inferred}})
}
