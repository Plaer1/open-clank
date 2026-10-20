//! Integration tests for the compile-time-checked strict `forward`.
//!
//! The strict `.forward()` is generated per source error set by `#[error_set]`
//! and carries a `where Target: Has<V>` bound for every variant of the
//! source — making a missing target variant a compile error instead of a
//! silent collapse to `Target::Internal`.
//!
//! Compile-fail coverage lives in `tests/compile_fail/forward_strict_*.rs`;
//! this file exercises the happy path.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;

// ---------------------------------------------------------------------------
// Discrete error types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct NotFound {
    pub resource: String,
}

impl fmt::Display for NotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "not found: {}", self.resource)
    }
}

impl Error for NotFound {}

impl FfiError for NotFound {
    fn ffi_code(&self) -> i32 {
        10
    }
}

#[derive(Debug)]
pub struct Timeout {
    pub duration_ms: u64,
}

impl fmt::Display for Timeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "timeout after {}ms", self.duration_ms)
    }
}

impl Error for Timeout {}

impl FfiError for Timeout {
    fn ffi_code(&self) -> i32 {
        11
    }
}

// ---------------------------------------------------------------------------
// SourceErrors declares NotFound and Timeout.
// SupersetErrors declares NotFound, Timeout, and one extra variant — it is a
// valid strict-forward target.
// ---------------------------------------------------------------------------

#[error_set]
pub enum SourceErrors {
    NotFound,
    Timeout,
}

#[error_set]
pub enum SupersetErrors {
    NotFound,
    Timeout,
    RateLimit,
}

#[derive(Debug)]
pub struct RateLimit;

impl fmt::Display for RateLimit {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "rate limited")
    }
}

impl Error for RateLimit {}

impl FfiError for RateLimit {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Has<V> auto-impls — sanity checks the macro emits the marker per variant.
// ---------------------------------------------------------------------------

fn assert_has<S, V>()
where
    S: lore_error_set::Has<V>,
{
}

#[test]
fn macro_emits_has_impl_for_each_declared_variant() {
    assert_has::<SourceErrors, NotFound>();
    assert_has::<SourceErrors, Timeout>();
    assert_has::<SupersetErrors, NotFound>();
    assert_has::<SupersetErrors, Timeout>();
    assert_has::<SupersetErrors, RateLimit>();
}

#[test]
fn macro_emits_has_internal_universally() {
    assert_has::<SourceErrors, lore_error_set::Internal>();
    assert_has::<SupersetErrors, lore_error_set::Internal>();
}

// ---------------------------------------------------------------------------
// Strict forward on a Result — uses the generated ResultForwardStrict trait.
// ---------------------------------------------------------------------------

#[test]
fn strict_forward_on_result_maps_not_found_directly() {
    use lore_error_set::ForwardStrict;

    fn caller() -> Result<String, SupersetErrors> {
        let result: Result<String, SourceErrors> = Err(NotFound {
            resource: "doc".into(),
        }
        .into());
        result.forward::<SupersetErrors>("loading doc")
    }

    let err = caller().unwrap_err();
    assert!(err.is_not_found());
    assert_eq!(err.to_string(), "not found: doc");
}

#[test]
fn strict_forward_on_result_maps_timeout_directly() {
    use lore_error_set::ForwardStrict;

    fn caller() -> Result<String, SupersetErrors> {
        let result: Result<String, SourceErrors> = Err(Timeout { duration_ms: 5000 }.into());
        result.forward::<SupersetErrors>("waiting")
    }

    let err = caller().unwrap_err();
    assert!(err.is_timeout());
    assert_eq!(err.to_string(), "timeout after 5000ms");
}

#[test]
fn strict_forward_on_result_passes_ok_through() {
    use lore_error_set::ForwardStrict;

    fn caller() -> Result<String, SupersetErrors> {
        let result: Result<String, SourceErrors> = Ok("hello".into());
        result.forward::<SupersetErrors>("never used")
    }

    assert_eq!(caller().unwrap(), "hello");
}

#[test]
fn strict_forward_with_closure_only_called_on_error_path() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    use lore_error_set::ForwardStrict;

    static CALLED: AtomicBool = AtomicBool::new(false);

    CALLED.store(false, Ordering::SeqCst);
    let result: Result<String, SourceErrors> = Ok("hello".into());
    let _: Result<String, SupersetErrors> = result.forward_with(|| {
        CALLED.store(true, Ordering::SeqCst);
        "should not run".into()
    });
    assert!(!CALLED.load(Ordering::SeqCst));

    CALLED.store(false, Ordering::SeqCst);
    let result: Result<String, SourceErrors> = Err(Timeout { duration_ms: 1 }.into());
    let mapped: Result<String, SupersetErrors> = result.forward_with(|| {
        CALLED.store(true, Ordering::SeqCst);
        "lazy ctx".into()
    });
    assert!(CALLED.load(Ordering::SeqCst));
    assert!(mapped.unwrap_err().is_timeout());
}

// ---------------------------------------------------------------------------
// Strict forward as inherent method on the source set value.
// ---------------------------------------------------------------------------

#[test]
fn strict_forward_inherent_method_maps_variant_directly() {
    let source: SourceErrors = NotFound {
        resource: "branch".into(),
    }
    .into();
    let target: SupersetErrors = source.forward("translating");
    assert!(target.is_not_found());
}

#[test]
fn strict_forward_inherent_method_maps_internal_to_internal() {
    let internal = SourceErrors::internal("synthetic");
    let target: SupersetErrors = internal.forward("forwarding internal");
    assert!(target.is_internal());
    let traced = target
        .as_internal_traced()
        .expect("should be traced internal");
    // The outer .forward context is recorded on the trace as a Location entry.
    assert!(
        traced
            .trace()
            .locations()
            .iter()
            .any(|l| l.context() == Some("forwarding internal")),
        "forward context should appear on trace"
    );
}

// ---------------------------------------------------------------------------
// Strict forward on the Matched* enum.
// ---------------------------------------------------------------------------

#[test]
fn matched_strict_forward_maps_variant_directly() {
    use lore_error_set::ResultExt;

    fn caller() -> Result<(), SupersetErrors> {
        let r: Result<(), SourceErrors> = Err(NotFound {
            resource: "doc".into(),
        }
        .into());
        match r.try_match("loading")? {
            Ok(()) => Ok(()),
            Err(other) => Err(other.forward::<SupersetErrors>("catch-all")),
        }
    }

    let err = caller().unwrap_err();
    assert!(err.is_not_found());
}

#[test]
fn matched_strict_forward_with_lazy_context_only_runs_closure_on_error() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    use lore_error_set::ResultExt;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn run(input: Result<(), SourceErrors>) -> Result<(), SupersetErrors> {
        match input.try_match("loading")? {
            Ok(()) => Ok(()),
            Err(other) => Err(other.forward_with::<SupersetErrors, _>(|| {
                CALLED.store(true, Ordering::SeqCst);
                "lazy ctx".into()
            })),
        }
    }

    CALLED.store(false, Ordering::SeqCst);
    let ok: Result<(), SourceErrors> = Ok(());
    let _ = run(ok);
    assert!(
        !CALLED.load(Ordering::SeqCst),
        "Matched closure should NOT run on Ok"
    );

    CALLED.store(false, Ordering::SeqCst);
    let err: Result<(), SourceErrors> = Err(Timeout { duration_ms: 1 }.into());
    let mapped = run(err);
    assert!(CALLED.load(Ordering::SeqCst), "closure must run on Err");
    assert!(mapped.unwrap_err().is_timeout());
}
