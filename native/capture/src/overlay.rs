//! Compositor-space target matching for the optional desktop cursor surface.
//! A portal position and size must match exactly one active output; never infer
//! an output from pixel dimensions or select the first of mirrored outputs.

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LogicalRect { pub x: i32, pub y: i32, pub width: u32, pub height: u32 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Output { pub registry_id: u32, pub rect: LogicalRect }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mapping { pub output_id: u32, pub rect: LogicalRect }

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TargetIdentity {
    pub compositor_pid: u32,
    pub compositor_start: u64,
    pub output_id: u32,
    pub rect: LogicalRect,
}

pub fn match_output(portal: Option<LogicalRect>, outputs: &[Output]) -> Option<Mapping> {
    let rect = portal?;
    if rect.width == 0 || rect.height == 0 { return None; }
    let mut matches = outputs.iter().filter(|out| out.rect == rect);
    let output = matches.next()?;
    if matches.next().is_some() { return None; }
    Some(Mapping { output_id: output.registry_id, rect })
}

impl Mapping {
    pub fn still_matches(self, outputs: &[Output]) -> bool {
        match_output(Some(self.rect), outputs) == Some(self)
    }
    /// Source coordinates are already in portal compositor-space, not PipeWire pixels.
    pub fn point(self, x: i64, y: i64) -> Option<(i32, i32)> {
        if x < 0 || y < 0 || x >= self.rect.width as i64 || y >= self.rect.height as i64 { return None; }
        Some((i32::try_from(x).ok()?, i32::try_from(y).ok()?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_unique_matching_logical_output_survives_topology_change() {
        let rect = LogicalRect { x: -1920, y: 0, width: 1920, height: 1080 };
        let first = Output { registry_id: 7, rect };
        let other = Output { registry_id: 8, rect: LogicalRect { x: 0, ..rect } };
        let mapping = match_output(Some(rect), &[first, other]).unwrap();
        assert_eq!(mapping.output_id, 7);
        assert_eq!(mapping.point(1919, 1079), Some((1919, 1079)));
        assert_eq!(mapping.point(1920, 0), None);
        assert!(!mapping.still_matches(&[other]));
        assert_eq!(match_output(Some(rect), &[first, Output { registry_id: 9, rect }]), None);
        assert_eq!(match_output(None, &[first]), None);
    }
}

#[cfg(feature = "cursor-overlay")]
mod native {
    use super::{match_output, LogicalRect, Output, TargetIdentity};
    use std::{collections::HashMap, ffi::CString, fs::File, io::Write, os::fd::{AsFd, AsRawFd, FromRawFd}, sync::{mpsc, Arc, atomic::{AtomicBool, Ordering}}, thread, time::{Duration, Instant}};
    use wayland_client::{delegate_noop, Connection, Dispatch, QueueHandle, protocol::{wl_buffer, wl_compositor, wl_output, wl_region, wl_registry, wl_shm, wl_shm_pool, wl_surface}};
    use wayland_protocols::xdg::xdg_output::zv1::client::{zxdg_output_manager_v1, zxdg_output_v1};
    use wayland_protocols_wlr::layer_shell::v1::client::{zwlr_layer_shell_v1, zwlr_layer_surface_v1};

    const SIDE: i32 = 24;
    enum Message { Move(i32, i32), Stop }
    pub struct Cursor { tx: mpsc::Sender<Message>, alive: Arc<AtomicBool>, stop: Arc<AtomicBool>, target: TargetIdentity }
    impl Cursor {
        pub fn move_to(&self, x: i32, y: i32) { let _ = self.tx.send(Message::Move(x, y)); }
        pub fn is_active(&self) -> bool { self.alive.load(Ordering::Acquire) }
        pub fn target(&self) -> Option<TargetIdentity> { self.is_active().then_some(self.target) }
    }
    impl Drop for Cursor { fn drop(&mut self) { self.stop.store(true, Ordering::Release); let _ = self.tx.send(Message::Stop); } }

    #[derive(Default)]
    struct OutputData { object: Option<wl_output::WlOutput>, position: Option<(i32, i32)>, size: Option<(i32, i32)> }
    #[derive(Default)]
    struct State {
        compositor: Option<wl_compositor::WlCompositor>, shm: Option<wl_shm::WlShm>,
        manager: Option<zxdg_output_manager_v1::ZxdgOutputManagerV1>, layer: Option<zwlr_layer_shell_v1::ZwlrLayerShellV1>,
        outputs: HashMap<u32, OutputData>, surface: Option<wl_surface::WlSurface>,
        shell_surface: Option<zwlr_layer_surface_v1::ZwlrLayerSurfaceV1>, configured: bool, closed: bool,
    }
    impl State {
        fn outputs(&self) -> Option<Vec<Output>> {
            if self.outputs.is_empty() { return None; }
            self.outputs.iter().map(|(&id, data)| {
                let ((x, y), (w, h)) = (data.position?, data.size?);
                if w <= 0 || h <= 0 { return None; }
                Some(Output { registry_id: id, rect: LogicalRect { x, y, width: w as u32, height: h as u32 } })
            }).collect()
        }
    }
    impl Dispatch<wl_registry::WlRegistry, ()> for State {
        fn event(state: &mut Self, registry: &wl_registry::WlRegistry, event: wl_registry::Event, _: &(), _: &Connection, qh: &QueueHandle<Self>) {
            match event {
                wl_registry::Event::Global { name, interface, version } => match interface.as_str() {
                    "wl_compositor" => state.compositor = Some(registry.bind(name, version.min(4), qh, ())),
                    "wl_shm" => state.shm = Some(registry.bind(name, 1, qh, ())),
                    "wl_output" => { state.outputs.insert(name, OutputData { object: Some(registry.bind(name, version.min(3), qh, ())), ..Default::default() }); },
                    "zxdg_output_manager_v1" => state.manager = Some(registry.bind(name, version.min(3), qh, ())),
                    "zwlr_layer_shell_v1" => state.layer = Some(registry.bind(name, version.min(4), qh, ())),
                    _ => (),
                },
                wl_registry::Event::GlobalRemove { name } => { state.outputs.remove(&name); },
                _ => (),
            }
        }
    }
    impl Dispatch<zxdg_output_v1::ZxdgOutputV1, u32> for State {
        fn event(state: &mut Self, _: &zxdg_output_v1::ZxdgOutputV1, event: zxdg_output_v1::Event, id: &u32, _: &Connection, _: &QueueHandle<Self>) {
            if let Some(out) = state.outputs.get_mut(id) {
                match event {
                    zxdg_output_v1::Event::LogicalPosition { x, y } => out.position = Some((x, y)),
                    zxdg_output_v1::Event::LogicalSize { width, height } => out.size = Some((width, height)),
                    _ => (),
                }
            }
        }
    }
    impl Dispatch<zwlr_layer_surface_v1::ZwlrLayerSurfaceV1, ()> for State {
        fn event(state: &mut Self, obj: &zwlr_layer_surface_v1::ZwlrLayerSurfaceV1, event: zwlr_layer_surface_v1::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
            match event {
                zwlr_layer_surface_v1::Event::Configure { serial, width, height } => {
                    obj.ack_configure(serial);
                    if width != SIDE as u32 || height != SIDE as u32 { state.closed = true; } else { state.configured = true; }
                },
                zwlr_layer_surface_v1::Event::Closed => state.closed = true,
                _ => (),
            }
        }
    }
    impl Dispatch<wl_buffer::WlBuffer, ()> for State {
        fn event(_: &mut Self, buffer: &wl_buffer::WlBuffer, event: wl_buffer::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
            if let wl_buffer::Event::Release = event { buffer.destroy(); }
        }
    }
    delegate_noop!(State: ignore wl_compositor::WlCompositor);
    delegate_noop!(State: ignore wl_surface::WlSurface);
    delegate_noop!(State: ignore wl_region::WlRegion);
    delegate_noop!(State: ignore wl_output::WlOutput);
    delegate_noop!(State: ignore wl_shm::WlShm);
    delegate_noop!(State: ignore wl_shm_pool::WlShmPool);
    delegate_noop!(State: ignore zxdg_output_manager_v1::ZxdgOutputManagerV1);
    delegate_noop!(State: ignore zwlr_layer_shell_v1::ZwlrLayerShellV1);

    fn draw(shm: &wl_shm::WlShm, qh: &QueueHandle<State>, ox: i32, oy: i32) -> Option<wl_buffer::WlBuffer> {
        let mut pixels = vec![0u8; (SIDE * SIDE * 4) as usize];
        for y in 0..SIDE { for x in 0..SIDE {
            let dx = x - ox; let dy = y - oy;
            let d = dx * dx + dy * dy;
            if d <= 36 || (dx.abs() <= 1 && dy.abs() <= 10) || (dy.abs() <= 1 && dx.abs() <= 10) {
                let n = ((y * SIDE + x) * 4) as usize;
                pixels[n..n+4].copy_from_slice(&[0x36, 0xa6, 0xff, 0xff]);
            }
        }}
        let name = CString::new("openclank-cursor").ok()?;
        let fd = unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC) };
        if fd < 0 { return None; }
        let mut file = unsafe { File::from_raw_fd(fd) };
        file.write_all(&pixels).ok()?;
        let pool = shm.create_pool(file.as_fd(), pixels.len() as i32, qh, ());
        let buffer = pool.create_buffer(0, SIDE, SIDE, SIDE * 4, wl_shm::Format::Argb8888, qh, ());
        pool.destroy();
        Some(buffer)
    }

    pub fn start(rect: LogicalRect) -> Option<Cursor> {
        let (tx, rx) = mpsc::channel();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let alive = Arc::new(AtomicBool::new(false));
        let alive_thread = alive.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let stop_thread = stop.clone();
        thread::spawn(move || {
            let _ = run(rect, rx, ready_tx, &alive_thread, &stop_thread);
            alive_thread.store(false, Ordering::Release);
        });
        let target = ready_rx.recv_timeout(Duration::from_secs(3)).ok()??;
        Some(Cursor { tx, alive, stop, target })
    }
    pub struct TargetMonitor { stop: Arc<AtomicBool>, alive: Arc<AtomicBool>, target: TargetIdentity }
    impl TargetMonitor {
        pub fn target(&self) -> TargetIdentity { self.target }
        pub fn is_active(&self) -> bool { self.alive.load(Ordering::Acquire) }
    }
    impl Drop for TargetMonitor { fn drop(&mut self) { self.stop.store(true, Ordering::Release); } }

    pub fn monitor(rect: LogicalRect) -> Option<TargetMonitor> {
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let alive = Arc::new(AtomicBool::new(false)); let alive_thread = alive.clone();
        let stop = Arc::new(AtomicBool::new(false)); let stop_thread = stop.clone();
        thread::spawn(move || {
            let _ = run_monitor(rect, ready_tx, &alive_thread, &stop_thread);
            alive_thread.store(false, Ordering::Release);
        });
        let target = ready_rx.recv_timeout(Duration::from_secs(3)).ok()??;
        Some(TargetMonitor { stop, alive, target })
    }
    fn run_monitor(rect: LogicalRect, ready: mpsc::SyncSender<Option<TargetIdentity>>, alive: &AtomicBool, stop: &AtomicBool) -> Option<()> {
        let conn = Connection::connect_to_env().ok()?;
        let (compositor_pid, compositor_start) = peer_identity(&conn)?;
        let mut queue = conn.new_event_queue(); let qh = queue.handle();
        conn.display().get_registry(&qh, ());
        let mut state = State::default(); queue.roundtrip(&mut state).ok()?;
        let manager = state.manager.as_ref()?.clone();
        for (&id, out) in &state.outputs { manager.get_xdg_output(out.object.as_ref()?, &qh, id); }
        queue.roundtrip(&mut state).ok()?;
        let mapping = match_output(Some(rect), &state.outputs()?)?;
        let target = TargetIdentity { compositor_pid, compositor_start, output_id: mapping.output_id, rect };
        alive.store(true, Ordering::Release);
        if ready.send(Some(target)).is_err() { return None; }
        while !stop.load(Ordering::Acquire) {
            if !state.outputs().is_some_and(|outputs| mapping.still_matches(&outputs)) { return None; }
            pump(&mut queue, &mut state)?;
        }
        Some(())
    }

    fn run(rect: LogicalRect, rx: mpsc::Receiver<Message>, ready: mpsc::SyncSender<Option<TargetIdentity>>, alive: &AtomicBool, stop: &AtomicBool) -> Option<()> {
        let conn = Connection::connect_to_env().ok()?;
        let (compositor_pid, compositor_start) = peer_identity(&conn)?;
        let mut queue = conn.new_event_queue(); let qh = queue.handle();
        conn.display().get_registry(&qh, ());
        let mut state = State::default();
        queue.roundtrip(&mut state).ok()?;
        let manager = state.manager.as_ref()?.clone();
        for (&id, out) in &state.outputs { manager.get_xdg_output(out.object.as_ref()?, &qh, id); }
        queue.roundtrip(&mut state).ok()?;
        let mapping = match_output(Some(rect), &state.outputs()?)?;
        if rect.width < SIDE as u32 || rect.height < SIDE as u32 { return None; }
        let compositor = state.compositor.as_ref()?.clone();
        let output = state.outputs.get(&mapping.output_id)?.object.as_ref()?.clone();
        let surface = compositor.create_surface(&qh, ());
        let region = compositor.create_region(&qh, ());
        surface.set_input_region(Some(&region)); region.destroy();
        let shell = state.layer.as_ref()?.get_layer_surface(&surface, Some(&output), zwlr_layer_shell_v1::Layer::Overlay, "openclank-agent-cursor".into(), &qh, ());
        shell.set_anchor(zwlr_layer_surface_v1::Anchor::Top | zwlr_layer_surface_v1::Anchor::Left);
        shell.set_exclusive_zone(-1);
        shell.set_keyboard_interactivity(zwlr_layer_surface_v1::KeyboardInteractivity::None);
        shell.set_size(SIDE as u32, SIDE as u32); surface.commit();
        state.surface = Some(surface); state.shell_surface = Some(shell);
        let until = Instant::now() + Duration::from_secs(2);
        while !state.configured && !state.closed && !stop.load(Ordering::Acquire) && Instant::now() < until {
            pump(&mut queue, &mut state)?;
        }
        if !state.configured || state.closed || !state.outputs().is_some_and(|outputs| mapping.still_matches(&outputs)) { let _ = ready.send(None); return None; }
        alive.store(true, Ordering::Release);
        let target = TargetIdentity { compositor_pid, compositor_start, output_id: mapping.output_id, rect };
        if ready.send(Some(target)).is_err() { return None; }
        loop {
            if stop.load(Ordering::Acquire) || state.closed || !state.outputs().is_some_and(|outputs| mapping.still_matches(&outputs)) { return None; }
            while let Ok(message) = rx.try_recv() {
                if stop.load(Ordering::Acquire) || state.closed || !state.outputs().is_some_and(|outputs| mapping.still_matches(&outputs)) { return None; }
                match message {
                    Message::Stop => return Some(()),
                    Message::Move(x, y) => {
                        let (x, y) = mapping.point(x as i64, y as i64)?;
                        let left = x.min(rect.width as i32 - SIDE); let top = y.min(rect.height as i32 - SIDE);
                        let buffer = draw(state.shm.as_ref()?, &qh, x - left, y - top)?;
                        state.shell_surface.as_ref()?.set_margin(top, 0, 0, left);
                        let surface = state.surface.as_ref()?; surface.attach(Some(&buffer), 0, 0); surface.damage(0, 0, SIDE, SIDE); surface.commit();
                    }
                }
            }
            if state.closed || !state.outputs().is_some_and(|outputs| mapping.still_matches(&outputs)) { return None; }
            pump(&mut queue, &mut state)?;
        }
    }
    fn peer_identity(conn: &Connection) -> Option<(u32, u64)> {
        let mut peer = libc::ucred { pid: 0, uid: 0, gid: 0 };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let rc = unsafe { libc::getsockopt(conn.as_fd().as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
                                             &mut peer as *mut _ as *mut libc::c_void, &mut len) };
        if rc != 0 || len as usize != std::mem::size_of::<libc::ucred>() || peer.pid <= 1 || peer.uid != unsafe { libc::geteuid() } { return None; }
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", peer.pid)).ok()?;
        let fields = stat.rsplit_once(')')?.1.split_whitespace().collect::<Vec<_>>();
        let start = fields.get(19)?.parse::<u64>().ok()?;
        Some((peer.pid as u32, start))
    }

    fn pump(queue: &mut wayland_client::EventQueue<State>, state: &mut State) -> Option<()> {
        queue.dispatch_pending(state).ok()?; queue.flush().ok()?;
        if let Some(guard) = queue.prepare_read() {
            let mut fd = libc::pollfd { fd: guard.connection_fd().as_raw_fd(), events: libc::POLLIN, revents: 0 };
            let rc = unsafe { libc::poll(&mut fd, 1, 25) };
            if rc < 0 { return None; }
            if rc > 0 && fd.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 { return None; }
            if rc > 0 && fd.revents & libc::POLLIN != 0 { guard.read().ok()?; }
        }
        Some(())
    }
}

#[cfg(feature = "cursor-overlay")]
pub use native::{start, Cursor, monitor, TargetMonitor};
