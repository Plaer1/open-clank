//! Same-account custody facts backed by the existing guarded rename receipt.
//! This module does not grant source, review, account or mutation authority.
use super::*;
use crate::store::sqlite::GuardedOwnerRenameRequest;
use std::collections::BTreeSet;

#[derive(Clone,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
struct CustodyLink {
    version:String, request:GuardedOwnerRenameRequest, receipt_id:String, receipt_sha256:String,
    previous_id:Option<String>, previous_sha256:Option<String>,
}
#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct CustodyHead {
    version:String,database_id:String,account_id:String,anchor_owner:String,current_owner:String,
    link_id:String,link_sha256:String,
}
/// A verified lineage view only. Callers separately authenticate the account,
/// current owner, policy and any live source access before using these facts.
pub(crate) struct VerifiedCustodyView<'a,'db> {
    tx:&'a Transaction<'db>,account_id:String,current_owner:String,captured_owner:String,owners:Vec<String>,
    transitions:Vec<(GuardedOwnerRenameRequest,String,String)>,
}
/// A per-record captured owner within the same checked account chain.
pub(crate) struct VerifiedCapturedOwner<'a> {account_id:&'a str,current_owner:&'a str,captured_owner:&'a str}
impl<'a> VerifiedCapturedOwner<'a> {
    pub(crate) fn account_id(&self)->&str {self.account_id}
    pub(crate) fn current_owner(&self)->&str {self.current_owner}
    pub(crate) fn captured_owner(&self)->&str {self.captured_owner}
}
impl<'a,'db> VerifiedCustodyView<'a,'db> {
    pub(crate) fn account_id(&self)->&str {&self.account_id}
    pub(crate) fn current_owner(&self)->&str {&self.current_owner}
    pub(crate) fn captured_owner(&self)->&str {&self.captured_owner}
    /// Owner labels actually traversed in this verified chain, newest first.
    pub(crate) fn owners(&self)->&[String] {&self.owners}
    /// Historical fact only; caller still owns live authority and source state.
    pub(crate) fn contains_transition(&self,tx:&Transaction<'_>,request:&GuardedOwnerRenameRequest,receipt_id:&str,receipt_hash:&str)->Result<bool,RepositoryError> {
        if !std::ptr::eq(self.tx,tx) || request.account_id!=self.account_id {return Err(unavailable());}
        Ok(self.transitions.iter().any(|(r,id,hash)|r==request && id==receipt_id && hash==receipt_hash))
    }
    /// Renewal requires the actual current head, not an older superseded move.
    pub(crate) fn is_head_transition(&self,tx:&Transaction<'_>,request:&GuardedOwnerRenameRequest,receipt_id:&str,receipt_hash:&str)->Result<bool,RepositoryError> {
        if !std::ptr::eq(self.tx,tx) || request.account_id!=self.account_id {return Err(unavailable());}
        Ok(self.transitions.first().is_some_and(|(r,id,hash)|r==request && id==receipt_id && hash==receipt_hash))
    }
    /// Factual committed head only; this is not a current mutation grant.
    pub(crate) fn head_transition<'v>(&'v self,tx:&Transaction<'_>)->Result<Option<&'v (GuardedOwnerRenameRequest,String,String)>,RepositoryError> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}
        Ok(self.transitions.first())
    }
    /// Exact newest-first committed links from this same verified account
    /// snapshot. These are facts, never a mutation or source grant.
    pub(crate) fn transitions<'v>(&'v self,tx:&Transaction<'_>)->Result<&'v [(GuardedOwnerRenameRequest,String,String)],RepositoryError> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}Ok(&self.transitions)
    }
    /// Each historical record supplies its own captured actor/owner. The same
    /// verified chain may describe distinct owners for root and successor.
    pub(crate) fn for_captured<'v>(&'v self,tx:&Transaction<'_>,captured:&str)->Result<VerifiedCapturedOwner<'v>,RepositoryError> {
        if !std::ptr::eq(self.tx,tx) {return Err(unavailable());}
        let owner=self.owners.iter().find(|v|v.as_str()==captured).ok_or_else(unavailable)?;
        Ok(VerifiedCapturedOwner{account_id:&self.account_id,current_owner:&self.current_owner,captured_owner:owner})
    }
}
fn unavailable()->RepositoryError {RepositoryError::NotFound}
fn text_ok(v:&str)->bool {!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control)}
fn account_digest(database:&str,account:&str)->Result<String,RepositoryError> {
    if !text_ok(database) || !text_ok(account) {return Err(unavailable());}
    Ok(sha256_hex(&serde_json::to_vec(&("s04.owner-custody-head.v1",database,account))?))
}
fn head_key(database:&str,account:&str)->Result<String,RepositoryError> {
    Ok(format!("owner_custody_head_{}",account_digest(database,account)?))
}
fn link_prefix(database:&str,account:&str)->Result<String,RepositoryError> {
    Ok(format!("owner_custody_link_{}_",account_digest(database,account)?))
}
fn link_key(database:&str,account:&str,receipt:&str)->Result<String,RepositoryError> {
    if !receipt.strip_prefix("owner_transition_").is_some_and(valid_sha256) {return Err(unavailable());}
    Ok(format!("{}{}",link_prefix(database,account)?,sha256_hex(receipt.as_bytes())))
}
// Read length before allocating/decoding an fm_meta value. Caller owns the
// original connection's single progress handler and budget.
fn meta(tx:&Connection,key:&str,budget:&mut ResetFenceBudget)->Result<Option<String>,RepositoryError> {
    let len:Option<i64>=tx.query_row("SELECT length(CAST(value AS BLOB)) FROM fm_meta WHERE key=?1",[key],|r|r.get(0)).optional()?;
    let Some(len)=len else {return Ok(None)};let len=usize::try_from(len).map_err(|_|unavailable())?;
    if len>65536 {return Err(unavailable());}budget.check(len.saturating_add(key.len())).map_err(RepositoryError::Validation)?;
    let raw:String=tx.query_row("SELECT value FROM fm_meta WHERE key=?1",[key],|r|r.get(0))?;
    if raw.len()!=len {return Err(unavailable());}Ok(Some(raw))
}
fn link(tx:&Connection,database:&str,account:&str,id:&str,expected_hash:&str,budget:&mut ResetFenceBudget)->Result<CustodyLink,RepositoryError> {
    if !valid_sha256(expected_hash) {return Err(unavailable());}
    let raw=meta(tx,id,budget)?.ok_or_else(unavailable)?;
    if sha256_hex(raw.as_bytes())!=expected_hash {return Err(unavailable());}
    let value:CustodyLink=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if value.version!="s04.owner-custody-link.v1" || value.request.database_id!=database || value.request.account_id!=account || link_key(database,account,&value.receipt_id)?!=id || !valid_sha256(&value.receipt_sha256) || value.previous_id.is_some()!=value.previous_sha256.is_some() || value.previous_sha256.as_deref().is_some_and(|v|!valid_sha256(v)) {return Err(unavailable());}
    validate_queued_owner_receipt(tx,&value.request,&value.receipt_id,&value.receipt_sha256)?;
    Ok(value)
}
// A later phase of the same guarded operation must follow the actual prior
// forward/reverse receipt; a new operation begins at forward generation one.
fn follows(current:&GuardedOwnerRenameRequest,previous:Option<&CustodyLink>)->Result<(),RepositoryError> {
    let Some(previous)=previous else {
        if current.action!="forward" || current.generation!=1 || current.prior_receipt_id.is_some() || current.prior_receipt_hash.is_some() {return Err(unavailable());}
        return Ok(());
    };
    if previous.request.new_owner!=current.owner {return Err(unavailable());}
    if current.action=="forward" && current.generation==1 {
        if current.prior_receipt_id.is_some() || current.prior_receipt_hash.is_some() {return Err(unavailable());}
    } else {
        let expected=if current.action=="reverse" {Some((current.generation,"forward"))} else if current.action=="forward" && current.generation>1 {Some((current.generation-1,"reverse"))} else {None}.ok_or_else(unavailable)?;
        if previous.request.operation_id!=current.operation_id || previous.request.step!=current.step || previous.request.generation!=expected.0 || previous.request.action!=expected.1 || current.prior_receipt_id.as_deref()!=Some(previous.receipt_id.as_str()) || current.prior_receipt_hash.as_deref()!=Some(previous.receipt_sha256.as_str()) {return Err(unavailable());}
    }
    Ok(())
}
// Returns newest-to-oldest fully checked links and the exact stored head bytes.
fn chain(tx:&Connection,database:&str,account:&str,budget:&mut ResetFenceBudget)->Result<Option<(CustodyHead,String,Vec<(String,String,CustodyLink)>)>,RepositoryError> {
    let key=head_key(database,account)?;let Some(raw)=meta(tx,&key,budget)? else {return Ok(None)};
    let head:CustodyHead=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
    if head.version!="s04.owner-custody-head.v1" || head.database_id!=database || head.account_id!=account || !text_ok(&head.anchor_owner) || !text_ok(&head.current_owner) || !valid_sha256(&head.link_sha256) {return Err(unavailable());}
    let mut result=Vec::new();let mut seen=BTreeSet::new();let mut cursor=Some((head.link_id.clone(),head.link_sha256.clone()));let mut expected_owner=head.current_owner.clone();
    while let Some((id,hash))=cursor {
        budget.check(0).map_err(RepositoryError::Validation)?;if !seen.insert(id.clone()) {return Err(unavailable());}
        let item=link(tx,database,account,&id,&hash,budget)?;if item.request.new_owner!=expected_owner {return Err(unavailable());}
        expected_owner=item.request.owner.clone();cursor=match (&item.previous_id,&item.previous_sha256) {(Some(id),Some(hash))=>Some((id.clone(),hash.clone())),(None,None)=>None,_=>return Err(unavailable())};
        result.push((id,hash,item));
    }
    if result.is_empty() || expected_owner!=head.anchor_owner {return Err(unavailable());}
    for i in 0..result.len() {follows(&result[i].2.request,result.get(i+1).map(|v|&v.2))?;}
    Ok(Some((head,raw,result)))
}

/// Append only for a genuinely newly inserted v1 receipt, in the same original
/// IMMEDIATE owner-move transaction. A historical replay is status-only and
/// must not call this with a fabricated "new" receipt.
pub(crate) fn append_committed_transition(tx:&Transaction<'_>,request:&GuardedOwnerRenameRequest,receipt_id:&str,receipt_hash:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    validate_queued_owner_receipt(tx,request,receipt_id,receipt_hash)?;
    // A missing head is not a new lineage when indexed orphan links exist.
    // This uses only the exact account prefix and the caller's budget.
    account_custody_keys(tx,&request.database_id,&request.account_id,budget)?;
    let key=head_key(&request.database_id,&request.account_id)?;let id=link_key(&request.database_id,&request.account_id,receipt_id)?;
    let current=chain(tx,&request.database_id,&request.account_id,budget)?;
    if let Some(existing)=meta(tx,&id,budget)? {
        let value:CustodyLink=serde_json::from_value(crate::field_authority::parse_unique_json(&existing).map_err(RepositoryError::Validation)?)?;
        if value.request!=*request || value.receipt_id!=receipt_id || value.receipt_sha256!=receipt_hash {return Err(unavailable());}
        // Replaying a valid older operation never moves the head backwards.
        if current.as_ref().is_none_or(|(_,_,links)|!links.iter().any(|(link_id,hash,_)|link_id==&id && hash==&sha256_hex(existing.as_bytes()))) {return Err(unavailable());}
        return Ok(());
    }
    let previous=current.as_ref().and_then(|(_,_,links)|links.first().map(|v|&v.2));
    if current.as_ref().is_some_and(|(head,_,_)|head.current_owner!=request.owner) {return Err(unavailable());}
    follows(request,previous)?;
    let link=CustodyLink{version:"s04.owner-custody-link.v1".into(),request:request.clone(),receipt_id:receipt_id.into(),receipt_sha256:receipt_hash.into(),previous_id:current.as_ref().map(|(h,_,_)|h.link_id.clone()),previous_sha256:current.as_ref().map(|(h,_,_)|h.link_sha256.clone())};
    let raw=serde_json::to_string(&link)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
    let head=CustodyHead{version:"s04.owner-custody-head.v1".into(),database_id:request.database_id.clone(),account_id:request.account_id.clone(),anchor_owner:current.as_ref().map(|(h,_,_)|h.anchor_owner.clone()).unwrap_or_else(||request.owner.clone()),current_owner:request.new_owner.clone(),link_id:id.clone(),link_sha256:sha256_hex(raw.as_bytes())};
    let head_raw=serde_json::to_string(&head)?;budget.check(head_raw.len()).map_err(RepositoryError::Validation)?;
    tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![id,raw])?;
    let changed=if let Some((_,old,_))=current {tx.execute("UPDATE fm_meta SET value=?1 WHERE key=?2 AND value=?3",params![head_raw,key,old])?} else {tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,?2)",params![key,head_raw])?};
    if changed!=1 {return Err(unavailable());}Ok(())
}

/// Verify captured/current owner continuity; unavailable older history stays
/// unavailable. The caller supplies independently authenticated current scope.
pub(crate) fn verified_custody_view<'a,'db>(tx:&'a Transaction<'db>,database:&str,account:&str,current:&str,captured:&str,budget:&mut ResetFenceBudget)->Result<VerifiedCustodyView<'a,'db>,RepositoryError> {
    if !text_ok(current) || !text_ok(captured) {return Err(unavailable());}
    // Do not accept a valid-looking head while another account-scoped link is
    // detached, nor treat an absent head with orphan links as empty history.
    account_custody_keys(tx,database,account,budget)?;
    let (head,_,links)=chain(tx,database,account,budget)?.ok_or_else(unavailable)?;
    if head.current_owner!=current {return Err(unavailable());}
    let mut owners=Vec::new();let mut seen=BTreeSet::new();
    for value in std::iter::once(current).chain(links.iter().map(|v|v.2.request.owner.as_str())) {if seen.insert(value.to_owned()) {owners.push(value.to_owned());}}
    if !owners.iter().any(|v|v==captured) {return Err(unavailable());}
    let transitions=links.iter().map(|(_,_,link)|(link.request.clone(),link.receipt_id.clone(),link.receipt_sha256.clone())).collect();
    Ok(VerifiedCustodyView{tx,account_id:account.into(),current_owner:current.into(),captured_owner:captured.into(),owners,transitions})
}

/// Select the normal verified chain, or a current-only factual view when the
/// exact account index positively has no head and no links. This does not
/// certify any earlier legacy history and never grants a mutation.
pub(crate) fn verified_or_direct_custody_view<'a,'db>(tx:&'a Transaction<'db>,database:&str,account:&str,current:&str,captured:&str,budget:&mut ResetFenceBudget)->Result<VerifiedCustodyView<'a,'db>,RepositoryError> {
    if ![database,account,current,captured].iter().all(|v|text_ok(v)) {return Err(unavailable());}
    let actual:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).optional()?.ok_or_else(unavailable)?;
    budget.check(actual.len()).map_err(RepositoryError::Validation)?;if actual!=database {return Err(unavailable());}
    let keys=account_custody_keys(tx,database,account,budget)?;
    if !keys.is_empty() {return verified_custody_view(tx,database,account,current,captured,budget);}
    if captured!=current {return Err(unavailable());}
    Ok(VerifiedCustodyView{tx,account_id:account.into(),current_owner:current.into(),captured_owner:current.into(),owners:vec![current.into()],transitions:Vec::new()})
}

/// Factual read-only custody check; it grants no transaction-bound authority.
/// The caller holds one consistent read snapshot and owns the work budget.
pub(crate) fn captured_owner_factual(conn:&Connection,database:&str,account:&str,current:&str,captured:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    if ![database,account,current,captured].iter().all(|v|text_ok(v)) {return Err(unavailable());}
    let actual:String=conn.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).optional()?.ok_or_else(unavailable)?;
    budget.check(actual.len()).map_err(RepositoryError::Validation)?;if actual!=database {return Err(unavailable());}
    let keys=account_custody_keys(conn,database,account,budget)?;
    if keys.is_empty() {return if current==captured {Ok(())} else {Err(unavailable())};}
    let (head,_,links)=chain(conn,database,account,budget)?.ok_or_else(unavailable)?;
    if head.current_owner!=current || !(current==captured || links.iter().any(|v|v.2.request.owner==captured)) {return Err(unavailable());}
    Ok(())
}

/// Bounded account-scoped inventory for existing account export/purge owners.
/// All indexed link keys must belong to the unique verified chain; no global
/// JSON scan, implicit cleanup or account authority is provided here.
pub(crate) fn account_custody_keys(tx:&Connection,database:&str,account:&str,budget:&mut ResetFenceBudget)->Result<Vec<String>,RepositoryError> {
    let head_name=head_key(database,account)?;let prefix=link_prefix(database,account)?;let upper=format!("{prefix}~");
    let current=chain(tx,database,account,budget)?;
    let expected:BTreeSet<String>=current.as_ref().map(|(_,_,links)|links.iter().map(|v|v.0.clone()).collect()).unwrap_or_default();
    let mut found=BTreeSet::new();let mut stmt=tx.prepare("SELECT key FROM fm_meta WHERE key>?1 AND key<?2 ORDER BY key")?;let mut rows=stmt.query(params![prefix,upper])?;
    while let Some(row)=rows.next()? {
        let key=row.get_ref(0)?.as_str().map_err(|_|unavailable())?;budget.check(key.len()).map_err(RepositoryError::Validation)?;
        if key.strip_prefix(&prefix).is_none_or(|v|!valid_sha256(v)) || !found.insert(key.to_owned()) {return Err(unavailable());}
    }
    drop(rows);drop(stmt);
    if found!=expected {return Err(unavailable());}
    let mut result:Vec<String>=found.into_iter().collect();if current.is_some(){result.push(head_name);}result.sort();Ok(result)
}
/// Explicit final-account purge seam. The caller must already hold the actual
/// account purge claim and original transaction, and roll back on any error.
pub(crate) fn purge_account_custody(tx:&Transaction<'_>,database:&str,account:&str,current_owner:&str,expected:&[String],budget:&mut ResetFenceBudget)->Result<usize,RepositoryError> {
    if !text_ok(current_owner) {return Err(unavailable());}
    if chain(tx,database,account,budget)?.as_ref().is_some_and(|(head,_,_)|head.current_owner!=current_owner) {return Err(unavailable());}
    let actual=account_custody_keys(tx,database,account,budget)?;
    if actual!=expected {return Err(unavailable());}
    for key in &actual {if tx.execute("DELETE FROM fm_meta WHERE key=?1",[key])?!=1 {return Err(unavailable());}}
    Ok(actual.len())
}
