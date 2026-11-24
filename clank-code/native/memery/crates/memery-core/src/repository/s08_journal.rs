//! BQ-8 journal delta: the closed `journal_delta` selection carried by the S08 model-prepare request.
//!
//! The wire is frozen in `.clanker/robonotes/claude-last-lap-2026-11-09/codex-conformance/BQ-8-journal-delta-contract.md`
//! ("Wire (frozen)"). This module owns only the TYPES and the pure checks: shape bounds, the page digest and the selection
//! identity recomputed from the rows, never trusted from the caller. Gap/omitted classification is a Host/archive matter;
//! Native binds the page it is given and refuses a digest that does not match.
use super::*;

pub(crate) const OUTPUT_KIND:&str="journal_synthesis";
pub(crate) const CONSUMER:&str="journal_delta.v1";
const MAX_ROWS:usize=32;
/// Evidence locator of a journal row. Deliberately NOT `s08_admitted_source`: coverage of an archive part is not an S06 admission
/// ("No fabricated helped events"; decision 11), so no reader that counts admissions can ever see a journal row.
pub(crate) const LOCATOR_TYPE:&str="s08_journal_source";
pub(crate) const ACTOR:&str="s08_journal";
pub(crate) const REASON:&str="unreviewed inferred journal synthesis";

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JournalRowRef {
    pub seq:i64, pub actor_id:String, pub message_id:String, pub part_id:String, pub revision:i64, pub content_hash:Option<String>,
    pub workspace_id:Option<String>, pub project_id:Option<String>, pub role:String, pub part_type:String, pub class:String, pub reason:String, pub selector:Value,
    #[serde(default,skip_serializing_if="Option::is_none")] pub source:Option<HostArchiveSourceRef>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub authority:Option<ObservationOriginAuthority>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct S08JournalDeltaSelection {
    pub version:u32, pub chat_id:String, pub prior_cursor:i64, pub high_seq:i64, pub end_seq:i64,
    pub page_sha256:String, pub selection_sha256:String, pub rows:Vec<JournalRowRef>,
}

fn canonical(value:&Value)->Result<String,RepositoryError> {
    // Same recipe as the Host and `journal_delta` digests: sorted keys, compact, UTF-8 not escaped.
    fn sorted(v:&Value)->Value {
        match v {
            Value::Object(o)=>{let mut keys:Vec<&String>=o.keys().collect();keys.sort();let mut out=serde_json::Map::new();for k in keys {out.insert(k.clone(),sorted(&o[k]));}Value::Object(out)}
            Value::Array(a)=>Value::Array(a.iter().map(sorted).collect()),
            other=>other.clone(),
        }
    }
    Ok(serde_json::to_string(&sorted(value))?)
}

fn classes_ok(row:&JournalRowRef)->bool {
    let carries_source=matches!(row.class.as_str(),"included"|"already_covered");
    matches!(row.class.as_str(),"included"|"superseded"|"gap"|"omitted"|"already_covered")
        && row.source.is_some()==carries_source && row.authority.is_some()==carries_source
        && !row.reason.is_empty() && row.reason.len()<=64 && row.reason.bytes().all(|b|b.is_ascii_lowercase()||b==b'_')
        && row.selector.is_object()
}

impl S08JournalDeltaSelection {
    /// The page object whose canonical JSON is hashed (rows without their source proofs).
    fn page_value(&self,account:&str,workspace:&str)->Value {
        let rows:Vec<Value>=self.rows.iter().map(|r|json!({"seq":r.seq,"actor_id":r.actor_id,"message_id":r.message_id,"part_id":r.part_id,"revision":r.revision,"content_hash":r.content_hash,"workspace_id":r.workspace_id,"project_id":r.project_id,"role":r.role,"part_type":r.part_type,"class":r.class,"reason":r.reason,"selector":r.selector})).collect();
        json!({"version":1,"kind":"journal_delta_page","consumer":CONSUMER,"account_id":account,"chat_id":self.chat_id,"workspace_id":workspace,"ack_seq":self.prior_cursor,"high_seq":self.high_seq,"end_seq":self.end_seq,"rows":rows})
    }
    pub(crate) fn page_digest(&self,account:&str,workspace:&str)->Result<String,RepositoryError> {Ok(sha256_hex(canonical(&self.page_value(account,workspace))?.as_bytes()))}
    pub(crate) fn selection_digest(&self,account:&str,workspace:&str,policy_fingerprint:&str,authority_fingerprint:&str)->Result<String,RepositoryError> {
        Ok(sha256_hex(canonical(&json!({"version":1,"kind":"journal_delta_selection","account_id":account,"chat_id":self.chat_id,"workspace_id":workspace,"consumer":CONSUMER,"prior_cursor":self.prior_cursor,"high_seq":self.high_seq,"end_seq":self.end_seq,"page_sha256":self.page_sha256,"policy_fingerprint":policy_fingerprint,"authority_fingerprint":authority_fingerprint}))?.as_bytes()))
    }
    /// Bounds, row order, class/proof pairing, and both digests recomputed. `Ok` means the manifest is exactly what the Host described.
    pub(crate) fn validate(&self,selection:&S08RulesSelection)->Result<(),RepositoryError> {
        let bad=||RepositoryError::Validation("S08 journal selection unavailable".into());
        if selection.importance_target.is_some() || self.version!=1 || self.chat_id.is_empty() || self.chat_id.trim()!=self.chat_id || self.chat_id.len()>512
            || self.prior_cursor<0 || self.end_seq<=self.prior_cursor || self.end_seq>self.high_seq
            || !(1..=MAX_ROWS).contains(&self.rows.len()) || !valid_sha256(&self.page_sha256) || !valid_sha256(&self.selection_sha256)
            || !selection.candidates.is_empty() || !selection.admissions.is_empty() || selection.project_id.is_some() {return Err(bad())}
        let mut last=self.prior_cursor;
        for row in &self.rows {
            if row.seq<=last || row.seq>self.end_seq || !classes_ok(row) || row.revision<1
                || [&row.actor_id,&row.message_id,&row.part_id,&row.role,&row.part_type].iter().any(|v|v.is_empty()||v.trim()!=v.as_str()||v.len()>512)
                || row.content_hash.as_deref().is_some_and(|h|!valid_sha256(h)) {return Err(bad())}
            if let (Some(source),true)=(&row.source,matches!(row.class.as_str(),"included"|"already_covered")) {
                if source.account_id!=selection.source_account_id || source.chat_id!=self.chat_id || source.message_id!=row.message_id || source.revision!=row.revision {return Err(bad())}
            }
            last=row.seq;
        }
        if self.rows.last().map(|r|r.seq)!=Some(self.end_seq) {return Err(bad())}
        // Every job keeps at least one evidence row, because lifecycle readers find the captured owner through one.
        if !self.rows.iter().any(|r|r.source.is_some()) {return Err(bad())}
        if self.page_digest(&selection.source_account_id,&selection.workspace_id)?!=self.page_sha256 {return Err(bad())}
        // The authority fingerprint is the selection's `target_fingerprint` (the existing capture/source authority commitment).
        if self.selection_digest(&selection.source_account_id,&selection.workspace_id,&selection.policy_fingerprint,&selection.target_fingerprint)?!=self.selection_sha256
            || selection.idempotency_key!=format!("s08_{}",self.selection_sha256) {return Err(bad())}
        Ok(())
    }
    /// Prepare-time closed shape of the model request around a journal selection (never reached by summary/connection with no journal):
    /// the journal rides inside `selection`, the kind and the journal come together, model/hybrid only, importance unset, and the
    /// snapshot commitment IS the selection digest (there is no block/graph snapshot to commit to).
    pub(crate) fn prepare_ok(model:&S08ModelPrepareRequest)->bool {
        match (&model.selection.journal,model.output_kind==OUTPUT_KIND) {
            (None,false)=>true,
            (Some(j),true)=>matches!(model.mode.as_str(),"model"|"hybrid") && model.connection_predicate.is_none()
                && matches!(&model.importance,S08ImportanceDecision::Skipped{reason} if reason=="unset")
                && model.snapshots_sha256==j.selection_sha256 && j.validate(&model.selection).is_ok(),
            _=>false,
        }
    }
    /// Custody of every archive source a journal selection cites (the same proofs an admission carries), under the verified view.
    pub(crate) fn custody(tx:&Transaction<'_>,selection:&S08RulesSelection,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
        let journal=selection.journal.as_ref().ok_or(RepositoryError::NotFound)?;
        journal.validate(selection)?;view.for_captured(tx,&selection.owner)?;
        let owner=&selection.owner;
        for row in journal.sourced() {
            let (source,original)=match (&row.source,&row.authority) {(Some(s),Some(a))=>(s,a),_=>return Err(RepositoryError::NotFound)};
            source.validate_in(tx)?;if source.account_id!=selection.source_account_id {return Err(RepositoryError::NotFound)}
            let mut authority=original.clone();
            let head=observation_rebind_head_custody(tx,owner,owner,&authority.origin_id,&authority.origin_hash,&authority.captured_owner,&selection.workspace_id,source,view,budget)?;
            match head {
                Some((id,hash))=>{authority.current_fingerprint=read_observation_rebind_custody(tx,owner,&id,&hash,view,budget)?.current_fingerprint;authority.rebind_id=Some(id);authority.rebind_hash=Some(hash);},
                None=>{authority.rebind_id=None;authority.rebind_hash=None;}
            }
            validate_observation_origin_custody(tx,owner,owner,&selection.workspace_id,source,&authority,view,budget)?;
        }
        Ok(())
    }
    /// The pending candidate's stored proposal: the cited `included` rows (what the text was synthesized from), never the page body.
    pub(crate) fn proposal(&self,content:&str)->Value {
        let rows:Vec<Value>=self.rows.iter().filter(|r|r.class=="included").map(|r|json!({"seq":r.seq,"message_id":r.message_id,"part_id":r.part_id,"revision":r.revision,"content_hash":r.content_hash})).collect();
        json!({"version":"s08.policy-journal.v1","classification":"inferred_untrusted","content":content,"journal":{"selection_sha256":self.selection_sha256,"page_sha256":self.page_sha256,"chat_id":self.chat_id,"rows":rows}})
    }
    /// Exact closure of a stored proposal: it must equal the one this selection yields for its own content.
    pub(crate) fn proposal_ok(&self,stored:&Value)->bool {
        stored.get("content").and_then(Value::as_str).is_some_and(|c|!c.trim().is_empty() && c.trim()==c && c.len()<=4096 && *stored==self.proposal(c))
    }
    /// Stable id of one row (also its manifest id and the `row_id` of its evidence locator).
    pub(crate) fn row_id(row:&JournalRowRef)->Result<String,RepositoryError> {Ok(format!("s08journalrow_{}",sha256_hex(canonical(&serde_json::to_value(row)?)?.as_bytes())))}
    /// Manifest ids of a journal selection (the S08 `selected_json` of the job): one stable id per row.
    pub(crate) fn selected_ids(&self)->Result<BTreeSet<String>,RepositoryError> {
        let mut out=BTreeSet::new();
        for row in &self.rows {out.insert(Self::row_id(row)?);}
        Ok(out)
    }
    /// Rows that cite an archive part (`included` / `already_covered`): the only rows with an evidence row.
    pub(crate) fn sourced(&self)->impl Iterator<Item=&JournalRowRef> {self.rows.iter().filter(|r|r.source.is_some())}
    pub(crate) fn evidence_id(owner:&str,job:&str,row_id:&str)->Result<String,RepositoryError> {Ok(format!("s08journalevidence_{}",sha256_hex(&serde_json::to_vec(&(owner,job,row_id))?)))}
    /// The exact locator of a row's evidence: six keys, bound to the job, the selection digest and the selection owner.
    pub(crate) fn locator(&self,owner:&str,job:&str,row_id:&str,input_hash:&str)->Value {
        json!({"kind":LOCATOR_TYPE,"job_id":job,"row_id":row_id,"input_hash":input_hash,"selection_owner":owner,"selection_sha256":self.selection_sha256})
    }
    /// Sorted evidence ids the job must own, exactly.
    pub(crate) fn expected_evidence_ids(&self,owner:&str,job:&str)->Result<Vec<String>,RepositoryError> {
        let mut out=Vec::new();
        for row in self.sourced() {out.push(Self::evidence_id(owner,job,&Self::row_id(row)?)?);}
        out.sort();Ok(out)
    }
    /// Closure check of one stored evidence row: exact key set, exact id, exact job/digest binding, and the row it names must be
    /// a sourced row of this selection whose archive source is `source_id`/`revision`. Returns the row.
    pub(crate) fn verify_evidence(&self,evidence_id:&str,source_id:&str,revision:i64,locator:&Value,owner:&str,job:&str,input_hash:&str)->Result<&JournalRowRef,RepositoryError> {
        let bad=||RepositoryError::NotFound;
        let obj=locator.as_object().ok_or_else(bad)?;
        let row_id=obj.get("row_id").and_then(Value::as_str).ok_or_else(bad)?;
        if obj.len()!=6 || *locator!=self.locator(owner,job,row_id,input_hash) || evidence_id!=Self::evidence_id(owner,job,row_id)? {return Err(bad())}
        for row in self.sourced() {
            if Self::row_id(row)?==row_id {
                let source=row.source.as_ref().ok_or_else(bad)?;
                if host_archive_source_id(source)?!=source_id || source.revision!=revision {return Err(bad())}
                return Ok(row);
            }
        }
        Err(bad())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    fn selection(rows:Vec<JournalRowRef>)->(S08RulesSelection,S08JournalDeltaSelection) {
        let mut journal=S08JournalDeltaSelection{version:1,chat_id:"chat-1".into(),prior_cursor:0,high_seq:9,end_seq:rows.last().map(|r|r.seq).unwrap_or(1),page_sha256:"0".repeat(64),selection_sha256:"0".repeat(64),rows};
        let mut sel=S08RulesSelection{version:1,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,source_account_id:"acct".into(),idempotency_key:String::new(),target_fingerprint:"a".repeat(64),policy_fingerprint:"b".repeat(64),owner_queue_limit:4,global_queue_limit:16,candidates:vec![],admissions:vec![],journal:None,importance_target:None};
        journal.page_sha256=journal.page_digest("acct","ws").unwrap();
        journal.selection_sha256=journal.selection_digest("acct","ws",&sel.policy_fingerprint,&sel.target_fingerprint).unwrap();
        sel.idempotency_key=format!("s08_{}",journal.selection_sha256);
        (sel,journal)
    }
    fn included(seq:i64)->JournalRowRef {
        let mut r=row(seq,"included","covered_by_review");
        r.source=Some(HostArchiveSourceRef{version:1,kind:"host_user_body".into(),account_id:"acct".into(),chat_id:"chat-1".into(),actor_id:"main".into(),message_id:format!("m{seq}"),part_id:"body".into(),revision:1,content_hash:"c".repeat(64),workspace_id:Some("ws".into()),project_id:None});
        r.authority=Some(ObservationOriginAuthority{origin_id:format!("origin{seq}"),origin_hash:"d".repeat(64),captured_owner:"alice".into(),current_fingerprint:"e".repeat(64),rebind_id:None,rebind_hash:None});
        r
    }
    fn row(seq:i64,class:&str,reason:&str)->JournalRowRef {
        JournalRowRef{seq,actor_id:"main".into(),message_id:format!("m{seq}"),part_id:"body".into(),revision:1,content_hash:Some("c".repeat(64)),workspace_id:None,project_id:None,role:"assistant".into(),part_type:"tool".into(),class:class.into(),reason:reason.into(),selector:json!({"seq":seq}),source:None,authority:None}
    }

    #[test]
    fn a_consistent_page_validates_and_any_tamper_is_refused() {
        let (sel,journal)=selection(vec![included(2),row(3,"omitted","tool_or_media_part"),row(4,"gap","source_missing"),row(5,"superseded","tombstone")]);
        journal.validate(&sel).unwrap();
        let mut a=journal.clone();a.rows[2].reason="source_denied".into();assert!(a.validate(&sel).is_err(),"a changed row no longer matches the page digest");
        let mut b=journal.clone();b.high_seq=1;assert!(b.validate(&sel).is_err(),"end beyond high");
        let mut c=journal.clone();c.rows.reverse();assert!(c.validate(&sel).is_err(),"rows must ascend");
        let mut d=sel.clone();d.idempotency_key=format!("s08_{}","d".repeat(64));assert!(journal.validate(&d).is_err(),"the key is the selection identity");
        let mut e=sel.clone();e.policy_fingerprint="e".repeat(64);assert!(journal.validate(&e).is_err(),"the policy revision is bound");
        let mut f=sel.clone();f.target_fingerprint="f".repeat(64);assert!(journal.validate(&f).is_err(),"the capture/source authority is bound");
        let mut g=journal.clone();g.rows[1].class="included".into();assert!(g.validate(&sel).is_err(),"an included row needs its source proof");
        let mut h=journal.clone();h.rows.clear();assert!(h.validate(&sel).is_err());
        let (sel2,none)=selection(vec![row(3,"omitted","tool_or_media_part")]);assert!(none.validate(&sel2).is_err(),"a job must keep one evidence row");
        let mut i=sel.clone();i.candidates=vec![];i.admissions=vec![];i.project_id=Some("p".into());assert!(journal.validate(&i).is_err());
    }

    #[test]
    fn the_digest_is_independent_of_key_order_and_binds_account_chat_and_workspace() {
        let (sel,journal)=selection(vec![included(2),row(3,"omitted","tool_or_media_part")]);
        let page=journal.page_digest("acct","ws").unwrap();
        assert_eq!(page,journal.page_sha256);
        assert_ne!(page,journal.page_digest("other","ws").unwrap());
        assert_ne!(page,journal.page_digest("acct","ws2").unwrap());
        let mut moved=journal.clone();moved.chat_id="chat-2".into();assert_ne!(page,moved.page_digest("acct","ws").unwrap());
        assert!(journal.selected_ids().unwrap().iter().all(|id|id.starts_with("s08journalrow_")));
        journal.validate(&sel).unwrap();
    }

    // ---- Group 2: lifecycle / custody classifiers fed a hand-built journal manifest + evidence row set ----
    pub(crate) const OWNER:&str="alice";

    pub(crate) fn db()->Connection {
        let c=Connection::open_in_memory().unwrap();
        for sql in [include_str!("../store/current-schema.sql"),include_str!("../store/schema-v17-additions.sql"),include_str!("../store/schema-v17-authority.sql"),include_str!("../store/schema-v17-s05-additions.sql")] {c.execute_batch(sql).unwrap();}
        c.execute_batch("CREATE INDEX s08_job_evidence ON fm_v2_evidence(owner_id,json_extract(locator_json,'$.job_id'),evidence_id) WHERE locator_type='s08_admitted_source' AND json_valid(locator_json);
            CREATE INDEX s08_source_evidence ON fm_v2_evidence(owner_id,source_id,evidence_id) WHERE locator_type='s08_admitted_source';
            CREATE INDEX s08_consolidation_jobs ON fm_v2_jobs(owner_id,workspace_key,created_at,job_id) WHERE kind='access_consolidation';
            INSERT OR REPLACE INTO fm_meta(key,value) VALUES('database_id','db-1');INSERT OR REPLACE INTO fm_meta(key,value) VALUES('openclank.s08.model.v1','1');").unwrap();
        // The writer fence and epoch the real guarded store provides (same fixture shape the repository's own host-review test uses).
        c.create_scalar_function("fm_writer_capability",0,rusqlite::functions::FunctionFlags::SQLITE_UTF8|rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC|rusqlite::functions::FunctionFlags::SQLITE_INNOCUOUS,|_|Ok("openclank.frankenmemory.writer.v2")).unwrap();
        c.execute_batch("PRAGMA user_version=17; PRAGMA trusted_schema=ON;").unwrap();
        // The S05 insert/transition guards belong to the live scheduler; hand-built settled rows need them out of the way (test DB only).
        let triggers:Vec<String>=c.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('fm_v2_jobs','fm_v2_attempts','fm_v2_s05_reservations')").unwrap().query_map([],|r|r.get(0)).unwrap().collect::<Result<_,_>>().unwrap();
        for name in triggers {c.execute_batch(&format!("DROP TRIGGER {name}")).unwrap();}
        c
    }
    fn job_of(sel:&S08RulesSelection)->String {format!("s08job_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-job.v1",&sel.owner,&sel.idempotency_key,OUTPUT_KIND)).unwrap()))}
    /// The rows a prepared (not yet dispatched) journal job owns: job, selection receipt + outbox, manifest, sources, evidence.
    fn refs()->PolicyPublicationRefs {let r=|c:&str|PolicyProjectionRef{epoch:1,fingerprint:c.repeat(64),transition_id:c.repeat(64)};PolicyPublicationRefs{account:r("a"),global:r("b")}}
    pub(crate) fn prepare(sel:&S08RulesSelection,j:&S08JournalDeltaSelection)->S08ModelPrepareRequest {
        S08ModelPrepareRequest{version:1,selection:sel.clone(),mode:"model".into(),output_kind:OUTPUT_KIND.into(),connection_predicate:None,importance:S08ImportanceDecision::Skipped{reason:"unset".into()},
            importance_target:None,snapshots_sha256:j.selection_sha256.clone(),assignment_sha256:"4".repeat(64),connection_id:"conn-1".into(),model_route_id:"route-1".into(),max_input_tokens:1000,max_output_tokens:512,daily_calls:4,daily_tokens:100000,policy_projection:refs(),cadence_policy:None}
    }
    pub(crate) fn seed(c:&Connection,sel:&S08RulesSelection,j:&S08JournalDeltaSelection,state:&str)->(String,String) {
        let m=prepare(sel,j);let tool=sha256_hex(format!("s08-policy-{}-{}-v1",m.mode,m.output_kind).as_bytes());
        c.execute("INSERT OR REPLACE INTO fm_meta(key,value) VALUES('database_id','db-1')",[]).unwrap();
        let raw=serde_json::to_string(sel).unwrap();let input=sha256_hex(raw.as_bytes());let job=job_of(sel);
        let change=format!("cs_s08selection_{}",sha256_hex(&serde_json::to_vec(&(&sel.owner,&job)).unwrap()));
        c.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,input_hash,created_at,updated_at) VALUES(?,?,'access_consolidation',?,'',?,'queued',?,'t','t')",params![sel.owner,job,sel.workspace_id,sel.idempotency_key,input]).unwrap();
        if state!="queued" {c.execute("UPDATE fm_v2_jobs SET state=? WHERE owner_id=? AND job_id=?",params![state,sel.owner,job]).unwrap();}
        c.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?,?,'automation','s08_rules','s08.selection.v1',NULL,?,?,'t',?)",params![sel.owner,change,CONTRACT_VERSION,input,raw]).unwrap();
        c.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?,?,?,'s08_selection',?,1,'t')",params![sel.owner,format!("outbox_{change}_s08_selection"),change,serde_json::to_string(&serde_json::from_str::<Value>(&raw).unwrap()).unwrap()]).unwrap();
        c.execute("INSERT INTO fm_v2_job_manifests(owner_id,manifest_id,job_id,phase,selected_json,config_hash,model_hash,tool_hash,input_hash,content_hash,sealed_at) VALUES(?,?,?,'selection',?,?,?,?,?,?,'t')",params![sel.owner,format!("m_{job}"),job,serde_json::to_string(&j.selected_ids().unwrap()).unwrap(),sel.policy_fingerprint,m.assignment_sha256,tool,input,input]).unwrap();
        let intent=serde_json::to_string(&m).unwrap();let model_id=format!("cs_s08model_{}",sha256_hex(&serde_json::to_vec(&(&sel.source_account_id,&job)).unwrap()));
        c.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','s08_model','s08.model-intent.v1',NULL,?3,?4,'t',?5)",params![sel.owner,model_id,CONTRACT_VERSION,sha256_hex(intent.as_bytes()),intent]).unwrap();
        c.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s08_model_intent',?4,0,'t')",params![sel.owner,format!("outbox_{model_id}_s08_model_intent"),model_id,intent]).unwrap();
        for row in j.sourced() {
            let source=row.source.as_ref().unwrap();let sid=host_archive_source_id(source).unwrap();
            c.execute("INSERT OR IGNORE INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,forget_state,created_at) VALUES(?,?,?,'',?,?,?,'host_archive_part.v1','active','t')",params![sel.owner,sid,sel.workspace_id,format!("host-archive://{sid}"),source.revision,source.content_hash]).unwrap();
            let rid=S08JournalDeltaSelection::row_id(row).unwrap();
            c.execute("INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES(?,?,?,?,?,?,NULL,'s08-selection-v1','open-clank','null','null','t')",params![sel.owner,S08JournalDeltaSelection::evidence_id(&sel.owner,&job,&rid).unwrap(),sid,source.revision,LOCATOR_TYPE,serde_json::to_string(&j.locator(&sel.owner,&job,&rid,&input)).unwrap()]).unwrap();
        }
        (job,input)
    }
    fn count(c:&Connection,sql:&str)->i64 {c.query_row(sql,[],|r|r.get(0)).unwrap()}
    pub(crate) fn fixture()->(Connection,S08RulesSelection,S08JournalDeltaSelection) {
        let (mut sel,j)=selection(vec![included(2),included(3),row(4,"omitted","tool_or_media_part"),row(5,"gap","source_missing")]);
        sel.journal=Some(j.clone());
        let c=db();(c,sel,j)
    }

    #[test]
    fn evidence_closure_is_exact() {
        let (c,sel,j)=fixture();let (job,input)=seed(&c,&sel,&j,"queued");
        V2Repository::verify_s08_evidence(&c,OWNER,&sel,&job,&input).unwrap();
        // The locator type is distinct: no admission reader can see the rows.
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE locator_type='s08_admitted_source'"),0);
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE locator_type='s08_journal_source'"),2);
        let first:String=c.query_row("SELECT evidence_id FROM fm_v2_evidence ORDER BY evidence_id LIMIT 1",[],|r|r.get(0)).unwrap();
        // A missing row, a stray admission row, a re-bound digest, and a moved source revision are each refused.
        c.execute("DELETE FROM fm_v2_evidence WHERE evidence_id=?",[&first]).unwrap();
        assert!(V2Repository::verify_s08_evidence(&c,OWNER,&sel,&job,&input).is_err());
        let (c,sel,j)=fixture();let (job,input)=seed(&c,&sel,&j,"queued");
        c.execute("UPDATE fm_v2_evidence SET locator_json=replace(locator_json,?,?)",[&j.selection_sha256,&"9".repeat(64)]).unwrap();
        assert!(V2Repository::verify_s08_evidence(&c,OWNER,&sel,&job,&input).is_err());
        let (c,sel,j)=fixture();let (job,input)=seed(&c,&sel,&j,"queued");
        c.execute("INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) SELECT owner_id,'s08evidence_x',source_id,source_revision,'s08_admitted_source',locator_json,NULL,'s08-selection-v1','open-clank','null','null','t' FROM fm_v2_evidence LIMIT 1",[]).unwrap();
        assert!(V2Repository::verify_s08_evidence(&c,OWNER,&sel,&job,&input).is_err(),"a journal job never owns an admission row");
    }

    #[test]
    fn the_source_walk_sees_journal_rows_by_source() {
        let (c,sel,j)=fixture();let (job,_)=seed(&c,&sel,&j,"queued");
        let source=host_archive_source_id(j.rows[0].source.as_ref().unwrap()).unwrap();
        let page=V2Repository::s08_source_page(&c,OWNER,&source,"").unwrap();
        assert_eq!(page.len(),1);
        let locator:Value=serde_json::from_str(&page[0].1).unwrap();
        assert_eq!(locator["job_id"],job.as_str());assert_eq!(locator["kind"],LOCATOR_TYPE);
        assert!(V2Repository::s08_source_page(&c,OWNER,&source,&page[0].0).unwrap().is_empty());
    }

    #[test]
    fn owner_erase_and_reset_remove_journal_rows_and_leave_other_kinds() {
        let (mut c,sel,j)=fixture();let (job,_)=seed(&c,&sel,&j,"queued");
        // An unrelated summary-style job: its admission evidence and job row must survive byte for byte.
        let other="s08job_other";
        c.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,input_hash,created_at,updated_at) VALUES(?,?,'access_consolidation','ws','','s08_other','queued',?,'t','t')",params![OWNER,other,"7".repeat(64)]).unwrap();
        let src=host_archive_source_id(j.rows[0].source.as_ref().unwrap()).unwrap();
        c.execute("INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES(?,'s08evidence_other',?,1,'s08_admitted_source',?,NULL,'s08-selection-v1','open-clank','null','null','t')",params![OWNER,src,json!({"kind":"s08_admitted_source","job_id":other,"admission_id":"a","input_hash":"7".repeat(64),"selection_owner":OWNER}).to_string()]).unwrap();
        let excluded:std::collections::HashSet<String>=[other.to_owned()].into_iter().collect();
        let tx=c.transaction().unwrap();
        let mut keep=|_:&str,_:&str|->Result<bool,RepositoryError>{Ok(true)};
        V2Repository::erase_s08_owner_selections_inner(&tx,OWNER,Some(&mut keep),Some(&excluded)).unwrap();
        tx.commit().unwrap();
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE locator_type='s08_journal_source'"),0);
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_change_sets WHERE reason='s08.selection.v1'"),0);
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_outbox WHERE kind='s08_selection'"),0);
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_job_manifests"),0);
        assert_eq!(c.query_row("SELECT state FROM fm_v2_jobs WHERE job_id=?",[&job],|r|r.get::<_,String>(0)).unwrap(),"cancelled");
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE evidence_id='s08evidence_other' AND locator_type='s08_admitted_source'"),1);
        assert_eq!(c.query_row("SELECT state FROM fm_v2_jobs WHERE job_id='s08job_other'",[],|r|r.get::<_,String>(0)).unwrap(),"queued");
    }

    #[test]
    fn the_terminal_validator_finds_a_prepared_cancelled_journal_job() {
        let (c,sel,j)=fixture();let (job,_)=seed(&c,&sel,&j,"cancelled");
        let mut budget=|_:usize|->Result<(),String>{Ok(())};
        let mut seen=Vec::new();
        {let mut policy=|workspace:&str,job:&str,attempt:Option<&str>|->Result<(),RepositoryError>{seen.push((workspace.to_owned(),job.to_owned(),attempt.map(str::to_owned)));Ok(())};
        validate_s08_owner_terminal_impl(&c,OWNER,Some("acct"),&mut budget,None,Some(&mut policy)).unwrap();}
        assert_eq!(seen,vec![("ws".to_owned(),job.clone(),None)]);
        // Losing the evidence (an orphaned receipt) or the journal selection's closed shape is refused, not skipped.
        c.execute("DELETE FROM fm_v2_evidence WHERE locator_type='s08_journal_source'",[]).unwrap();
        let mut policy=|_:&str,_:&str,_:Option<&str>|->Result<(),RepositoryError>{Ok(())};
        assert!(validate_s08_owner_terminal_impl(&c,OWNER,Some("acct"),&mut budget,None,Some(&mut policy)).is_err());
    }

    #[test]
    fn a_stored_proposal_must_be_exactly_the_one_the_selection_yields() {
        let (_,j)=selection(vec![included(2),row(3,"omitted","tool_or_media_part")]);
        let good=j.proposal("The user decided to ship on Friday.");
        assert!(j.proposal_ok(&good));
        let mut edited=good.clone();edited["journal"]["rows"][0]["revision"]=json!(2);assert!(!j.proposal_ok(&edited));
        let mut extra=good.clone();extra["importance"]=json!({"state":"skipped"});assert!(!j.proposal_ok(&extra));
        let mut empty=good.clone();empty["content"]=json!(" ");assert!(!j.proposal_ok(&empty));
        let (_,other)=selection(vec![included(2),row(4,"omitted","tool_or_media_part")]);assert!(!other.proposal_ok(&good),"bound to its own page digest");
    }

    // ---- Group 3: prepare shape, committed outcomes, status ----
    #[test]
    fn cadence_rechecks_retained_usage_after_an_empty_observation() {
        let (mut c,sel,j)=fixture();
        let old=prepare(&sel,&j);
        assert!(serde_json::to_value(&old).unwrap().get("cadence_policy").is_none(),"legacy original bytes omit the optional field");
        let raw=serde_json::to_string(&json!({"can_manage_memory":true,"preferences":{
            "memory_policy":[false,null],"memory_policy_consolidation_enabled":[true,true],
            "memory_policy_consolidation_interval_days":[true,1]}})).unwrap();
        let mut current=old.clone();current.policy_projection.account.fingerprint=sha256_hex(raw.as_bytes());
        let policy=S08CadencePolicy{version:1,accepted_policy_input_json:raw};
        {let tx=c.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();let mut budget=ResetFenceBudget::install(&tx);
         assert!(s08_cadence::enforce(&tx,&current,&policy,&mut budget).is_ok(),"empty retained ledger can initialize");}
        c.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        c.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,created_at,updated_at) VALUES(?1,'orphan','s08job_orphan',1,'cancelled',1,'t','t')",params![OWNER]).unwrap();
        {let tx=c.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();let mut budget=ResetFenceBudget::install(&tx);
         assert!(s08_cadence::enforce(&tx,&current,&policy,&mut budget).is_err(),"orphan S08 attempt is unknown");}
        c.execute("DELETE FROM fm_v2_attempts WHERE owner_id=?1 AND attempt_id='orphan'",params![OWNER]).unwrap();
        c.execute_batch("PRAGMA foreign_keys=ON").unwrap();
        let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&old,&j,&job,None);
        let now=chrono::Utc::now().timestamp_millis();
        c.execute_batch("PRAGMA ignore_check_constraints=ON").unwrap(); // old fixture schema predates S08 zero-charge reservations
        c.execute("UPDATE fm_v2_s05_reservations SET state='reserved',charged_calls=0,settled_at_ms=NULL WHERE owner_id=?1 AND job_id=?2",params![OWNER,job]).unwrap();
        {let tx=c.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();let mut budget=ResetFenceBudget::install(&tx);
         assert!(s08_cadence::enforce(&tx,&current,&policy,&mut budget).is_err(),"an uncharged in-flight journal cannot bypass cadence");}
        c.execute("UPDATE fm_v2_s05_reservations SET state='settled',charged_calls=1,reserved_at_ms=?1,settled_at_ms=?1 WHERE owner_id=?2 AND job_id=?3",params![now,OWNER,job]).unwrap();
        c.execute_batch("PRAGMA ignore_check_constraints=OFF").unwrap();
        {let tx=c.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();let mut budget=ResetFenceBudget::install(&tx);
         assert!(s08_cadence::enforce(&tx,&current,&policy,&mut budget).is_err(),"a settlement after the empty observation blocks the next reservation");}
    }

    #[test]
    fn the_prepare_request_has_one_closed_journal_shape() {
        let (sel,j)=selection(vec![included(2),row(3,"omitted","tool_or_media_part")]);let mut sel=sel;sel.journal=Some(j.clone());
        let ok=prepare(&sel,&j);assert!(S08JournalDeltaSelection::prepare_ok(&ok));
        let mut rules=ok.clone();rules.mode="rules".into();assert!(!S08JournalDeltaSelection::prepare_ok(&rules),"journal is model/hybrid only");
        let mut imp=ok.clone();imp.importance=S08ImportanceDecision::Skipped{reason:"disabled".into()};assert!(!S08JournalDeltaSelection::prepare_ok(&imp),"importance must be unset");
        let mut snap=ok.clone();snap.snapshots_sha256="9".repeat(64);assert!(!S08JournalDeltaSelection::prepare_ok(&snap),"the commitment is the selection digest");
        let mut kind=ok.clone();kind.output_kind="summary".into();assert!(!S08JournalDeltaSelection::prepare_ok(&kind),"a journal only rides a journal kind");
        let mut bare=ok.clone();bare.selection.journal=None;assert!(!S08JournalDeltaSelection::prepare_ok(&bare),"the journal kind needs its journal");
        let mut page=ok.clone();page.selection.journal.as_mut().unwrap().page_sha256="8".repeat(64);assert!(!S08JournalDeltaSelection::prepare_ok(&page),"a changed page digest is refused");
        let mut row3=ok.clone();row3.selection.journal.as_mut().unwrap().rows[1].reason="source_denied".into();assert!(!S08JournalDeltaSelection::prepare_ok(&row3),"a changed row is refused");
        let mut pred=ok.clone();pred.connection_predicate=Some(S08ConnectionPredicate{predicate:"p".into(),version:1,sha256:"a".repeat(64)});assert!(!S08JournalDeltaSelection::prepare_ok(&pred));
        // The two existing kinds are untouched: no journal, kind summary/connection.
        let mut summary=ok.clone();summary.selection.journal=None;summary.output_kind="summary".into();assert!(S08JournalDeltaSelection::prepare_ok(&summary));
        assert!(!serde_json::to_string(&summary.selection).unwrap().contains("journal"),"an absent journal never changes an existing request's bytes");
    }

    /// Rows a settled (committed) model attempt leaves, built by hand exactly as `commit_s08_policy` writes them.
    pub(crate) fn commit_rows(c:&mut Connection,m:&S08ModelPrepareRequest,j:&S08JournalDeltaSelection,job:&str,staged:Option<&str>) {
        let tx=c.transaction().unwrap();let account=&m.selection.source_account_id;let owner=&m.selection.owner;let attempt="attempt-1";let lease=1i64;
        let request="5".repeat(64);let reserve_hash="6".repeat(64);
        let reserve_id=format!("cs_s08modelreserve_{}",sha256_hex(&serde_json::to_vec(&("s08.model-reserve.v1",account,job,&reserve_hash)).unwrap()));
        tx.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,created_at,updated_at) VALUES(?,?,?,1,'succeeded',?,'t','t')",params![owner,attempt,job,lease]).unwrap();
        tx.execute("INSERT INTO fm_v2_s05_reservations(owner_id,attempt_id,job_id,lease_epoch,connection_id,coverage_generation,clock_generation,foreground_epoch,state,reserved_at_ms,settled_at_ms,reserved_calls,reserved_seconds,reserved_input_tokens,reserved_output_tokens,charged_calls,charged_seconds,charged_input_tokens,charged_output_tokens) VALUES(?,?,?,?,?,'cov','clk',0,'settled',1,2,1,60,?,?,1,1,10,10)",params![owner,attempt,job,lease,m.connection_id,m.max_input_tokens,m.max_output_tokens]).unwrap();
        let cs=|id:&str,reason:&str,actor:&str,source:Option<&str>,kind:&str,value:&Value,rev:i64| {
            let raw=serde_json::to_string(value).unwrap();
            tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation',?3,?4,?5,?6,?7,'t',?8)",params![owner,id,actor,reason,source,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),raw]).unwrap();
            tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,?4,?5,?6,'t')",params![owner,format!("outbox_{id}_{kind}"),id,kind,raw,rev]).unwrap();
        };
        cs(&reserve_id,"s08.model-reserve.v1","s08_model",None,"s08_model_reserve",&json!({"version":"s08.model-reserve.v1","request_sha256":reserve_hash,"owner":owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"foreground_epoch":0}),0);
        let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(account,job)).unwrap()));
        let content=staged.map(|text|serde_json::to_string(&j.proposal(text)).unwrap());
        let result=json!({"version":"s08.policy-result.v1","reserve_receipt_id":reserve_id,"request_sha256":request,"owner":owner,"account_id":account,"job_id":job,"attempt_id":attempt,"lease_epoch":lease,"output_kind":OUTPUT_KIND,"state":if staged.is_some() {"staged"} else {"no_proposal"},"candidate_id":staged.map(|_|candidate.clone())});
        let result_id=format!("cs_s08policyresult_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-result.v1",account,job,attempt,lease)).unwrap()));
        cs(&result_id,"s08.policy-result.v1","s08_policy",Some(attempt),"s08_policy_result",&result,0);
        let outcome=json!({"version":"s08.policy-outcome.v1","reserve_receipt_id":reserve_id,"request_sha256":request,"candidate_id":staged.map(|_|candidate.clone()),"content_sha256":content.as_ref().map(|v|sha256_hex(v.as_bytes())),"attempt_id":attempt,"lease_epoch":lease,"importance":if staged.is_some() {json!({"state":"skipped","reason":"unset"})} else {json!({"state":"skipped","reason":"no_candidate"})}});
        tx.execute("UPDATE fm_v2_jobs SET state='succeeded',current_attempt_id=?,attempt_count=1,result_json=? WHERE owner_id=? AND job_id=?",params![attempt,serde_json::to_string(&outcome).unwrap(),owner,job]).unwrap();
        if let Some(content)=content {
            let evidence=j.expected_evidence_ids(owner,job).unwrap();let now="t";
            let actor=RevisionActor{actor_type:"automation".into(),actor_id:ACTOR.into(),reason:REASON.into(),source_event_id:Some(attempt.into())};
            let mut payload=CandidatePayload{proposal:serde_json::from_str(&content).unwrap(),state:CandidateState::Proposed,confidence:0.0,importance:0.0,reason:REASON.into(),source_id:None,source_revision:None,accepted_block_id:None,evidence_ids:evidence.clone()};
            let change=insert_change_set(&tx,owner,&actor,&serde_json::to_value(&payload).unwrap(),now).unwrap();
            tx.execute("INSERT INTO fm_v2_candidates(owner_id,candidate_id,workspace_key,project_key,idempotency_key,created_change_set_id,current_revision,created_at) VALUES (?1,?2,?3,'',?2,?4,0,?5)",params![owner,candidate,m.selection.workspace_id,change,now]).unwrap();
            insert_candidate_revision(&tx,owner,&candidate,1,None,&payload,&actor,&change,now).unwrap();payload.state=CandidateState::Validating;insert_candidate_revision(&tx,owner,&candidate,2,Some(1),&payload,&actor,&change,now).unwrap();payload.state=CandidateState::AwaitingReview;insert_candidate_revision(&tx,owner,&candidate,3,Some(2),&payload,&actor,&change,now).unwrap();
            tx.execute("UPDATE fm_v2_candidates SET current_revision=3 WHERE owner_id=?1 AND candidate_id=?2",params![owner,candidate]).unwrap();
            tx.execute("INSERT INTO candidates(id,content,kind,confidence_score,importance_score,owner,workspace_id,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids) VALUES (?1,?2,'fact',0,0,?3,?4,'',?5,?6,'derived','auto_extracted',?7,?1,'pending',?8,?9,?9,?10,1,?11,'[]')",params![candidate,content,owner,m.selection.workspace_id,job,serde_json::to_string(&evidence).unwrap(),attempt,REASON,now,format!("s08://{job}"),crate::privacy::content_hash(&content)]).unwrap();
            insert_outbox(&tx,owner,&change,"candidate_revision",&json!({"candidate_id":candidate,"revision":3}),3,now).unwrap();
        }
        tx.commit().unwrap();
    }
    fn status(c:&mut Connection,m:&S08ModelPrepareRequest,sha:&str)->Value {
        let r=JournalDeltaStatusRequest{version:1,owner:m.selection.owner.clone(),workspace_id:m.selection.workspace_id.clone(),account_id:m.selection.source_account_id.clone(),policy_projection:m.policy_projection.clone(),selection_sha256:sha.into(),prepare:m.clone()};
        let before=c.total_changes();
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1",&r.account_id,&r.owner,&r.owner,&mut budget).unwrap();
        let out=V2Repository::journal_delta_status_in(&tx,&r,&view,&mut budget).unwrap();drop(view);drop(budget);drop(tx);
        assert_eq!(before,c.total_changes(),"status is a read: it never writes");out
    }

    #[test]
    fn status_reports_the_original_outcome_and_never_more() {
        // absent: nothing was prepared. selected: prepared, no attempt.
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"absent");
        let (job,_)=seed(&c,&sel,&j,"queued");
        let s=status(&mut c,&m,&j.selection_sha256);assert_eq!(s["state"],"selected");assert_eq!(s["job_id"],job.as_str());assert_eq!(s["page_sha256"],j.page_sha256.as_str());assert!(s["candidate_id"].is_null());
        // a foreign selection digest answers as absent; a changed page digest or row is unverifiable, never coverage
        assert_eq!(status(&mut c,&m,&"7".repeat(64))["state"],"absent");
        let mut page=m.clone();page.selection.journal.as_mut().unwrap().page_sha256="8".repeat(64);assert_eq!(status(&mut c,&page,&j.selection_sha256)["state"],"unknown");
        let mut changed=m.clone();changed.selection.journal.as_mut().unwrap().rows[2].reason="source_denied".into();assert_eq!(status(&mut c,&changed,&j.selection_sha256)["state"],"unknown");
        // no_finding recorded
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,None);
        let s=status(&mut c,&m,&j.selection_sha256);assert_eq!(s["state"],"recorded_no_finding");assert!(s["candidate_id"].is_null());assert_eq!(s["receipt_ids"].as_array().unwrap().len(),4);
        // a damaged result receipt is unknown, not no_finding
        c.execute("UPDATE fm_v2_change_sets SET operation_metadata_json=replace(operation_metadata_json,'no_proposal','staged') WHERE reason='s08.policy-result.v1'",[]).unwrap();
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"unknown");
        // proposal recorded: a pending inferred candidate that the lifecycle reader accepts, for exactly this proposal
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,Some("The user chose Friday."));
        let s=status(&mut c,&m,&j.selection_sha256);assert_eq!(s["state"],"recorded_proposal");
        let candidate=s["candidate_id"].as_str().unwrap().to_owned();assert!(candidate.starts_with("s08candidate_"));
        {let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1","acct",OWNER,OWNER,&mut budget).unwrap();
        let settled=s08_policy_lifecycle::settled_candidate(&tx,OWNER,"acct","ws",&candidate,&view,&mut budget).unwrap();
        assert_eq!(settled.intent.output_kind,OUTPUT_KIND);assert!(j.proposal_ok(&crate::field_authority::parse_unique_json(&settled.content).unwrap()));}
        // a proposal edited after the fact is refused by the reader
        c.execute("UPDATE candidates SET content=replace(content,'Friday','Monday') WHERE id=?",[&candidate]).unwrap();
        {let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1","acct",OWNER,OWNER,&mut budget).unwrap();
        assert!(s08_policy_lifecycle::settled_candidate(&tx,OWNER,"acct","ws",&candidate,&view,&mut budget).is_err());}
    }

    #[test]
    fn the_selective_classifier_accepts_a_committed_journal_job() {
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,None);
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let (retained,erase,bindings,locators,retained_locators)=classify_selective_s08(&tx,OWNER,Some("acct"),None,&mut budget).unwrap();
        assert!(retained.is_empty() && erase.is_empty() && retained_locators.is_empty());
        assert_eq!(bindings.len(),1);assert_eq!(locators.len(),1);assert_eq!(locators[0].job_id,job);
    }

    /// A prepared (never attempted) original, cancelled with its factual receipt exactly as `cancel_s08_policy_prepared` writes it.
    fn cancel_prepared(c:&mut Connection,m:&S08ModelPrepareRequest,job:&str) {
        let account=&m.selection.source_account_id;let owner=&m.selection.owner;
        let id=format!("cs_s08policycancel_{}",sha256_hex(&serde_json::to_vec(&("s08.policy-cancel.v1",account,job)).unwrap()));
        let value=json!({"version":"s08.policy-cancel.v1","owner":owner,"account_id":account,"job_id":job,"request_sha256":sha256_hex(&serde_json::to_vec(m).unwrap()),"state":"cancelled"});
        let raw=serde_json::to_string(&value).unwrap();
        c.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','s08_policy','s08.policy-cancel.v1',NULL,?3,?4,'t',?5)",params![owner,id,CONTRACT_VERSION,sha256_hex(raw.as_bytes()),raw]).unwrap();
        c.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s08_policy_cancel',?4,0,'t')",params![owner,format!("outbox_{id}_s08_policy_cancel"),id,raw]).unwrap();
        c.execute("UPDATE fm_v2_jobs SET state='cancelled' WHERE owner_id=? AND job_id=?",params![owner,job]).unwrap();
    }
    fn revive(c:&mut Connection,m:&S08ModelPrepareRequest,job:&str)->Result<bool,RepositoryError> {
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1",&m.selection.source_account_id,OWNER,OWNER,&mut budget).unwrap();
        let out=V2Repository::revive_cancelled_journal_original(&tx,m,job,&view,&mut budget);drop(view);drop(budget);
        if out.is_ok() {tx.commit().unwrap();}
        out
    }

    #[test]
    fn a_journal_cancelled_before_dispatch_can_be_prepared_again_with_the_same_key_and_no_other_state_can() {
        // prepare -> prepared_cancel: status says cancelled; the same key is then re-preparable (the original is replaced).
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");
        assert!(s08_journal_replay_open("queued"),"an idempotent replay of a live original stays allowed");
        assert!(!revive(&mut c,&m,&job).unwrap(),"a live original is never replaced");
        cancel_prepared(&mut c,&m,&job);
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"cancelled");
        assert!(!s08_journal_replay_open("cancelled"),"a cancelled original is not replayed; it is replaced first or refused");
        let unrelated=c.query_row("SELECT count(*) FROM fm_v2_job_manifests",[],|r|r.get::<_,i64>(0)).unwrap();assert_eq!(unrelated,1);
        assert!(revive(&mut c,&m,&job).unwrap(),"a verified prepared-cancelled original is removed");
        for table in ["fm_v2_jobs","fm_v2_job_manifests","fm_v2_evidence","fm_v2_change_sets","fm_v2_outbox"] {assert_eq!(count(&c,&format!("SELECT count(*) FROM {table}")),0,"{table} is empty after the original is replaced");}
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"absent");
        assert!(!revive(&mut c,&m,&job).unwrap());
        // the same key can now be prepared again: the seeded rows are exactly what a fresh prepare writes
        let (job2,_)=seed(&c,&sel,&j,"queued");assert_eq!(job2,job,"same key, same job identity");assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"selected");
        // prepare -> reserve/dispatch -> outcome or cancel: never re-prepared, status keeps the original
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,None);
        assert!(!revive(&mut c,&m,&job).unwrap());assert!(!s08_journal_replay_open("succeeded"));
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"recorded_no_finding");
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_jobs"),1,"nothing was removed");
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,None);
        // dispatched then cancelled (an attempt exists): the cancel does not verify as a prepared cancel, so nothing is replaced
        c.execute("UPDATE fm_v2_jobs SET state='cancelled',result_json=NULL WHERE job_id=?",[&job]).unwrap();
        assert!(!revive(&mut c,&m,&job).unwrap());assert!(!s08_journal_replay_open("cancelled"));assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_attempts"),1);
        assert_eq!(status(&mut c,&m,&j.selection_sha256)["state"],"unknown","issued work with no verified outcome is unknown, never coverage");
        // a cancelled original whose cancel receipt does not verify is refused, not silently replaced
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");
        c.execute("UPDATE fm_v2_jobs SET state='cancelled' WHERE job_id=?",[&job]).unwrap();
        assert!(revive(&mut c,&m,&job).is_err());assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_jobs"),1);
    }

    // ---- Group 4: review of a journal proposal through the real review functions ----
    /// Policy projection, the `s08.composite` predicate, and nothing else: what the review lane needs beyond the committed rows.
    pub(crate) fn review_ready(c:&Connection,m:&S08ModelPrepareRequest) {
        let refs=&m.policy_projection;
        c.execute("INSERT OR REPLACE INTO fm_meta(key,value) VALUES('openclank.policy_projection.v1','1')",[]).unwrap();
        let database="db-1";let target=&m.selection.workspace_id;let account=&m.selection.source_account_id;
        let account_key=format!("policy_projection.v1.{}",sha256_hex(&serde_json::to_vec(&(database,"account",account,target)).unwrap()));
        let global_key=format!("policy_projection.v1.{}",sha256_hex(&serde_json::to_vec(&(database,"global")).unwrap()));
        for (key,r) in [(account_key,&refs.account),(global_key,&refs.global)] {
            c.execute("INSERT OR REPLACE INTO fm_meta(key,value) VALUES(?,?)",params![key,json!({"version":1,"epoch":r.epoch,"fingerprint":r.fingerprint,"state":"ready","transition_id":r.transition_id}).to_string()]).unwrap();
        }
        c.execute("INSERT OR REPLACE INTO fm_v2_predicates(predicate,value_type,cardinality,version,allow_unknown) VALUES('s08.composite','object','single',1,0)",[]).unwrap();
    }
    pub(crate) fn review_request(m:&S08ModelPrepareRequest,candidate:&str,sha:&str,action:&str,key:&str)->super::s02_connection::ConnectionReviewRequest {
        super::s02_connection::ConnectionReviewRequest{version:1,owner:m.selection.owner.clone(),account_id:m.selection.source_account_id.clone(),workspace_id:m.selection.workspace_id.clone(),candidate_id:candidate.into(),candidate_revision:3,candidate_sha256:sha.into(),request_key:key.into(),reason:"reviewed".into(),action:action.into(),policy_projection:m.policy_projection.clone()}
    }
    /// One real review step on the in-memory store: `accept` runs `accept_journal`, `reject` the shared reject path.
    pub(crate) fn review(c:&mut Connection,m:&S08ModelPrepareRequest,owner:&str,candidate:&str,sha:&str,action:&str,key:&str)->Result<Value,RepositoryError> {
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1",&m.selection.source_account_id,owner,owner,&mut budget).unwrap();
        let mut r=review_request(m,candidate,sha,action,key);r.owner=owner.into();
        let out=if action=="accept" {super::s08_journal_review::accept_journal(&tx,&r,&view,&mut budget)} else {super::s08_policy_review::reject_apply(&tx,&r,OUTPUT_KIND,&view,&mut budget)};
        drop(view);drop(budget);if out.is_ok() {tx.commit().unwrap();}out
    }
    /// A committed, pending journal proposal ready for review: (connection, selection, journal, prepare, job, candidate id, candidate sha).
    pub(crate) fn pending_proposal()->(Connection,S08RulesSelection,S08JournalDeltaSelection,S08ModelPrepareRequest,String,String,String) {
        let (mut c,sel,j)=fixture();let m=prepare(&sel,&j);let (job,_)=seed(&c,&sel,&j,"queued");commit_rows(&mut c,&m,&j,&job,Some("The user chose Friday."));review_ready(&c,&m);
        let candidate=format!("s08candidate_{}",sha256_hex(&serde_json::to_vec(&(&sel.source_account_id,&job)).unwrap()));
        let content:String=c.query_row("SELECT content FROM candidates WHERE id=?",[&candidate],|r|r.get(0)).unwrap();let sha=sha256_hex(content.as_bytes());
        (c,sel,j,m,job,candidate,sha)
    }
    fn manifest(c:&mut Connection,m:&S08ModelPrepareRequest,owner:&str,candidate:&str)->Result<Value,RepositoryError> {
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1",&m.selection.source_account_id,owner,owner,&mut budget).unwrap();
        let out=super::s08_journal_review::journal_manifest(&tx,owner,&m.selection.source_account_id,&m.selection.workspace_id,candidate,&m.policy_projection,&view,&mut budget);
        drop(view);drop(budget);out
    }

    #[test]
    fn the_review_preview_shows_text_selectors_and_producer_and_nothing_else() {
        let (mut c,_sel,j,m,job,candidate,sha)=pending_proposal();
        let before=c.total_changes();
        let p=manifest(&mut c,&m,OWNER,&candidate).unwrap();assert_eq!(before,c.total_changes(),"a preview writes nothing");
        assert_eq!(p["candidate_sha256"],sha.as_str());assert_eq!(p["content"],"The user chose Friday.");assert_eq!(p["output_kind"],OUTPUT_KIND);assert_eq!(p["candidate_revision"],3);
        let cited=p["journal"]["cited"].as_array().unwrap();assert_eq!(cited.len(),2,"the two `included` rows, not the omitted/gap ones");
        assert_eq!(cited[0]["selector"],j.rows[0].selector);assert_eq!(cited[0]["message_id"],"m2");assert_eq!(cited[0]["revision"],1);
        assert_eq!(p["journal"]["selection_sha256"],j.selection_sha256.as_str());assert_eq!(p["journal"]["page_sha256"],j.page_sha256.as_str());
        assert_eq!(p["producer"]["actor"],ACTOR);assert_eq!(p["producer"]["job_id"],job.as_str());assert_eq!(p["producer"]["model_route_id"],"route-1");assert_eq!(p["producer"]["assignment_sha256"],m.assignment_sha256.as_str());
        let text=p.to_string();for forbidden in ["body_text","\"text\":","quote"] {assert!(!text.contains(forbidden),"no source text in a preview");}
        // unknown id and a foreign owner answer as the existing kinds do: a plain refusal, nothing written
        assert!(manifest(&mut c,&m,OWNER,"s08candidate_unknown").is_err());
        assert!(manifest(&mut c,&m,"mallory",&candidate).is_err());
        // an already decided candidate is refused
        review(&mut c,&m,OWNER,&candidate,&sha,"reject","k").unwrap();
        assert!(manifest(&mut c,&m,OWNER,&candidate).is_err());
    }

    /// The summary lineage reader works on what an accepted candidate stores. No summary fixture exists, so an accepted journal candidate's rows
    /// stand in: its content is replaced by a summary-shaped proposal (the reader only parses it), everything else is the real stored rows.
    #[test]
    fn an_accepted_candidate_has_summary_lineage_from_stored_rows_and_a_journal_one_has_none() {
        let (mut c,_sel,_j,m,_job,candidate,sha)=pending_proposal();
        assert_eq!(review(&mut c,&m,OWNER,&candidate,&sha,"accept","key-lineage-1").unwrap()["state"],"applied");
        let ws:String=c.query_row("SELECT workspace_id FROM candidates WHERE id=?",[&candidate],|r|r.get(0)).unwrap();
        assert!(super::super::s08_policy_review::summary_lineage(&c,OWNER,&ws,&candidate).is_none(),"a journal proposal is not a summary");
        let summary=json!({"version":"s08.policy-summary.v1","classification":"inferred_untrusted","semantic_claim":"none","summary":"s","source_block_ids":["b1","b2"],"importance":"normal"}).to_string();
        c.execute("UPDATE candidates SET content=? WHERE id=?",rusqlite::params![summary,candidate]).unwrap();
        let lineage=super::super::s08_policy_review::summary_lineage(&c,OWNER,&ws,&candidate).expect("lineage from stored rows");
        assert_eq!(lineage["kind"],"summary");assert_eq!(lineage["reviewer"],OWNER);assert_eq!(lineage["producer"]["actor"],"s08_policy");
        assert_eq!(lineage["summary"]["source_block_ids"],json!(["b1","b2"]));
        assert!(lineage["block_id"].as_str().unwrap().starts_with("s08policyblock_"));
        assert!(!lineage["evidence_ids"].as_array().unwrap().is_empty(),"the block's evidence links");
        assert!(super::super::s08_policy_review::summary_lineage(&c,"someone-else",&ws,&candidate).is_none());
    }

    #[test]
    fn accept_publishes_one_inferred_block_with_producer_and_approver_kept_apart_and_replays_to_the_original() {
        let (mut c,_sel,j,m,job,candidate,sha)=pending_proposal();
        let evidence_before=count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE locator_type='s08_journal_source'");
        let selection_before:String=c.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE reason='s08.selection.v1'",[],|r|r.get(0)).unwrap();
        let revision3_before:String=c.query_row("SELECT proposal_json FROM fm_v2_candidate_revisions WHERE candidate_id=? AND revision=3",[&candidate],|r|r.get(0)).unwrap();
        // a stale digest, a foreign owner and a wrong action refuse and write nothing
        let writes=c.total_changes();
        assert!(review(&mut c,&m,OWNER,&candidate,&"1".repeat(64),"accept","key-1").is_err());
        assert!(review(&mut c,&m,"mallory",&candidate,&sha,"accept","key-1").is_err());
        assert_eq!(writes,c.total_changes());
        let applied=review(&mut c,&m,OWNER,&candidate,&sha,"accept","key-1").unwrap();
        assert_eq!(applied["state"],"applied");let block=applied["block_id"].as_str().unwrap().to_owned();
        assert_eq!(block,format!("s08policyblock_{}",sha256_hex(&serde_json::to_vec(&("acct",&candidate)).unwrap())));
        // the derived block: inferred/untrusted, review-approved, the proposal as its object value, journal evidence ids as its evidence
        let (actor_type,actor_id,source,status,kind,activation,rationale,value,evidence_json):(String,String,String,String,String,String,String,String,String)=c.query_row("SELECT actor_type,actor_id,source_event_id,status,kind,activation,activation_rationale,value_json,evidence_json FROM fm_v2_knowledge_revisions WHERE block_id=? AND revision=1",[&block],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?))).unwrap();
        assert_eq!((actor_type.as_str(),actor_id.as_str(),source.as_str(),status.as_str(),kind.as_str(),activation.as_str()),("user",OWNER,"attempt-1","active","fact","review_approved"));
        let rationale:Value=serde_json::from_str(&rationale).unwrap();
        assert_eq!(rationale["classification"],"inferred_untrusted");assert_eq!(rationale["reviewer"],OWNER);
        assert_eq!(rationale["producer"],json!({"actor":ACTOR,"job_id":job,"role":OUTPUT_KIND,"mode":"model","model_route_id":"route-1","assignment_sha256":m.assignment_sha256}),"the producer is kept beside, not replaced by, the approving owner");
        let value:Value=serde_json::from_str(&value).unwrap();assert_eq!(value["type"],"object");assert!(j.proposal_ok(&value["value"]));
        assert_eq!(serde_json::from_str::<Vec<String>>(&evidence_json).unwrap(),j.expected_evidence_ids(OWNER,&job).unwrap());
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_knowledge_blocks WHERE predicate='s08.composite'"),1);
        // only the approved value path becomes manual authority
        assert_eq!(count(&c,&format!("SELECT count(*) FROM fm_v2_revision_field_authority WHERE block_id='{block}' AND field_path='/value' AND origin='manual' AND approved_by='alice'")),1);
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_revision_field_authority WHERE field_path='/value' AND origin<>'manual'"),0);
        // the candidate is accepted and linked to its curated projection; the originals are untouched
        assert_eq!(c.query_row("SELECT status FROM candidates WHERE id=?",[&candidate],|r|r.get::<_,String>(0)).unwrap(),"accepted");
        assert_eq!(count(&c,"SELECT count(*) FROM curated WHERE source_type='autoextracted'"),1);
        // the curated record carries the explainable lineage: cited parts, producer, reviewer
        let meta:String=c.query_row("SELECT metadata FROM curated WHERE source_type='autoextracted'",[],|r|r.get(0)).unwrap();let meta:Value=serde_json::from_str(&meta).unwrap();
        assert_eq!(meta["lineage"]["kind"],OUTPUT_KIND);assert_eq!(meta["lineage"]["reviewer"],OWNER);assert_eq!(meta["lineage"]["producer"]["actor"],ACTOR);
        assert_eq!(meta["lineage"]["journal"]["selection_sha256"],j.selection_sha256.as_str());assert_eq!(meta["lineage"]["journal"]["rows"].as_array().unwrap().len(),2);
        // the normal legacy reader keeps the accepted block's curated record (its job is a policy outcome, not a rules selection)
        {
            use crate::store::sqlite::{LegacyLineageKind,LegacyLineageTarget,SqliteStore};
            let (id,ws):(String,String)=c.query_row("SELECT id,workspace_id FROM curated WHERE source_type='autoextracted'",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
            let target=LegacyLineageTarget{kind:LegacyLineageKind::Curated,owner:OWNER.into(),workspace:ws,id};
            assert!(SqliteStore::rejected_legacy_lineage(&c,&[target]).unwrap().is_empty(),"an accepted journal block stays listed");
        }
        assert_eq!(count(&c,"SELECT count(*) FROM fm_v2_evidence WHERE locator_type='s08_journal_source'"),evidence_before);
        assert_eq!(c.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE reason='s08.selection.v1'",[],|r|r.get::<_,String>(0)).unwrap(),selection_before);
        assert_eq!(c.query_row("SELECT proposal_json FROM fm_v2_candidate_revisions WHERE candidate_id=? AND revision=3",[&candidate],|r|r.get::<_,String>(0)).unwrap(),revision3_before);
        // the receipt is the EXISTING s08.policy-review.v1, fixed 15 keys, unchanged shape
        let raw:String=c.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE reason='s08.policy-review.v1'",[],|r|r.get(0)).unwrap();
        let receipt:Value=serde_json::from_str(&raw).unwrap();assert_eq!(receipt.as_object().unwrap().len(),15);assert_eq!(receipt["action"],"accept");assert_eq!(receipt["candidate_sha256"],sha.as_str());
        // replay of the same decision returns the original and writes nothing; another decision on a decided candidate refuses
        let writes=c.total_changes();
        let again=review(&mut c,&m,OWNER,&candidate,&sha,"accept","key-1").unwrap();assert_eq!(again["state"],"applied");assert_eq!(again["block_id"],block.as_str());assert_eq!(writes,c.total_changes());
        assert!(review(&mut c,&m,OWNER,&candidate,&sha,"reject","key-2").is_err());
        assert!(review(&mut c,&m,OWNER,&candidate,&sha,"accept","key-2").is_err());
        // the extended lifecycle reader classifies the accepted root exactly
        let tx=c.transaction().unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,"db-1","acct",OWNER,OWNER,&mut budget).unwrap();
        let root=super::s08_journal_review::journal_accepted_root(&tx,OWNER,"acct","ws",&candidate,&view,&mut budget).unwrap();assert_eq!(root,receipt);
        let original=super::s08_journal_review::journal_original(&tx,OWNER,"acct","ws",&candidate,None,&m.policy_projection,&view,&mut budget).unwrap();assert_eq!(original["state"],"found");assert_eq!(original["original"]["action"],"accept");
    }

    #[test]
    fn reject_changes_only_the_candidate_state_and_its_receipt() {
        let (mut c,_sel,_j,m,_job,candidate,sha)=pending_proposal();
        let snapshot=|c:&Connection|->Vec<i64> {["fm_v2_evidence","fm_v2_job_manifests","fm_v2_jobs","fm_v2_knowledge_blocks","fm_v2_knowledge_revisions","fm_v2_entities","curated","fm_v2_revision_evidence"].iter().map(|t|count(c,&format!("SELECT count(*) FROM {t}"))).collect()};
        let before=snapshot(&c);
        let out=review(&mut c,&m,OWNER,&candidate,&sha,"reject","key-r").unwrap();assert_eq!(out["state"],"rejected");
        assert_eq!(snapshot(&c),before,"no block, entity, curated row or evidence change");
        assert_eq!(c.query_row("SELECT status FROM candidates WHERE id=?",[&candidate],|r|r.get::<_,String>(0)).unwrap(),"rejected");
        let raw:String=c.query_row("SELECT operation_metadata_json FROM fm_v2_change_sets WHERE reason='s08.policy-reject.v1'",[],|r|r.get(0)).unwrap();assert_eq!(serde_json::from_str::<Value>(&raw).unwrap().as_object().unwrap().len(),10);
        let writes=c.total_changes();assert_eq!(review(&mut c,&m,OWNER,&candidate,&sha,"reject","key-r").unwrap()["state"],"rejected");assert_eq!(writes,c.total_changes(),"replay returns the original");
        assert!(review(&mut c,&m,OWNER,&candidate,&sha,"accept","key-a").is_err(),"a rejected proposal cannot then be accepted");
        assert!(review(&mut c,&m,OWNER,&candidate,&"1".repeat(64),"reject","key-r2").is_err(),"a stale digest refuses");
    }
}
