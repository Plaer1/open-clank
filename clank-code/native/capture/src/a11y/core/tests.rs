use super::*;

#[derive(Clone)]
struct FNode { info: Info, kids: Vec<usize> }
struct Fake { nodes: Vec<FNode>, apps: Vec<usize>, calls: Vec<String>, ready: Result<(), BusErr>, result: Result<bool, BusErr>, retitle: Option<(usize, String)>, frozen: bool }
impl Fake {
    fn new() -> Fake { Fake { nodes: vec![], apps: vec![], calls: vec![], ready: Ok(()), result: Ok(true), retitle: None, frozen: false } }
    fn add(&mut self, role: &str, name: &str, parent: Option<usize>) -> usize {
        self.nodes.push(FNode { info: Info { role: role.into(), name: name.into(), showing: true, purpose_complete: true, ..Default::default() }, kids: vec![] });
        let n = self.nodes.len() - 1;
        if let Some(p) = parent { self.nodes[p].kids.push(n); }
        n
    }
    fn app(&mut self, name: &str, pid: u32) -> usize { let n = self.add("application", name, None); self.nodes[n].info.pid = Some(pid); self.nodes[n].info.start = Some(1000 + pid as u64); self.apps.push(n); n }
    fn win(&mut self, app: usize, title: &str, active: bool) -> usize { let w = self.add("frame", title, Some(app)); self.nodes[w].info.active = active; w }
    fn button(&mut self, parent: usize, name: &str) -> usize { let b = self.add("push button", name, Some(parent)); self.nodes[b].info.actions = vec!["click".into()]; b }
    fn field(&mut self, parent: usize, name: &str, value: &str) -> usize { let f = self.add("text", name, Some(parent)); self.nodes[f].info.editable = true; self.nodes[f].info.value = Some(value.into()); f }
    fn acted(&self) -> usize { self.calls.iter().filter(|c| !c.starts_with("read")).count() }
}
impl A11yBus for Fake {
    type Obj = usize;
    async fn ready(&mut self) -> Result<(), BusErr> { self.ready }
    async fn apps(&mut self) -> Result<Vec<usize>, BusErr> { Ok(self.apps.clone()) }
    async fn info(&mut self, o: &usize) -> Result<Info, BusErr> { self.nodes.get(*o).map(|n| n.info.clone()).ok_or(BusErr::Gone) }
    async fn children(&mut self, o: &usize) -> Result<Vec<usize>, BusErr> { self.nodes.get(*o).map(|n| n.kids.clone()).ok_or(BusErr::Gone) }
    async fn do_action(&mut self, o: &usize, i: usize) -> Result<bool, BusErr> { self.calls.push(format!("action {o} {i}")); if let Some((n, t)) = self.retitle.take() { self.nodes[n].info.name = t; } self.result }
    async fn set_text(&mut self, o: &usize, t: &str) -> Result<bool, BusErr> { self.calls.push(format!("set_text {o}")); if !self.frozen { self.nodes[*o].info.value = Some(t.into()); } self.result }
    async fn insert_text(&mut self, o: &usize, t: &str) -> Result<bool, BusErr> { self.calls.push(format!("insert {o}")); if !self.frozen { let v = self.nodes[*o].info.value.clone().unwrap_or_default(); self.nodes[*o].info.value = Some(format!("{v}{t}")); } self.result }
    async fn set_number(&mut self, o: &usize, v: f64) -> Result<bool, BusErr> { self.calls.push(format!("number {o} {v}")); if !self.frozen { self.nodes[*o].info.value = Some(v.to_string()); } self.result }
    async fn grab_focus(&mut self, o: &usize) -> Result<bool, BusErr> { self.calls.push(format!("focus {o}")); self.result }
    async fn select_text(&mut self, o: &usize, s: i32, e: i32) -> Result<bool, BusErr> { self.calls.push(format!("select {o} {s} {e}")); self.result }
}

fn sel_pid(p: u32) -> Sel { Sel { pid: Some(p), name: None } }
fn sel_name(n: &str) -> Sel { Sel { pid: None, name: Some(n.into()) } }
async fn state(c: &mut Core<Fake>, p: u32) -> Value { let v = c.handle(Req::AppState { v: 1, app: sel_pid(p), window_index: None }).await; assert_eq!(v["ok"], true, "{v}"); v }
async fn act(c: &mut Core<Fake>, s: &Value, el: &str, kind: ActKind, name: Option<&str>, text: Option<&str>) -> Value {
    c.handle(Req::Act { v: 1, snapshot_id: s["snapshot_id"].as_str().unwrap().into(), element: el.into(), action: kind, name: name.map(str::to_string), text: text.map(str::to_string), start: None, end: None }).await
}
/// The demo app: window 0 = [panel[button "Send", text "Name"], password field, list].
fn demo() -> (Fake, usize) {
    let mut f = Fake::new(); let a = f.app("Demo", 42); let w = f.win(a, "Main", true);
    let p = f.add("panel", "", Some(w)); f.button(p, "Send"); f.field(p, "Name", "alice");
    let pw = f.field(w, "Password", "hunter2"); f.nodes[pw].info.role = "password text".into();
    (f, w)
}
fn find(tree: &Value, name: &str) -> Option<String> {
    if tree["name"] == name { return tree["id"].as_str().map(str::to_string); }
    tree["children"].as_array()?.iter().find_map(|c| find(c, name))
}

#[tokio::test] async fn handshake_reports_each_readiness_state() {
    let (mut f, _) = demo(); f.ready = Err(BusErr::Disabled);
    let mut c = Core::new(f);
    assert_eq!(c.handle(Req::Handshake { v: 1 }).await, json!({"ok": false, "code": "accessibility_disabled"}));
    c.bus.ready = Err(BusErr::Unavailable); assert_eq!(c.handle(Req::Handshake { v: 1 }).await["code"], "bus_unavailable");
    c.bus.ready = Ok(()); let h = c.handle(Req::Handshake { v: 1 }).await;
    assert_eq!(h["accessibility"], "ready"); assert_eq!(h["limits"], json!({"nodes": 1200, "depth": 64, "value_chars": 500}));
}
#[tokio::test] async fn password_managers_are_never_listed_or_selected() {
    let (mut f, _) = demo(); let k = f.app("KeePassXC", 7); f.win(k, "Vault", true);
    let mut c = Core::new(f);
    let l = c.handle(Req::ListApps { v: 1 }).await; assert_eq!(l["apps"].as_array().unwrap().len(), 1); assert_eq!(l["apps"][0]["name"], "Demo");
    for s in [sel_pid(7), sel_name("keepass")] {
        assert_eq!(c.handle(Req::ListWindows { v: 1, app: s.clone() }).await["code"], "app_excluded");
        assert_eq!(c.handle(Req::AppState { v: 1, app: s, window_index: None }).await["code"], "app_excluded");
    }
    assert_eq!(c.handle(Req::ListWindows { v: 1, app: sel_name("nothing") }).await["code"], "no_such_app");
    assert_eq!(c.handle(Req::ListWindows { v: 1, app: Sel::default() }).await["code"], "invalid_request");
    assert_eq!(c.handle(Req::ListWindows { v: 1, app: Sel { pid: Some(1), name: Some("x".into()) } }).await["code"], "invalid_request");
    assert_eq!(c.bus.acted(), 0);
}
#[tokio::test] async fn tree_hides_secret_values_and_elides_empty_containers() {
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await;
    let text = s["tree"].to_string();
    assert!(!text.contains("hunter2"), "password value must not leave the helper"); assert!(text.contains("alice") && text.contains("Send"));
    assert_eq!(s["counts"]["hidden"], 1); assert!(s["tree"]["children"].as_array().unwrap().iter().any(|n| n["hidden"] == true));
    assert!(!text.contains("\"role\":\"panel\""), "the empty panel is elided");
    assert_eq!(s["app"], json!({"name": "Demo", "pid": 42})); assert_eq!(s["coordinate_space"], "window"); assert_eq!(s["target"]["backend"], "atspi/1"); assert_eq!(s["target"]["targetKind"], "window");
    assert_eq!(s["target"]["processStartIdentity"], "42:1042"); assert_eq!(s["target"]["targetGeneration"], s["snapshot_id"]);
}
#[tokio::test] async fn node_cap_is_1200_and_value_cap_is_500() {
    let mut f = Fake::new(); let a = f.app("Big", 1); let w = f.win(a, "Main", true);
    for i in 0..1500 { let n = f.add("list item", &format!("row {i}"), Some(w)); f.nodes[n].info.value = Some("v ".repeat(400)); }
    let mut c = Core::new(f); let s = state(&mut c, 1).await;
    assert_eq!(s["counts"]["nodes"], 1200); assert_eq!(s["truncated"]["nodes"], true); assert_eq!(s["truncated"]["depth"], false);
    assert_eq!(s["tree"]["children"].as_array().unwrap().len(), 1199);
    assert_eq!(s["tree"]["children"][0]["value"].as_str().unwrap().chars().count(), 500);
}
#[tokio::test] async fn depth_cap_is_64() {
    let mut f = Fake::new(); let a = f.app("Deep", 1); let mut cur = f.win(a, "Main", true);
    for i in 0..70 { cur = f.add("filler", &format!("n{i}"), Some(cur)); }
    let mut c = Core::new(f); let s = state(&mut c, 1).await;
    assert_eq!(s["truncated"]["depth"], true); assert_eq!(s["counts"]["nodes"], 65, "window (depth 0) down to depth 64");
}
#[tokio::test] async fn click_secondary_set_value_type_and_focus_are_action_first() {
    let (mut f, _) = demo(); let sec = f.apps[0]; let _ = sec;
    let b = f.nodes.iter().position(|n| n.info.name == "Send").unwrap(); f.nodes[b].info.actions = vec!["click".into(), "Menu".into()];
    let mut c = Core::new(f); let s = state(&mut c, 42).await;
    let (send, name) = (find(&s["tree"], "Send").unwrap(), find(&s["tree"], "Name").unwrap());
    let r = act(&mut c, &s, &send, ActKind::Click, None, None).await;
    assert_eq!((r["ok"].clone(), r["outcome"].clone(), r["window_changed"].clone()), (json!(true), json!("performed"), json!(false)), "{r}");
    assert_eq!(act(&mut c, &s, &send, ActKind::Secondary, Some("menu"), None).await["ok"], true);
    assert_eq!(act(&mut c, &s, &send, ActKind::Secondary, Some("nope"), None).await["code"], "not_actionable");
    assert_eq!(act(&mut c, &s, &name, ActKind::SetValue, None, Some("bob")).await["ok"], true);
    assert_eq!(act(&mut c, &s, &name, ActKind::Type, None, Some("x")).await["ok"], true);
    assert_eq!(act(&mut c, &s, &name, ActKind::Focus, None, None).await["ok"], true);
    assert_eq!(c.bus.calls.iter().filter(|x| x.starts_with("action")).count(), 2);
    let r = act(&mut c, &s, &name, ActKind::Click, None, None).await; assert_eq!(r["code"], "not_actionable", "a text field has no action: typed refusal, no pointer fallback");
    let r = act(&mut c, &s, &send, ActKind::Type, None, Some("x")).await; assert_eq!(r["code"], "not_actionable");
    assert!(!r.to_string().contains("\"x\""), "typed text is never echoed");
}
#[tokio::test] async fn numeric_value_is_set_through_the_value_interface() {
    let mut f = Fake::new(); let a = f.app("Spin", 3); let w = f.win(a, "Main", true); let n = f.add("spin button", "Volume", Some(w)); f.nodes[n].info.numeric = true;
    let mut c = Core::new(f); let s = state(&mut c, 3).await; let id = find(&s["tree"], "Volume").unwrap();
    assert_eq!(act(&mut c, &s, &id, ActKind::SetValue, None, Some("7.5")).await["ok"], true);
    assert_eq!(act(&mut c, &s, &id, ActKind::SetValue, None, Some("abc")).await["code"], "invalid_request");
    assert_eq!(c.bus.calls, vec!["number 2 7.5".to_string()]);
}
#[tokio::test] async fn a_stale_element_is_refused_before_any_action() {
    for change in 0..4 {
        let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
        let b = c.bus.nodes.iter().position(|n| n.info.name == "Send").unwrap();
        match change {
            0 => c.bus.nodes[b].info.name = "Cancel".into(),
            1 => c.bus.nodes[b].info.actions = vec!["press".into()],
            2 => c.bus.nodes[b].info.role = "check box".into(),
            _ => { let p = c.bus.nodes.iter().position(|n| n.kids.contains(&b)).unwrap(); c.bus.nodes[p].kids.clear(); }
        }
        let r = act(&mut c, &s, &send, ActKind::Click, None, None).await;
        assert_eq!(r["code"], "stale_element", "change {change}: {r}"); assert_eq!(c.bus.acted(), 0);
    }
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await;
    assert_eq!(act(&mut c, &s, "9.9", ActKind::Click, None, None).await["code"], "no_such_element");
    assert_eq!(act(&mut c, &s, "a.b", ActKind::Click, None, None).await["code"], "invalid_request");
}
#[tokio::test] async fn a_changed_or_missing_window_refuses_and_never_retargets() {
    let (mut f, w) = demo(); let a = f.apps[0]; let w2 = f.win(a, "Other", false); f.button(w2, "Send");
    let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
    c.bus.nodes[w].info.name = "Other".into(); // window 0 is now called Other, window 1 is also Other: index 0 no longer matches what was read
    let r = act(&mut c, &s, &send, ActKind::Click, None, None).await; assert_eq!(r["code"], "window_changed"); assert_eq!(c.bus.acted(), 0);
    c.bus.nodes[w].info.name = "Main".into(); let ai = a_idx(&c); c.bus.nodes[ai].kids.retain(|k| *k != w);
    assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["code"], "window_changed");
    assert_eq!(c.bus.acted(), 0);
}
fn a_idx(c: &Core<Fake>) -> usize { c.bus.apps[0] }
#[tokio::test] async fn a_restarted_process_is_target_gone_not_a_new_target() {
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
    let app = c.bus.apps[0]; c.bus.nodes[app].info.start = Some(5);
    assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["code"], "target_gone"); assert_eq!(c.bus.acted(), 0);
}
#[tokio::test] async fn snapshots_expire_and_unknown_ids_are_refused() {
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
    assert_eq!(c.handle(Req::Act { v: 1, snapshot_id: "nope".into(), element: send.clone(), action: ActKind::Click, name: None, text: None, start: None, end: None }).await["code"], "snapshot_expired");
    c.snaps[0].made = Instant::now() - TTL - Duration::from_secs(1);
    assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["code"], "snapshot_expired");
    assert!(c.snaps.is_empty()); assert_eq!(c.bus.acted(), 0);
}
#[tokio::test] async fn a_failed_action_is_reported_and_never_repeated() {
    let (mut f, _) = demo(); f.result = Ok(false); let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
    assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["code"], "action_failed"); assert_eq!(c.bus.acted(), 1);
    c.bus.result = Err(BusErr::Timeout); assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["code"], "timeout"); assert_eq!(c.bus.acted(), 2);
}
#[tokio::test] async fn an_action_that_changes_the_window_is_flagged_not_repeated() {
    let (mut f, w) = demo(); f.retitle = Some((w, "Main (sent)".into()));
    let mut c = Core::new(f); let s = state(&mut c, 42).await; let send = find(&s["tree"], "Send").unwrap();
    let r = act(&mut c, &s, &send, ActKind::Click, None, None).await;
    assert_eq!((r["ok"].clone(), r["window_changed"].clone()), (json!(true), json!(true)), "{r}"); assert_eq!(c.bus.acted(), 1);
    assert_eq!(r["target_after"]["targetGeneration"], s["snapshot_id"]);
}
#[tokio::test] async fn serve_parses_strictly_and_replies_one_line_per_request() {
    let (f, _) = demo(); let (tx, mut rx) = mpsc::channel(16);
    for l in [r#"{"v":1,"op":"handshake"}"#, r#"{"v":2,"op":"handshake"}"#, r#"{"v":1,"op":"handshake","x":1}"#, r#"{"v":1,"op":"drag"}"#, r#"{"op":"list_apps"}"#, r#"{"v":1,"op":"list_apps"}"#] {
        tx.send(Some(format!("{l}\n").into_bytes())).await.unwrap();
    }
    tx.send(None).await.unwrap();
    let mut outs = vec![]; serve(f, &mut rx, &mut |s| { outs.push(s); true }, &AtomicBool::new(false)).await;
    assert_eq!(outs.len(), 6);
    assert!(outs[0].contains("\"ready\"")); for o in &outs[1..5] { assert_eq!(o, r#"{"code":"invalid_request","ok":false}"#); }
    assert!(outs[5].contains("\"apps\""));
}

#[tokio::test] async fn set_value_and_type_report_what_was_read_back() {
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await; let name = find(&s["tree"], "Name").unwrap();
    let r = act(&mut c, &s, &name, ActKind::SetValue, None, Some("bob")).await; assert_eq!(r["verified"], true, "{r}");
    let r = act(&mut c, &s, &name, ActKind::Type, None, Some("X")).await; assert_eq!(r["verified"], true, "value became bobX: {r}");
    c.bus.frozen = true; // the application ignores the write: the read-back shows it
    let r = act(&mut c, &s, &name, ActKind::SetValue, None, Some("zzz")).await; assert_eq!(r["verified"], false);
    let r = act(&mut c, &s, &name, ActKind::Type, None, Some("Q")).await; assert_eq!(r["verified"], false);
    let send = find(&s["tree"], "Send").unwrap();
    assert_eq!(act(&mut c, &s, &send, ActKind::Click, None, None).await["verified"], "unknown", "a click is never claimed verified");
    // an unreadable value is unknown, not true
    let n = c.bus.nodes.iter().position(|n| n.info.name == "Name").unwrap(); c.bus.nodes[n].info.value = None; c.bus.frozen = true;
    assert_eq!(act(&mut c, &s, &name, ActKind::SetValue, None, Some("k")).await["verified"], "unknown" );
}
#[tokio::test] async fn numeric_set_is_verified_by_value() {
    let mut f = Fake::new(); let a = f.app("Spin", 3); let w = f.win(a, "Main", true); let n = f.add("spin button", "Volume", Some(w)); f.nodes[n].info.numeric = true; f.nodes[n].info.value = Some("1".into());
    let mut c = Core::new(f); let s = state(&mut c, 3).await; let id = find(&s["tree"], "Volume").unwrap();
    assert_eq!(act(&mut c, &s, &id, ActKind::SetValue, None, Some("7.5")).await["verified"], true);
    c.bus.frozen = true; // the application ignores the write
    assert_eq!(act(&mut c, &s, &id, ActKind::SetValue, None, Some("9")).await["verified"], false);
}
#[tokio::test] async fn text_selection_goes_through_the_text_interface() {
    let (f, _) = demo(); let mut c = Core::new(f); let s = state(&mut c, 42).await; let name = find(&s["tree"], "Name").unwrap(); let send = find(&s["tree"], "Send").unwrap();
    async fn sel(c: &mut Core<Fake>, id: &str, el: String, st: Option<u32>, en: Option<u32>) -> Value { c.handle(Req::Act { v: 1, snapshot_id: id.into(), element: el, action: ActKind::SelectText, name: None, text: None, start: st, end: en }).await }
    let sid = s["snapshot_id"].as_str().unwrap().to_string();
    let r = sel(&mut c, &sid, name.clone(), Some(1), Some(3)).await; assert_eq!((r["ok"].clone(), r["verified"].clone()), (json!(true), json!("unknown")), "{r}");
    assert!(c.bus.calls.contains(&"select 3 1 3".to_string()) || c.bus.calls.iter().any(|x| x.starts_with("select ")));
    for (st, en) in [(None, Some(2)), (Some(3), Some(1)), (Some(0), None)] { assert_eq!(sel(&mut c, &sid, name.clone(), st, en).await["code"], "invalid_request"); }
    assert_eq!(sel(&mut c, &sid, send, Some(0), Some(1)).await["code"], "not_actionable", "a button has no text");
    assert_eq!(c.bus.acted(), 1);
    let r = act(&mut c, &s, &name, ActKind::Click, None, None).await; assert_eq!(r["code"], "not_actionable");
}
#[tokio::test] async fn app_state_reports_modal_nesting_occlusion_and_unnamed_controls() {
    let mut f = Fake::new(); let a = f.app("Demo", 9); let w = f.win(a, "Main", true); let d = f.win(a, "Confirm", false); f.nodes[d].info.modal = true; f.button(d, "OK");
    let b = f.add("push button", "", Some(w)); f.nodes[b].info.actions = vec!["click".into()];
    let mut c = Core::new(f);
    let main = c.handle(Req::AppState { v: 1, app: sel_pid(9), window_index: Some(0) }).await; assert_eq!(main["ok"], true, "{main}");
    assert_eq!(main["window"]["modal"], false); assert_eq!(main["window"]["modal_open"], true); assert_eq!(main["window"]["blocked_by_modal"], true);
    assert_eq!(main["window"]["occlusion"], "unknown", "reported as unknown, not omitted");
    assert_eq!(main["controls"]["unnamed_actionable"], 1); assert!(main["controls"]["missing"].as_str().unwrap().starts_with("unknown"));
    assert_eq!(main["limits"], json!({"max_nodes": 1200, "max_depth": 64, "deepest_seen": 1}));
    let dlg = c.handle(Req::AppState { v: 1, app: sel_pid(9), window_index: Some(1) }).await; assert_eq!((dlg["window"]["modal"].clone(), dlg["window"]["blocked_by_modal"].clone()), (json!(true), json!(false)));
}

#[tokio::test] async fn selected_chooser_entry_requires_current_ancestry_and_exact_readback() {
    let mut f = Fake::new(); let a = f.app("Editor", 42); let w = f.win(a, "Open", true);
    f.nodes[a].info.toolkit = "gtk".into(); f.nodes[a].info.toolkit_version = "3.24.52".into(); f.nodes[a].info.locale = "C.UTF-8".into();
    f.nodes[w].info.role = "file chooser".into();
    let panel = f.add("panel", "Location", Some(w)); let field = f.field(panel, "File name", "");
    f.nodes[field].info.location_marker = true;
    let search = f.field(panel, "Search", "");
    let mut c = Core::new(f); let observed = state(&mut c, 42).await;
    let sid = observed["snapshot_id"].as_str().unwrap().to_string();
    let el = find(&observed["tree"], "File name").unwrap();
    let path = "/tmp/openclank-selected-abc/photo.png";
    assert_eq!(observed["tree"].to_string().matches("file_location").count(), 1);
    let search_id = find(&observed["tree"], "Search").unwrap();
    let refused = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid.clone(), element: search_id, path: path.into() }).await;
    assert_eq!(refused["code"], "not_actionable"); assert_eq!(c.bus.acted(), 0);
    c.bus.nodes[search].info.location_marker = true;
    let duplicate = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid.clone(), element: el.clone(), path: path.into() }).await;
    assert_eq!(duplicate["code"], "not_actionable"); assert_eq!(c.bus.acted(), 0);
    c.bus.nodes[search].info.location_marker = false;
    c.bus.nodes[search].info.purpose_complete = false;
    let incomplete = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid.clone(), element: el.clone(), path: path.into() }).await;
    assert_eq!(incomplete["code"], "not_actionable"); assert_eq!(c.bus.acted(), 0);
    c.bus.nodes[search].info.purpose_complete = true;
    c.bus.nodes[panel].info.name = "Changed".into();
    let stale = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid.clone(), element: el.clone(), path: path.into() }).await;
    assert_eq!(stale["code"], "stale_element"); assert_eq!(c.bus.acted(), 0);
    c.bus.nodes[panel].info.name = "Location".into();
    let entered = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid.clone(), element: el.clone(), path: path.into() }).await;
    assert_eq!(entered, json!({"ok": true, "outcome": "entered"}));
    assert!(!entered.to_string().contains(path));
    c.bus.frozen = true; c.bus.nodes[field].info.value = Some("different".into());
    let unknown = c.handle(Req::ChooserEnter { v: 1, snapshot_id: sid, element: el, path: path.into() }).await;
    assert_eq!(unknown["code"], "outcome_unknown");
}
