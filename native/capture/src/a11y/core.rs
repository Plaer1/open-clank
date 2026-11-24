//! Session rules for `--a11y-v1`. Everything here is bus-agnostic; refusals make no action call.
use super::{tree, A11yBus, BusErr, Info, MAX_DEPTH, MAX_NODES, MAX_TEXT};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{collections::HashMap, sync::atomic::{AtomicBool, Ordering}, time::{Duration, Instant}};
use tokio::{sync::mpsc, time::timeout};

pub const TTL: Duration = Duration::from_secs(120);
const MAX_SNAPS: usize = 8;
const REQ_TIMEOUT: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(100);
const WINDOW_ROLES: &[&str] = &["frame", "window", "dialog", "alert", "file chooser", "color chooser", "font chooser"];
const CLICK_NAMES: &[&str] = &["click", "press", "activate", "toggle", "jump", "select", "open", "check", "uncheck"];

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(deny_unknown_fields)]
pub struct Sel { pid: Option<u32>, name: Option<String> }
#[derive(Deserialize, Clone, Copy, PartialEq, Debug)]
#[serde(rename_all = "snake_case")]
pub enum ActKind { Click, Secondary, SetValue, Type, Focus, SelectText }
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Req {
    Handshake { v: u8 },
    ListApps { v: u8 },
    ListWindows { v: u8, app: Sel },
    AppState { v: u8, app: Sel, window_index: Option<usize> },
    Act { v: u8, snapshot_id: String, element: String, action: ActKind, name: Option<String>, text: Option<String>, start: Option<u32>, end: Option<u32> },
    ChooserEnter { v: u8, snapshot_id: String, element: String, path: String },
}
impl Req { fn v(&self) -> u8 { match self { Req::Handshake { v } | Req::ListApps { v } | Req::ListWindows { v, .. } | Req::AppState { v, .. } | Req::Act { v, .. } | Req::ChooserEnter { v, .. } => *v } } }

#[derive(Clone, PartialEq, Debug)]
struct Sig { role: String, name: String, id: String, actions: Vec<String> }
impl Sig { fn of(i: &Info) -> Sig { Sig { role: i.role.clone(), name: i.name.clone(), id: i.id.clone(), actions: i.actions.clone() } } }
struct Snap { id: String, pid: u32, start: u64, app_name: String, window: usize, win: (String, String), sigs: HashMap<Vec<usize>, Sig>, location_path: Option<Vec<usize>>, made: Instant }

fn bad(code: &str) -> Value { json!({"ok": false, "code": code}) }
fn bus_code(e: BusErr) -> &'static str {
    match e { BusErr::Unavailable => "bus_unavailable", BusErr::Disabled => "accessibility_disabled", BusErr::Timeout => "timeout", BusErr::Gone => "stale_element", BusErr::Failed => "action_failed" }
}
fn is_window(i: &Info) -> bool { WINDOW_ROLES.contains(&i.role.as_str()) }
// GTK 3.24's documented OPEN location entry emits this localized marker.
// This finite profile is source-backed, not an unforgeable widget class proof.
fn supported_gtk_location(app: &Info, win: &Info, field: &Info) -> bool {
    let mut unvalued = field.clone(); unvalued.value = None;
    app.toolkit == "gtk" && app.toolkit_version.strip_prefix("3.24.").is_some_and(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
        && matches!(app.locale.as_str(), "C" | "C.UTF-8") && win.role == "file chooser"
        && field.role == "text" && field.showing && field.editable && field.location_marker && !tree::hide_value(&unvalued)
}
fn path_text(p: &[usize]) -> String { if p.is_empty() { "root".into() } else { p.iter().map(|n| n.to_string()).collect::<Vec<_>>().join(".") } }
fn parse_path(s: &str) -> Option<Vec<usize>> {
    if s == "root" { return Some(vec![]); }
    let p: Option<Vec<usize>> = s.split('.').map(|x| if x.len() <= 6 && !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit()) { x.parse().ok() } else { None }).collect();
    p.filter(|p| p.len() <= MAX_DEPTH)
}
fn token() -> String {
    use std::io::Read;
    let mut b = [0u8; 8];
    let ok = std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).is_ok();
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0);
    if !ok { b = t.to_le_bytes(); }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// `ObservedTargetV1` (plan: stable handle + process-start identity + generation). Owner/session fields are added by the Host.
fn target(pid: u32, start: u64, window: usize, win: &Info, generation: &str) -> Value {
    json!({"version": 1, "backend": "atspi/1", "targetKind": "window", "stableTargetHandle": format!("atspi:{pid}:{start}:{window}"),
           "processStartIdentity": format!("{pid}:{start}"), "targetGeneration": generation,
           "focusState": if win.active { "active" } else if win.showing { "showing" } else { "background" }, "permission": "accessibility",
           "actionCapabilities": ["click", "secondary", "set_value", "type", "focus", "select_text"]})
}

pub struct Core<B: A11yBus> { bus: B, snaps: Vec<Snap> }

struct Rec { info: Info, path: Vec<usize>, kids: Vec<usize> }

impl<B: A11yBus> Core<B> {
    pub fn new(bus: B) -> Self { Core { bus, snaps: vec![] } }

    async fn find_app(&mut self, sel: &Sel) -> Result<(B::Obj, Info), &'static str> {
        if sel.pid.is_some() == sel.name.is_some() { return Err("invalid_request"); }
        let needle = sel.name.as_deref().map(str::to_lowercase);
        let apps = self.bus.apps().await.map_err(bus_code)?;
        for o in apps {
            let Ok(i) = self.bus.info(&o).await else { continue };
            let hit = sel.pid.is_some_and(|p| i.pid == Some(p)) || needle.as_deref().is_some_and(|n| !n.is_empty() && i.name.to_lowercase().contains(n));
            if hit { return if tree::excluded(&i.name, &i.exe) { Err("app_excluded") } else { Ok((o, i)) }; }
        }
        Err("no_such_app")
    }

    async fn windows(&mut self, app: &B::Obj) -> Result<Vec<(B::Obj, Info)>, &'static str> {
        let mut out = vec![];
        for c in self.bus.children(app).await.map_err(bus_code)? {
            if let Ok(i) = self.bus.info(&c).await { if is_window(&i) { out.push((c, i)); } }
        }
        Ok(out)
    }

    async fn list_apps(&mut self) -> Value {
        let apps = match self.bus.apps().await { Ok(a) => a, Err(e) => return bad(bus_code(e)) };
        let mut list = vec![];
        for o in apps {
            let Ok(i) = self.bus.info(&o).await else { continue };
            if tree::excluded(&i.name, &i.exe) { continue; }
            let n = self.windows(&o).await.map(|w| w.len()).unwrap_or(0);
            list.push(json!({"name": i.name, "pid": i.pid, "toolkit": i.toolkit, "windows": n}));
        }
        json!({"ok": true, "apps": list})
    }

    async fn list_windows(&mut self, sel: &Sel) -> Value {
        let (app, _) = match self.find_app(sel).await { Ok(x) => x, Err(c) => return bad(c) };
        match self.windows(&app).await {
            Ok(w) => json!({"ok": true, "windows": w.iter().enumerate().map(|(n, (_, i))| json!({"index": n, "title": i.name, "role": i.role, "active": i.active, "showing": i.showing, "modal": i.modal, "bounds": i.bounds})).collect::<Vec<_>>()}),
            Err(c) => bad(c),
        }
    }

    async fn app_state(&mut self, sel: &Sel, window_index: Option<usize>) -> Value {
        let (app, ai) = match self.find_app(sel).await { Ok(x) => x, Err(c) => return bad(c) };
        let (Some(pid), Some(start)) = (ai.pid, ai.start) else { return bad("no_such_app") };
        let wins = match self.windows(&app).await { Ok(w) => w, Err(c) => return bad(c) };
        let idx = match window_index {
            Some(n) if n < wins.len() => n,
            Some(_) => return bad("no_such_window"),
            None => match wins.iter().position(|(_, i)| i.active).or_else(|| wins.iter().position(|(_, i)| i.showing)).or(if wins.is_empty() { None } else { Some(0) }) {
                Some(n) => n, None => return bad("no_such_window"),
            },
        };
        let (wobj, winfo) = wins[idx].clone();
        let modal_open = wins.iter().any(|(_, i)| i.modal);
        let id = token();
        // Pre-order walk with explicit caps: 1,200 visited nodes, depth 64.
        let mut recs: Vec<Rec> = vec![];
        let mut stack: Vec<(B::Obj, Vec<usize>, Option<usize>)> = vec![(wobj, vec![], None)];
        let (mut cut_nodes, mut cut_depth, mut incomplete) = (false, false, false);
        while let Some((o, path, parent)) = stack.pop() {
            if recs.len() >= MAX_NODES { cut_nodes = true; break; }
            let info = match self.bus.info(&o).await { Ok(i) => i, Err(_) => { incomplete = true; continue; } };
            if !info.purpose_complete { incomplete = true; }
            let me = recs.len();
            if let Some(p) = parent { recs[p].kids.push(me); }
            let kids = match self.bus.children(&o).await { Ok(k) => k, Err(_) => { incomplete = true; vec![] } };
            if path.len() >= MAX_DEPTH { if !kids.is_empty() { cut_depth = true; } } else {
                for (n, k) in kids.into_iter().enumerate().rev() { let mut p = path.clone(); p.push(n); stack.push((k, p, Some(me))); }
            }
            recs.push(Rec { info, path, kids: vec![] });
        }
        // Kids were pushed in reverse so they pop in order; restore sibling order by sorting on path.
        for i in 0..recs.len() { let mut k = std::mem::take(&mut recs[i].kids); k.sort_by(|a, b| recs[*a].path.cmp(&recs[*b].path)); recs[i].kids = k; }
        if recs.is_empty() { return bad("bus_unavailable"); }
        let candidates: Vec<_> = recs.iter().filter(|r| supported_gtk_location(&ai, &winfo, &r.info)).map(|r| r.path.clone()).collect();
        let location_path = if !incomplete && !cut_nodes && !cut_depth && candidates.len() == 1 { Some(candidates[0].clone()) } else { None };
        let mut hidden = 0usize;
        let mut sigs = HashMap::new();
        for r in &recs { sigs.insert(r.path.clone(), Sig::of(&r.info)); }
        fn build(recs: &[Rec], i: usize, snap: &str, hidden: &mut usize, root: bool, location: Option<&[usize]>) -> Vec<Value> {
            let r = &recs[i];
            let kids: Vec<Value> = r.kids.iter().flat_map(|k| build(recs, *k, snap, hidden, false, location)).collect();
            if !root && tree::trivial(&r.info) { return kids; }
            let (value, was_hidden) = tree::shown_value(&r.info);
            if was_hidden { *hidden += 1; }
            let mut n = json!({"id": path_text(&r.path), "role": r.info.role});
            let o = n.as_object_mut().unwrap();
            if !r.info.name.is_empty() { o.insert("name".into(), json!(r.info.name)); }
            if !r.info.actions.is_empty() { o.insert("actions".into(), json!(r.info.actions)); }
            if let Some(v) = value { o.insert("value".into(), json!(v)); }
            if was_hidden { o.insert("hidden".into(), json!(true)); }
            if r.info.editable { o.insert("editable".into(), json!(true)); }
            if location.is_some_and(|path| path == r.path.as_slice()) { o.insert("field_purpose".into(), json!("file_location")); }
            if let Some(b) = r.info.bounds { o.insert("bounds".into(), json!(b)); }
            if !kids.is_empty() { o.insert("children".into(), json!(kids)); }
            vec![n]
        }
        let rendered = build(&recs, 0, &id, &mut hidden, true, location_path.as_deref()).into_iter().next().unwrap_or(Value::Null);
        if self.snaps.len() >= MAX_SNAPS { self.snaps.remove(0); }
        self.snaps.push(Snap { id: id.clone(), pid, start, app_name: ai.name.clone(), window: idx, win: (winfo.role.clone(), winfo.name.clone()), sigs, location_path, made: Instant::now() });
        json!({"ok": true, "snapshot_id": id, "target": target(pid, start, idx, &winfo, &id), "app": {"name": ai.name, "pid": pid},
               "window": {"index": idx, "title": winfo.name, "bounds": winfo.bounds, "active": winfo.active, "showing": winfo.showing, "windows": wins.len(),
                          "modal": winfo.modal, "modal_open": modal_open, "blocked_by_modal": modal_open && !winfo.modal, "occlusion": "unknown"},
               "limits": {"max_nodes": MAX_NODES, "max_depth": MAX_DEPTH, "deepest_seen": recs.iter().map(|r| r.path.len()).max().unwrap_or(0)},
               "controls": {"unnamed_actionable": recs.iter().filter(|r| !r.info.actions.is_empty() && r.info.name.is_empty()).count(), "missing": "unknown: the tree lists only what the application exposes"},
               "coordinate_space": "window", "tree": rendered, "truncated": {"nodes": cut_nodes, "depth": cut_depth}, "counts": {"nodes": recs.len(), "hidden": hidden}})
    }

    async fn act(&mut self, snapshot_id: &str, element: &str, kind: ActKind, name: Option<String>, text: Option<String>, range: (Option<u32>, Option<u32>)) -> Value {
        let Some(path) = parse_path(element) else { return bad("invalid_request") };
        match kind {
            ActKind::Secondary if name.as_deref().is_none_or(|n| n.is_empty() || n.len() > 64) => return bad("invalid_request"),
            ActKind::SetValue | ActKind::Type if text.as_deref().is_none_or(|t| t.is_empty() || t.chars().count() > MAX_TEXT || t.contains('\0')) => return bad("invalid_request"),
            ActKind::Click | ActKind::Focus if name.is_some() || text.is_some() => return bad("invalid_request"),
            ActKind::SelectText if name.is_some() || text.is_some() || range.0.is_none() || range.1.is_none() || range.0 > range.1 || range.1.is_some_and(|e| e > 1_000_000) => return bad("invalid_request"),
            ActKind::SelectText => {}
            _ if range.0.is_some() || range.1.is_some() => return bad("invalid_request"),
            _ => {}
        }
        let Some(at) = self.snaps.iter().position(|s| s.id == snapshot_id) else { return bad("snapshot_expired") };
        if self.snaps[at].made.elapsed() > TTL { self.snaps.remove(at); return bad("snapshot_expired"); }
        let (pid, start, app_name, widx, win) = { let s = &self.snaps[at]; (s.pid, s.start, s.app_name.clone(), s.window, s.win.clone()) };
        // 1. same process? (identity is pid + process start, never the name alone)
        let apps = match self.bus.apps().await { Ok(a) => a, Err(e) => return bad(bus_code(e)) };
        let mut app = None;
        for o in apps { if let Ok(i) = self.bus.info(&o).await { if i.pid == Some(pid) && i.start == Some(start) && i.name == app_name { app = Some((o, i)); break; } } }
        let Some((app, ai)) = app else { return bad("target_gone") };
        if tree::excluded(&ai.name, &ai.exe) { return bad("app_excluded"); }
        // 2. same window? (a changed window refuses; the helper never retargets another window)
        let wins = match self.windows(&app).await { Ok(w) => w, Err(c) => return bad(c) };
        let Some((wobj, winfo)) = wins.get(widx).cloned() else { return bad("window_changed") };
        if (winfo.role.clone(), winfo.name.clone()) != win { return bad("window_changed"); }
        // 3. same element? (walk the saved path, then compare role, name, id and actions)
        let Some(saved) = self.snaps[at].sigs.get(&path).cloned() else { return bad("no_such_element") };
        let mut cur = wobj;
        for n in &path {
            let kids = match self.bus.children(&cur).await { Ok(k) => k, Err(e) => return bad(bus_code(e)) };
            let Some(k) = kids.get(*n).cloned() else { return bad("stale_element") };
            cur = k;
        }
        let info = match self.bus.info(&cur).await { Ok(i) => i, Err(e) => return bad(bus_code(e)) };
        if Sig::of(&info) != saved { return bad("stale_element"); }
        // 4. the action: an AT-SPI action first; no matching interface is a typed refusal, never a pointer fallback.
        let text_check = if matches!(kind, ActKind::SetValue | ActKind::Type) { text.clone() } else { None };
        let before_value = info.value.clone();
        let done = match kind {
            ActKind::Click => {
                if info.actions.is_empty() { return bad("not_actionable"); }
                let i = info.actions.iter().position(|a| CLICK_NAMES.contains(&a.to_lowercase().as_str())).unwrap_or(0);
                self.bus.do_action(&cur, i).await
            }
            ActKind::Secondary => {
                let want = name.unwrap_or_default().to_lowercase();
                let Some(i) = info.actions.iter().position(|a| a.to_lowercase() == want) else { return bad("not_actionable") };
                self.bus.do_action(&cur, i).await
            }
            ActKind::SetValue => {
                let t = text.unwrap_or_default();
                if info.editable { self.bus.set_text(&cur, &t).await }
                else if info.numeric { match t.trim().parse::<f64>() { Ok(v) if v.is_finite() => self.bus.set_number(&cur, v).await, _ => return bad("invalid_request") } }
                else { return bad("not_actionable"); }
            }
            ActKind::Type => { if !info.editable { return bad("not_actionable"); } self.bus.insert_text(&cur, &text.unwrap_or_default()).await }
            ActKind::Focus => self.bus.grab_focus(&cur).await,
            ActKind::SelectText => {
                if info.value.is_none() && !info.editable { return bad("not_actionable"); }
                self.bus.select_text(&cur, range.0.unwrap_or(0) as i32, range.1.unwrap_or(0) as i32).await
            }
        };
        match done { Ok(true) => {} Ok(false) => return bad("action_failed"), Err(e) => return bad(match e { BusErr::Gone => "stale_element", e => bus_code(e) }) }
        // 5a. outcome check: re-read the element and say what the tree shows (never claim an effect that was not read back)
        let verified = self.verify(&cur, kind, text_check.as_deref(), before_value.as_deref(), info.numeric).await;
        // 5. report whether the window changed because of the action (the action is not repeated either way)
        let after = self.windows(&app).await.ok().and_then(|w| w.get(widx).map(|(_, i)| i.clone()));
        let changed = after.as_ref().is_none_or(|i| (i.role.clone(), i.name.clone()) != win);
        let tb = target(pid, start, widx, &winfo, snapshot_id);
        let ta = after.as_ref().map(|i| target(pid, start, widx, i, snapshot_id)).unwrap_or(Value::Null);
        json!({"ok": true, "outcome": "performed", "verified": verified, "window_changed": changed, "target_before": tb, "target_after": ta})
    }

    // This private address comes from the authenticated Host owner, never model arguments.
    async fn chooser_enter(&mut self, snapshot_id: &str, element: &str, path: &str) -> Value {
        use std::path::{Component, Path};
        let file = Path::new(path);
        let parent_name = file.parent().and_then(Path::file_name).and_then(|n| n.to_str());
        if !file.is_absolute() || path.len() > super::MAX_VALUE
            || path.chars().any(|c| c.is_control())
            || file.components().any(|c| matches!(c, Component::ParentDir | Component::CurDir))
            || !parent_name.is_some_and(|n| n.strip_prefix("openclank-selected-").is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')))
            || file.file_name().is_none() { return bad("invalid_request"); }
        let Some(steps) = parse_path(element) else { return bad("invalid_request") };
        let Some(at) = self.snaps.iter().position(|s| s.id == snapshot_id) else { return bad("snapshot_expired") };
        if self.snaps[at].made.elapsed() > TTL { self.snaps.remove(at); return bad("snapshot_expired"); }
        let snap = &self.snaps[at];
        let (pid, start, app_name, index, win, sigs) = (snap.pid, snap.start, snap.app_name.clone(), snap.window, snap.win.clone(), snap.sigs.clone());
        if snap.location_path.as_ref() != Some(&steps) { return bad("not_actionable"); }
        if win.0 != "file chooser" { return bad("window_changed"); }
        let locate = async |this: &mut Self| -> Result<(B::Obj, Info), &'static str> {
            let apps = this.bus.apps().await.map_err(bus_code)?;
            let mut app = None;
            for obj in apps {
                if let Ok(info) = this.bus.info(&obj).await {
                    if info.pid == Some(pid) && info.start == Some(start) && info.name == app_name {
                        if tree::excluded(&info.name, &info.exe) { return Err("app_excluded"); }
                        app = Some((obj, info)); break;
                    }
                }
            }
            let (app, app_info) = app.ok_or("target_gone")?;
            let wins = this.windows(&app).await?;
            let (mut obj, info) = wins.get(index).cloned().ok_or("window_changed")?;
            if (info.role.clone(), info.name.clone()) != win || !info.showing || !info.active { return Err("window_changed"); }
            // Re-enumerate the bounded chooser both before and after the set;
            // a new, hidden, ambiguous, skipped or truncated candidate refuses.
            let mut stack = vec![(obj.clone(), Vec::<usize>::new())];
            let mut count = 0usize; let mut candidates = Vec::new();
            while let Some((node, path)) = stack.pop() {
                if count >= MAX_NODES { return Err("not_actionable"); }
                count += 1;
                let item = this.bus.info(&node).await.map_err(bus_code)?;
                if !item.purpose_complete { return Err("not_actionable"); }
                if supported_gtk_location(&app_info, &info, &item) { candidates.push(path.clone()); }
                let kids = this.bus.children(&node).await.map_err(bus_code)?;
                if path.len() >= MAX_DEPTH && !kids.is_empty() { return Err("not_actionable"); }
                for (n, kid) in kids.into_iter().enumerate() { let mut next = path.clone(); next.push(n); stack.push((kid, next)); }
            }
            if candidates.len() != 1 || candidates[0] != steps { return Err("not_actionable"); }
            let mut prefix = Vec::new();
            let mut info = this.bus.info(&obj).await.map_err(bus_code)?;
            if sigs.get(&prefix) != Some(&Sig::of(&info)) { return Err("stale_element"); }
            for step in &steps {
                obj = this.bus.children(&obj).await.map_err(bus_code)?.get(*step).cloned().ok_or("stale_element")?;
                prefix.push(*step);
                info = this.bus.info(&obj).await.map_err(bus_code)?;
                if sigs.get(&prefix) != Some(&Sig::of(&info)) { return Err("stale_element"); }
            }
            // The submitted private path itself can resemble a secret to the
            // generic value heuristic; still reject protected/secret fields and
            // any preexisting hidden value before allowing the write.
            if !info.editable || (tree::hide_value(&info) && info.value.as_deref() != Some(path)) { return Err("not_actionable"); }
            let mut field = info.clone(); field.value = None;
            if tree::hide_value(&field) { return Err("not_actionable"); }
            Ok((obj, info))
        };
        let (obj, _) = match locate(self).await { Ok(x) => x, Err(c) => return bad(c) };
        // Once issued, even a false return or transport error may have changed the field.
        if !matches!(self.bus.set_text(&obj, path).await, Ok(true)) { return bad("outcome_unknown"); }
        let (_, after) = match locate(self).await { Ok(x) => x, Err(_) => return bad("outcome_unknown") };
        if after.value.as_deref() != Some(path) { return bad("outcome_unknown"); }
        json!({"ok": true, "outcome": "entered"})
    }

    /// `true` / `false` when the element's value can be read back, `"unknown"` otherwise (clicks, focus, selection, unreadable values).
    async fn verify(&mut self, o: &B::Obj, kind: ActKind, wanted: Option<&str>, before: Option<&str>, numeric: bool) -> Value {
        let (Some(wanted), true) = (wanted, matches!(kind, ActKind::SetValue | ActKind::Type)) else { return json!("unknown") };
        let Ok(after) = self.bus.info(o).await else { return json!("unknown") };
        let Some(now) = after.value.as_deref() else { return json!("unknown") };
        if now.chars().count() > super::MAX_VALUE { return json!("unknown") }   // the read-back is capped; a long value cannot be compared
        match kind {
            ActKind::SetValue if numeric => match (now.trim().parse::<f64>(), wanted.trim().parse::<f64>()) { (Ok(a), Ok(b)) => json!((a - b).abs() < 1e-9), _ => json!("unknown") },
            ActKind::SetValue => json!(now == wanted),
            _ => match before { Some(b) if now.len() >= b.len() => json!(now.contains(wanted) && now != b), _ => json!("unknown") },
        }
    }

    pub async fn handle(&mut self, req: Req) -> Value {
        match req {
            Req::Handshake { .. } => match self.bus.ready().await {
                Ok(()) => json!({"ok": true, "backend": "atspi/1", "accessibility": "ready", "actions": ["click", "secondary", "set_value", "type", "focus", "select_text"],
                                 "limits": {"nodes": MAX_NODES, "depth": MAX_DEPTH, "value_chars": super::MAX_VALUE}, "chooser_field_profile": "gtk3-location-v1"}),
                Err(e) => bad(bus_code(e)),
            },
            Req::ListApps { .. } => self.list_apps().await,
            Req::ListWindows { app, .. } => self.list_windows(&app).await,
            Req::AppState { app, window_index, .. } => self.app_state(&app, window_index).await,
            Req::Act { snapshot_id, element, action, name, text, start, end, .. } => self.act(&snapshot_id, &element, action, name, text, (start, end)).await,
            Req::ChooserEnter { snapshot_id, element, path, .. } => self.chooser_enter(&snapshot_id, &element, &path).await,
        }
    }
}

pub async fn serve<B: A11yBus>(bus: B, rx: &mut mpsc::Receiver<Option<Vec<u8>>>, out: &mut impl FnMut(String) -> bool, stop: &AtomicBool) {
    let mut core = Core::new(bus);
    loop {
        let line = loop {
            if stop.load(Ordering::Relaxed) { return; }
            tokio::select! { x = rx.recv() => match x { Some(Some(l)) => break l, _ => return }, _ = tokio::time::sleep(POLL) => {} }
        };
        let reply = match serde_json::from_slice::<Req>(&line) {
            Ok(r) if r.v() == 1 => timeout(REQ_TIMEOUT, core.handle(r)).await.unwrap_or_else(|_| bad("timeout")),
            _ => bad("invalid_request"),
        };
        if !out(reply.to_string()) { return; }
    }
}

#[cfg(test)]
mod tests;
