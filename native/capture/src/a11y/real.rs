//! Thin zbus layer behind `A11yBus`. No rule lives here: it maps AT-SPI D-Bus calls to the trait and every call is time-bounded.
//! Used through `ashpd::zbus` (already in the dependency tree); nothing is read from the screen and no input is injected.
use super::{core, A11yBus, BusErr, Info, MAX_VALUE};
use ashpd::zbus::{self, proxy::{Builder as ProxyBuilder, CacheProperties}, zvariant::OwnedObjectPath, Connection};
use std::{collections::HashMap, io::{self, Write}, sync::atomic::{AtomicBool, Ordering}, time::Duration};
use tokio::{io::{AsyncBufReadExt, AsyncReadExt, BufReader}, sync::mpsc};

const CALL: Duration = Duration::from_secs(3);
const ROOT: &str = "/org/a11y/atspi/accessible/root";
const REGISTRY: &str = "org.a11y.atspi.Registry";
const ACC: &str = "org.a11y.atspi.Accessible";
const WINDOW_COORDS: u32 = 1;
const MAX_LINE: u64 = 8192;

#[derive(Clone, Debug, PartialEq)]
pub struct ObjRef { dest: String, path: String }

fn err(e: zbus::Error) -> BusErr {
    use zbus::Error as E;
    match &e {
        E::MethodError(name, _, _) if matches!(name.as_str(), "org.freedesktop.DBus.Error.ServiceUnknown" | "org.freedesktop.DBus.Error.UnknownObject"
            | "org.freedesktop.DBus.Error.NoReply" | "org.freedesktop.DBus.Error.UnknownMethod" | "org.freedesktop.DBus.Error.Disconnected") => BusErr::Gone,
        E::InputOutput(_) | E::Address(_) => BusErr::Unavailable,
        _ => BusErr::Failed,
    }
}

impl From<zbus::Error> for BusErr { fn from(e: zbus::Error) -> BusErr { err(e) } }

/// Bit `n` of the AT-SPI state set (two u32 words).
pub fn has_state(words: &[u32], n: u32) -> bool { words.get((n / 32) as usize).is_some_and(|w| w & (1 << (n % 32)) != 0) }
const ST_ACTIVE: u32 = 1; const ST_EDITABLE: u32 = 7; const ST_MODAL: u32 = 16; const ST_SHOWING: u32 = 25;
/// Process start time (clock ticks since boot) from `/proc/<pid>/stat`, which survives a comm containing spaces or parentheses.
pub fn start_from_stat(stat: &str) -> Option<u64> { stat.rsplit_once(')')?.1.split_whitespace().nth(19)?.parse().ok() }

pub struct Real { conn: Option<Connection>, session: Option<Connection>, pids: HashMap<String, (Option<u32>, Option<u64>, String)>, tk: HashMap<String, String> }
impl Real { pub fn new() -> Real { Real { conn: None, session: None, pids: HashMap::new(), tk: HashMap::new() } } }

async fn bounded<T>(f: impl std::future::Future<Output = Result<T, zbus::Error>>) -> Result<T, BusErr> {
    match tokio::time::timeout(CALL, f).await { Ok(r) => r.map_err(err), Err(_) => Err(BusErr::Timeout) }
}

impl Real {
    async fn bus(&mut self) -> Result<Connection, BusErr> {
        if let Some(c) = &self.conn { return Ok(c.clone()); }
        let session = bounded(Connection::session()).await?;
        let addr: String = {
            let p = bounded(ProxyBuilder::<'_, zbus::Proxy<'_>>::new(&session).destination("org.a11y.Bus")?.path("/org/a11y/bus")?.interface("org.a11y.Bus")?.cache_properties(CacheProperties::No).build()).await?;
            bounded(p.call("GetAddress", &())).await?
        };
        let conn = bounded(async { zbus::connection::Builder::address(addr.as_str())?.build().await }).await?;
        self.session = Some(session);
        self.conn = Some(conn.clone());
        Ok(conn)
    }
    async fn proxy(&mut self, o: &ObjRef, iface: &str) -> Result<zbus::Proxy<'static>, BusErr> {
        let conn = self.bus().await?;
        bounded(ProxyBuilder::<'_, zbus::Proxy<'_>>::new(&conn).destination(o.dest.clone())?.path(o.path.clone())?.interface(iface.to_string())?.cache_properties(CacheProperties::No).build()).await
    }
    async fn pid_of(&mut self, dest: &str) -> (Option<u32>, Option<u64>, String) {
        if let Some(x) = self.pids.get(dest) { return x.clone(); }
        let mut out = (None, None, String::new());
        if let Ok(conn) = self.bus().await {
            let p = ProxyBuilder::<'_, zbus::Proxy<'_>>::new(&conn).destination("org.freedesktop.DBus").and_then(|b| b.path("/org/freedesktop/DBus")).and_then(|b| b.interface("org.freedesktop.DBus")).map(|b| b.cache_properties(CacheProperties::No));
            if let Ok(b) = p { if let Ok(p) = b.build().await {
                if let Ok(pid) = bounded(p.call::<_, _, u32>("GetConnectionUnixProcessID", &(dest,))).await {
                    let start = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok().and_then(|s| start_from_stat(&s));
                    let exe = std::fs::read_to_string(format!("/proc/{pid}/comm")).map(|s| s.trim().to_string()).unwrap_or_default();
                    out = (Some(pid), start, exe);
                }
            } }
        }
        self.pids.insert(dest.to_string(), out.clone());
        out
    }
}

impl A11yBus for Real {
    type Obj = ObjRef;
    async fn ready(&mut self) -> Result<(), BusErr> {
        let _ = self.bus().await?;
        let root = ObjRef { dest: REGISTRY.into(), path: ROOT.into() };
        let p = self.proxy(&root, ACC).await?;
        let kids: Vec<(String, OwnedObjectPath)> = bounded(p.call("GetChildren", &())).await?;
        if kids.is_empty() {
            // Nothing registered: either no application exposes accessibility or the switch is off.
            let session = self.session.clone().ok_or(BusErr::Unavailable)?;
            let st = bounded(ProxyBuilder::<'_, zbus::Proxy<'_>>::new(&session).destination("org.a11y.Bus")?.path("/org/a11y/bus")?.interface("org.a11y.Status")?.cache_properties(CacheProperties::No).build()).await?;
            let on: bool = bounded(st.get_property("IsEnabled")).await.unwrap_or(false);
            if !on { return Err(BusErr::Disabled); }
        }
        Ok(())
    }
    async fn apps(&mut self) -> Result<Vec<ObjRef>, BusErr> {
        let root = ObjRef { dest: REGISTRY.into(), path: ROOT.into() };
        let p = self.proxy(&root, ACC).await?;
        let kids: Vec<(String, OwnedObjectPath)> = bounded(p.call("GetChildren", &())).await?;
        Ok(kids.into_iter().map(|(d, p)| ObjRef { dest: d, path: p.to_string() }).collect())
    }
    async fn children(&mut self, o: &ObjRef) -> Result<Vec<ObjRef>, BusErr> {
        let p = self.proxy(o, ACC).await?;
        let kids: Vec<(String, OwnedObjectPath)> = bounded(p.call("GetChildren", &())).await?;
        Ok(kids.into_iter().filter(|(_, p)| p.as_str() != "/org/a11y/atspi/null").map(|(d, p)| ObjRef { dest: d, path: p.to_string() }).collect())
    }
    async fn info(&mut self, o: &ObjRef) -> Result<Info, BusErr> {
        let acc = self.proxy(o, ACC).await?;
        let mut i = Info::default();
        i.name = bounded(acc.get_property::<String>("Name")).await?;
        i.role = bounded(acc.call::<_, _, String>("GetRoleName", &())).await?.to_lowercase();
        let observed_states: Result<Vec<u32>, _> = bounded(acc.call("GetState", &())).await;
        i.purpose_complete = observed_states.is_ok();
        let states = observed_states.unwrap_or_default();
        i.showing = has_state(&states, ST_SHOWING); i.active = has_state(&states, ST_ACTIVE); i.editable = has_state(&states, ST_EDITABLE); i.modal = has_state(&states, ST_MODAL);
        i.id = bounded(acc.get_property::<String>("AccessibleId")).await.unwrap_or_default();
        if i.editable {
            let observed_attrs: Result<HashMap<String, String>, _> = bounded(acc.call("GetAttributes", &())).await;
            i.purpose_complete &= observed_attrs.is_ok();
            let attrs = observed_attrs.unwrap_or_default();
            i.location_marker = attrs.get("toolkit").is_some_and(|v| v == "gtk")
                && attrs.get("placeholder-text").is_some_and(|v| v == "Location");
        }
        let ifaces: Vec<String> = bounded(acc.call("GetInterfaces", &())).await.unwrap_or_default();
        let has = |n: &str| ifaces.iter().any(|x| x == &format!("org.a11y.atspi.{n}"));
        if has("Action") {
            let a = self.proxy(o, "org.a11y.atspi.Action").await?;
            let list: Vec<(String, String, String)> = bounded(a.call("GetActions", &())).await.unwrap_or_default();
            i.actions = list.into_iter().map(|(n, _, _)| n).collect();
        }
        if has("Text") {
            let t = self.proxy(o, "org.a11y.atspi.Text").await?;
            let n: i32 = bounded(t.get_property("CharacterCount")).await.unwrap_or(0);
            if n > 0 { i.value = bounded(t.call("GetText", &(0i32, n.min(MAX_VALUE as i32 + 1)))).await.ok(); }
        }
        if i.value.is_none() && has("Value") {
            let v = self.proxy(o, "org.a11y.atspi.Value").await?;
            if let Ok(x) = bounded(v.get_property::<f64>("CurrentValue")).await { i.value = Some(x.to_string()); i.numeric = !i.editable; }
        }
        if has("Component") {
            let c = self.proxy(o, "org.a11y.atspi.Component").await?;
            i.bounds = bounded(c.call::<_, _, (i32, i32, i32, i32)>("GetExtents", &(WINDOW_COORDS,))).await.ok().map(|(x, y, w, h)| [x, y, w, h]);
        }
        let (pid, start, exe) = self.pid_of(&o.dest).await;
        i.pid = pid; i.start = start; i.exe = exe;
        if has("Application") {
            let a = self.proxy(o, "org.a11y.atspi.Application").await?;
            if let Ok(t) = bounded(a.get_property::<String>("ToolkitName")).await { self.tk.insert(o.dest.clone(), t); }
            i.toolkit_version = bounded(a.get_property::<String>("ToolkitVersion")).await.unwrap_or_default();
            i.locale = bounded(acc.get_property::<String>("Locale")).await.unwrap_or_default();
        }
        i.toolkit = self.tk.get(&o.dest).cloned().unwrap_or_default();
        Ok(i)
    }
    async fn do_action(&mut self, o: &ObjRef, index: usize) -> Result<bool, BusErr> {
        let a = self.proxy(o, "org.a11y.atspi.Action").await?;
        bounded(a.call("DoAction", &(index as i32,))).await
    }
    async fn set_text(&mut self, o: &ObjRef, text: &str) -> Result<bool, BusErr> {
        let e = self.proxy(o, "org.a11y.atspi.EditableText").await?;
        bounded(e.call("SetTextContents", &(text,))).await
    }
    async fn insert_text(&mut self, o: &ObjRef, text: &str) -> Result<bool, BusErr> {
        let t = self.proxy(o, "org.a11y.atspi.Text").await?;
        let caret: i32 = bounded(t.get_property("CaretOffset")).await.unwrap_or(0);
        let e = self.proxy(o, "org.a11y.atspi.EditableText").await?;
        bounded(e.call("InsertText", &(caret, text, text.chars().count() as i32))).await
    }
    async fn set_number(&mut self, o: &ObjRef, v: f64) -> Result<bool, BusErr> {
        let p = self.proxy(o, "org.a11y.atspi.Value").await?;
        match tokio::time::timeout(CALL, p.set_property("CurrentValue", v)).await { Ok(Ok(())) => Ok(true), Ok(Err(_)) => Err(BusErr::Failed), Err(_) => Err(BusErr::Timeout) }
    }
    async fn select_text(&mut self, o: &ObjRef, start: i32, end: i32) -> Result<bool, BusErr> {
        let t = self.proxy(o, "org.a11y.atspi.Text").await?;
        let have: i32 = bounded(t.call("GetNSelections", &())).await.unwrap_or(0);
        if have > 0 { bounded(t.call("SetSelection", &(0i32, start, end))).await } else { bounded(t.call("AddSelection", &(start, end))).await }
    }
    async fn grab_focus(&mut self, o: &ObjRef) -> Result<bool, BusErr> {
        let c = self.proxy(o, "org.a11y.atspi.Component").await?;
        bounded(c.call("GrabFocus", &())).await
    }
}

static TERM: AtomicBool = AtomicBool::new(false);
extern "C" fn on_term(_: libc::c_int) { TERM.store(true, Ordering::Relaxed); }

/// `--a11y-v1`: serve JSON Lines on stdin/stdout until EOF or SIGTERM.
pub async fn run() {
    unsafe { libc::signal(libc::SIGTERM, on_term as extern "C" fn(libc::c_int) as libc::sighandler_t); }
    let (tx, mut rx) = mpsc::channel::<Option<Vec<u8>>>(1);
    tokio::spawn(async move {
        let mut input = BufReader::new(tokio::io::stdin());
        loop {
            let mut line = Vec::new();
            let n = (&mut input).take(MAX_LINE + 2).read_until(b'\n', &mut line).await;
            if !matches!(n, Ok(n) if n > 0) || line.len() as u64 > MAX_LINE || !line.ends_with(b"\n") { let _ = tx.send(None).await; break; }
            if tx.send(Some(line)).await.is_err() { break; }
        }
    });
    let mut out = |s: String| { let mut o = io::stdout().lock(); o.write_all(s.as_bytes()).and_then(|_| o.write_all(b"\n")).and_then(|_| o.flush()).is_ok() };
    core::serve(Real::new(), &mut rx, &mut out, &TERM).await;
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn state_bits_span_both_words() {
        let w = [(1 << 1) | (1 << 7), 0u32];
        assert!(has_state(&w, 1) && has_state(&w, 7) && !has_state(&w, 25) && !has_state(&[], 1));
        assert!(has_state(&[0, 1 << 2], 34), "bit 34 lives in the second word");
    }
    #[test] fn start_time_survives_odd_process_names() {
        let stat = "123 (my (odd) app) S 1 123 123 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 1000 100 18446744073709551615";
        assert_eq!(start_from_stat(stat), Some(987654)); assert_eq!(start_from_stat("garbage"), None);
    }
    #[test] fn bus_errors_map_to_closed_codes() {
        assert_eq!(err(zbus::Error::InputOutput(std::sync::Arc::new(io::Error::other("x")))), BusErr::Unavailable);
        assert_eq!(err(zbus::Error::Failure("x".into())), BusErr::Failed);
    }
}
