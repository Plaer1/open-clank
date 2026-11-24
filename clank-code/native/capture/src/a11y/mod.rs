//! `--a11y-v1` helper: read the accessibility tree of one application window and act on ONE element, over the
//! AT-SPI D-Bus interfaces. No portal, no input injection, no screen reading. JSON Lines on stdin/stdout.
//! All rules live in `core` behind the `A11yBus` trait; `real` is the thin zbus layer. Typed or set text is never
//! logged and never echoed back; replies carry codes and structure only.
pub mod core;
pub mod tree;
pub mod real;

pub const MAX_NODES: usize = 1200;
pub const MAX_DEPTH: usize = 64;
pub const MAX_VALUE: usize = 500;
pub const MAX_TEXT: usize = 4096;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum BusErr { Unavailable, Disabled, Timeout, Gone, Failed }

/// What the helper knows about one accessible object. Everything an element must still match to be "the same element".
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Info {
    pub name: String, pub role: String, pub id: String, pub actions: Vec<String>, pub value: Option<String>,
    pub protected: bool, pub showing: bool, pub active: bool, pub editable: bool, pub numeric: bool, pub modal: bool,
    pub pid: Option<u32>, pub start: Option<u64>, pub exe: String, pub toolkit: String, pub toolkit_version: String,
    pub locale: String, pub location_marker: bool, pub purpose_complete: bool, pub bounds: Option<[i32; 4]>,
}

/// The accessibility bus as the core sees it. The real layer talks D-Bus; tests use a fake tree.
pub trait A11yBus {
    type Obj: Clone;
    /// The accessibility bus is reachable and accessibility is switched on.
    async fn ready(&mut self) -> Result<(), BusErr>;
    async fn apps(&mut self) -> Result<Vec<Self::Obj>, BusErr>;
    async fn info(&mut self, o: &Self::Obj) -> Result<Info, BusErr>;
    async fn children(&mut self, o: &Self::Obj) -> Result<Vec<Self::Obj>, BusErr>;
    async fn do_action(&mut self, o: &Self::Obj, index: usize) -> Result<bool, BusErr>;
    async fn set_text(&mut self, o: &Self::Obj, text: &str) -> Result<bool, BusErr>;
    async fn insert_text(&mut self, o: &Self::Obj, text: &str) -> Result<bool, BusErr>;
    async fn set_number(&mut self, o: &Self::Obj, v: f64) -> Result<bool, BusErr>;
    async fn grab_focus(&mut self, o: &Self::Obj) -> Result<bool, BusErr>;
    /// Select characters [start, end) through the Text interface.
    async fn select_text(&mut self, o: &Self::Obj, start: i32, end: i32) -> Result<bool, BusErr>;
}
