//! Standing-guidance requirement field: identity, validation and round-trip cases.
use super::*;

fn refs()->PolicyPublicationRefs{
    let r=|e:i64|PolicyProjectionRef{epoch:e,fingerprint:"f".repeat(64),transition_id:format!("t{e}")};
    PolicyPublicationRefs{account:r(1),global:r(2)}
}

fn req()->GuidanceRequirement{GuidanceRequirement{version:1,effect:GuidanceEffect::PublishFile,deny:GuidanceDeny{audience:GuidanceAudience::Public}}}

fn request(requirement:Option<GuidanceRequirement>)->StandingGuidanceReviewRequest{
    let mut r=StandingGuidanceReviewRequest{version:1,owner:"o".into(),workspace_id:"w".into(),account_id:"a".into(),curated_id:"c".into(),block_id:"b".into(),predecessor_receipt_id:"p".into(),expected_revision:3,value_sha256:"ab".repeat(32),decision:"approve".into(),request_key:String::new(),policy_projection:refs(),requirement};
    r.request_key=standing_guidance_key(&r).unwrap();r
}

fn parse(raw:&str)->Result<StandingGuidanceReviewRequest,serde_json::Error>{serde_json::from_str(raw)}

/// Literals captured from the tree BEFORE the requirement field existed (same request, no field).
const BASELINE_JSON:&str=r#"{"version":1,"owner":"o","workspace_id":"w","account_id":"a","curated_id":"c","block_id":"b","predecessor_receipt_id":"p","expected_revision":3,"value_sha256":"abababababababababababababababababababababababababababababababab","decision":"approve","request_key":"s10purpose_6103c94aece89a7c4ad6f9713332086be9b4814965b7c84dfd1e6904fb354013","policy_projection":{"account":{"epoch":1,"fingerprint":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","transition_id":"t1"},"global":{"epoch":2,"fingerprint":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","transition_id":"t2"}}}"#;
const BASELINE_SHA:&str="05c5f00b902d8a5c0180b04bb26003baad951d99c57955d6550ce902afa11963";
const BASELINE_KEY:&str="s10purpose_6103c94aece89a7c4ad6f9713332086be9b4814965b7c84dfd1e6904fb354013";
const BASELINE_ID:&str="cs_s10_purpose_2f46efa944e4d844f69c675a365d14d77de5bdacf8dd239f3932a51ff691e53f";

#[test]
fn no_requirement_is_byte_and_id_identical_to_pre_field_code() {
    let r=request(None);
    let raw=serde_json::to_string(&r).unwrap();
    assert_eq!(raw,BASELINE_JSON);
    assert_eq!(sha256_hex(raw.as_bytes()),BASELINE_SHA);
    assert_eq!(r.request_key,BASELINE_KEY);
    assert_eq!(standing_guidance_id(&r).unwrap(),BASELINE_ID);
    validate_standing_guidance_request(&r).unwrap();
    // A pre-field request parses and re-serialises to the same bytes.
    assert_eq!(serde_json::to_string(&parse(BASELINE_JSON).unwrap()).unwrap(),BASELINE_JSON);
}

#[test]
fn valid_requirement_round_trips_byte_exact_and_changes_identity() {
    let with=request(Some(req()));let without=request(None);
    validate_standing_guidance_request(&with).unwrap();
    let raw=serde_json::to_string(&with).unwrap();
    assert!(raw.ends_with(r#","requirement":{"version":1,"effect":"publish_file","deny":{"audience":"public"}}}"#),"{raw}");
    assert_eq!(serde_json::to_string(&parse(&raw).unwrap()).unwrap(),raw);
    assert_eq!(parse(&raw).unwrap().requirement,Some(req()));
    assert_ne!(with.request_key,without.request_key);
    assert_ne!(standing_guidance_id(&with).unwrap(),standing_guidance_id(&without).unwrap());
    assert_ne!(sha256_hex(raw.as_bytes()),BASELINE_SHA);
    // Stripping the requirement from an approved request breaks its key.
    let mut stripped=with.clone();stripped.requirement=None;
    assert!(validate_standing_guidance_request(&stripped).is_err());
    // A requirement is only valid with approve.
    let mut w=request(Some(req()));w.decision="withdraw".into();w.request_key=standing_guidance_key(&w).unwrap();
    assert!(validate_standing_guidance_request(&w).is_err());
}

#[test]
fn invalid_requirement_shapes_are_refused() {
    let base=serde_json::to_value(request(Some(req()))).unwrap();
    let with=|f:&dyn Fn(&mut serde_json::Value)|{let mut v=base.clone();f(&mut v["requirement"]);v.to_string()};
    for bad in [
        with(&|q|q["effect"]=json!("delete_file")),
        with(&|q|q["deny"]["audience"]=json!("owner")),
        with(&|q|q["effect"]=json!(7)),
        with(&|q|q["deny"]["audience"]=json!(["public"])),
        with(&|q|q["version"]=json!("1")),
        with(&|q|q["deny"]=json!("public")),
        with(&|q|q["extra"]=json!(1)),
        with(&|q|q["deny"]["extra"]=json!(1)),
        with(&|q|{q.as_object_mut().unwrap().remove("deny");}),
        with(&|q|*q=json!(null)).replace("null","[]"),
    ] {assert!(parse(&bad).is_err(),"accepted {bad}");}
    // Version 2 parses but validation refuses it.
    let v2=parse(&with(&|q|q["version"]=json!(2))).unwrap();
    assert!(validate_standing_guidance_request(&v2).is_err());
    // Extra key beside the requirement is still refused at the request level.
    let mut top=base.clone();top["other"]=json!(1);assert!(parse(&top.to_string()).is_err());
}

#[test]
fn item_serialises_requirement_only_when_present_and_differs_for_revalidation() {
    let item=|q:Option<GuidanceRequirement>|StandingGuidanceItem{curated_id:"c".into(),block_id:"b".into(),revision:4,receipt_id:"r".into(),receipt_sha256:"s".into(),value_sha256:"v".into(),text:"t".into(),requirement:q};
    let plain=serde_json::to_string(&item(None)).unwrap();
    assert_eq!(plain,r#"{"curated_id":"c","block_id":"b","revision":4,"receipt_id":"r","receipt_sha256":"s","value_sha256":"v","text":"t"}"#.replace(", ",","));
    let with=serde_json::to_string(&item(Some(req()))).unwrap();
    assert_eq!(with,format!("{},\"requirement\":{{\"version\":1,\"effect\":\"publish_file\",\"deny\":{{\"audience\":\"public\"}}}}}}",&plain[..plain.len()-1]));
    // Revalidation compares current_guidance_item(..) != *item; derived equality must see the requirement.
    assert_ne!(item(None),item(Some(req())));
    assert_eq!(item(Some(req())),item(Some(req())));
    // Items without the key (older Hosts) still parse.
    assert_eq!(serde_json::from_str::<StandingGuidanceItem>(&plain).unwrap(),item(None));
}

#[test]
fn preview_and_status_carry_the_field_optionally() {
    let p=r#"{"version":1,"owner":"o","workspace_id":"w","account_id":"a","curated_id":"c","decision":"approve","policy_projection":{"account":{"epoch":1,"fingerprint":"f","transition_id":"t"},"global":{"epoch":1,"fingerprint":"f","transition_id":"t"}}}"#;
    assert!(serde_json::from_str::<StandingGuidancePreview>(p).unwrap().requirement.is_none());
    let mut v:serde_json::Value=serde_json::from_str(p).unwrap();
    v["requirement"]=serde_json::to_value(req()).unwrap();
    assert_eq!(serde_json::from_value::<StandingGuidancePreview>(v).unwrap().requirement,Some(req()));
}
