//! Opt-in, bounded diagnostic aggregates. No paths, source, IDs, logs or DB writes.
//! Timings are inclusive wall time and may overlap; never sum them as CPU time.
//! Attempts (including errors/panic unwinding) are counted, not successful work.
//! Normal operations leave capture disabled. Future worker pools must explicitly
//! enter the captured context and join before taking the final snapshot.
use serde::Serialize;
use std::{cell::RefCell, sync::{Arc, Mutex}, time::Instant};

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[repr(usize)]
pub enum Stage {
    IndexTotal, Admission, Inventory, Policy, Read, Hash, Extract, Interfaces,
    Resolve, Stage, Seal, Publish, QueryTotal, QueryOpen, QueryPrepare, QuerySql,
    Projection, Serialization,
    GenerationAuditHit, GenerationAuditMiss, EligibleCountHit, EligibleCountMiss,
    MapContext, MapPolicyEntry, MapPolicyFinal, MapProjection,
    MapContextFence, MapContextConnectionWait, MapContextDecode,
}
const STAGES: [Stage; 29] = [Stage::IndexTotal, Stage::Admission, Stage::Inventory,
    Stage::Policy, Stage::Read, Stage::Hash, Stage::Extract, Stage::Interfaces,
    Stage::Resolve, Stage::Stage, Stage::Seal, Stage::Publish, Stage::QueryTotal,
    Stage::QueryOpen, Stage::QueryPrepare, Stage::QuerySql, Stage::Projection,
    Stage::Serialization, Stage::GenerationAuditHit, Stage::GenerationAuditMiss,
    Stage::EligibleCountHit, Stage::EligibleCountMiss,
    Stage::MapContext, Stage::MapPolicyEntry, Stage::MapPolicyFinal, Stage::MapProjection,
    Stage::MapContextFence, Stage::MapContextConnectionWait, Stage::MapContextDecode];

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Counters {
    pub calls: u64,
    pub elapsed_ns: u64,
    pub items: u64,
    pub bytes: u64,
    pub saturated: bool,
}
#[derive(Clone, Debug, Serialize)]
pub struct Measurement { pub stage: Stage, pub counters: Counters }
#[derive(Clone, Debug, Serialize)]
pub struct CodeMetrics {
    pub schema: &'static str,
    pub stages: Vec<Measurement>,
}

#[derive(Clone, Default)]
pub struct MetricsContext(Arc<Mutex<[Counters; 29]>>);
thread_local! { static ACTIVE: RefCell<Option<MetricsContext>> = const { RefCell::new(None) }; }
struct Restore(Option<MetricsContext>);
impl Drop for Restore {
    fn drop(&mut self) { ACTIVE.with(|slot| { slot.replace(self.0.take()); }); }
}
impl MetricsContext {
    /// Explicit propagation only: never implicitly combine unrelated jobs.
    pub fn enter<R>(&self, work: impl FnOnce() -> R) -> R {
        let _restore = Restore(ACTIVE.with(|slot| slot.replace(Some(self.clone()))));
        work()
    }
    pub fn snapshot(&self) -> CodeMetrics {
        let data = self.0.lock().unwrap_or_else(|p| p.into_inner());
        CodeMetrics { schema: "openclank.code-metrics/v2", stages: STAGES.iter()
            .map(|&stage| Measurement { stage, counters: data[stage as usize] }).collect() }
    }
}
pub fn current_context() -> Option<MetricsContext> { ACTIVE.with(|slot| slot.borrow().clone()) }
pub fn capture_code_metrics<R>(work: impl FnOnce() -> R) -> (R, CodeMetrics) {
    let context = MetricsContext::default();
    let result = context.enter(work);
    (result, context.snapshot())
}

pub struct Span { context: Option<MetricsContext>, stage: Stage, started: Option<Instant>, items: u64, bytes: u64, saturated: bool }
pub fn span(stage: Stage) -> Span {
    let context = current_context();
    let started = context.as_ref().map(|_| Instant::now());
    Span { context, stage, started, items: 0, bytes: 0, saturated: false }
}
impl Span {
    /// Operation-specific attempts/bytes, not distinct-source or output counts.
    pub fn add(&mut self, items: usize, bytes: usize) {
        self.saturated |= self.items.checked_add(items as u64).is_none() || self.bytes.checked_add(bytes as u64).is_none();
        self.items = self.items.saturating_add(items as u64);
        self.bytes = self.bytes.saturating_add(bytes as u64);
    }
}
impl Drop for Span {
    fn drop(&mut self) {
        let (Some(context), Some(started)) = (&self.context, self.started) else { return; };
        let mut data = context.0.lock().unwrap_or_else(|p| p.into_inner());
        let row = &mut data[self.stage as usize];
        row.saturated |= self.saturated;
        let elapsed = started.elapsed().as_nanos();
        for (dst, value) in [(&mut row.calls, 1u128), (&mut row.elapsed_ns, elapsed),
            (&mut row.items, self.items as u128), (&mut row.bytes, self.bytes as u128)] {
            let sum = *dst as u128 + value;
            if sum > u64::MAX as u128 { row.saturated = true; }
            *dst = sum.min(u64::MAX as u128) as u64;
        }
    }
}
/// Hash existing admitted bytes while keeping hashing attributable on both paths.
pub(crate) fn source_hash(bytes: &[u8]) -> blake3::Hash {
    let mut timing = span(Stage::Hash); timing.add(1, bytes.len()); blake3::hash(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn disabled_and_nested_captures_do_not_mix() {
        assert!(current_context().is_none());
        let (_, outer) = capture_code_metrics(|| {
            let _outer = span(Stage::Read);
            let (_, inner) = capture_code_metrics(|| { let mut s=span(Stage::Hash);s.add(2,17); });
            assert_eq!(inner.stages[Stage::Hash as usize].counters.bytes,17);
            assert_eq!(inner.stages[Stage::Read as usize].counters.calls,0);
        });
        assert_eq!(outer.stages[Stage::Read as usize].counters.calls,1);
        assert_eq!(outer.stages[Stage::Hash as usize].counters.calls,0);
        assert!(current_context().is_none());
    }
    #[test]
    fn errors_panics_and_explicit_worker_propagation_are_bounded() {
        let context=MetricsContext::default();
        let failed=std::panic::catch_unwind(||context.enter(|| { let _s=span(Stage::Extract);panic!("fixture"); }));
        assert!(failed.is_err());assert!(current_context().is_none());
        let worker=context.clone();
        std::thread::spawn(move||worker.enter(||{let mut s=span(Stage::Resolve);s.add(3,29);})).join().unwrap();
        let snapshot=context.snapshot();assert_eq!(snapshot.stages.len(),29);
        assert_eq!(snapshot.stages[Stage::Extract as usize].counters.calls,1);
        assert_eq!(snapshot.stages[Stage::Resolve as usize].counters.items,3);
        let text=serde_json::to_string(&snapshot).unwrap();
        assert!(text.len()<4096);assert!(!text.contains("fixture"));
    }
}
