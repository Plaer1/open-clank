//! App-owner indexing execution. No source authority, filesystem or restart API.
//! CPU leases count coordinators and workers together; retained-data reservations
//! do not claim parser/allocator/SQLite/process RSS. Workers are demand-created.
use serde::{Deserialize, Serialize};
use crate::code_index_limits::{IndexLimits,HARD_LIMITS};
use std::{
    cell::RefCell,
    collections::{BTreeMap, VecDeque},
    rc::Rc,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    thread::JoinHandle,
};

pub const MAX_REVISION: u64 = (1u64 << 53) - 1;
pub const MAX_QUEUED: usize = 32;
pub const MAX_DESCRIPTOR_BYTES: usize = 64 * 1024;
pub const MAX_DATA_BYTES: usize = 256 * 1024 * 1024;
pub const SMALL_DATA_BYTES: usize = 32 * 1024 * 1024 + 512 * 1024;
pub const LARGE_DATA_BYTES: usize = MAX_DATA_BYTES;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct SchedulerStatus {
    pub schema: &'static str,
    pub revision: u64,
    pub effective_revision: u64,
    pub desired: usize,
    pub effective: usize,
    pub desired_limits: IndexLimits,
    pub effective_limits: IndexLimits,
    pub hard_limits: SchedulerHardLimits,
    pub active: usize,
    pub queued: usize,
    pub drain_pending: bool,
}
#[derive(Clone,Debug,Serialize,PartialEq,Eq)]
pub struct SchedulerHardLimits {
    #[serde(flatten)] pub source:IndexLimits,
    pub max_queued_jobs:usize,
    pub max_descriptor_bytes:usize,
    pub max_retained_data_bytes:usize,
    pub max_generation_disk_bytes:u64,
    pub max_source_records:usize,
    pub max_resolution_records:usize,
}
#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum SchedulerRequest {
    ConfigureResources {origin:String,revision:u64,max_threads:u8,limits:IndexLimits},
    Status {
        origin: String,
    },
    Configure {
        origin: String,
        revision: u64,
        max_threads: u8,
    },
}
impl SchedulerRequest {
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::Status { origin } | Self::Configure { origin, .. } | Self::ConfigureResources {origin,..} if origin != "app" => {
                Err("denied: indexing scheduler is app-only".into())
            }
            Self::Configure {
                revision,
                max_threads,
                ..
            } if *revision == 0 || *revision > MAX_REVISION || !(1..=32).contains(max_threads) => {
                Err("invalid: indexing scheduler configuration".into())
            }
            Self::ConfigureResources {revision,max_threads,limits,..} => {
                if *revision==0||*revision>MAX_REVISION||!(1..=32).contains(max_threads){return Err("invalid: indexing scheduler configuration".into());}
                limits.validate()
            }
            _ => Ok(()),
        }
    }
}

struct Task {
    cohort: u64,
    cancel: Arc<AtomicBool>,
    run: Box<dyn FnOnce(bool) -> Box<dyn FnOnce() + Send> + Send>,
}
struct State {
    #[cfg(test)]
    observation: TestObservation,
    revision: u64,
    effective_revision: u64,
    desired: usize,
    effective: usize,
    desired_limits: IndexLimits,
    effective_limits: IndexLimits,
    active: usize,
    interactive: usize,
    next: u64,
    closing: bool,
    queued: VecDeque<(u64, usize)>,
    descriptor_bytes: usize,
    cohorts: BTreeMap<u64, (Arc<AtomicBool>, usize)>,
    waiters: VecDeque<(u64, u64)>,
    tasks: VecDeque<Task>,
    last_task_cohort: Option<u64>,
    inflight: usize,
    data_bytes: usize,
    data_waiters: VecDeque<u64>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            #[cfg(test)]
            observation: TestObservation::default(),
            revision: 0,
            effective_revision: 0,
            desired: 2,
            effective: 2,
            desired_limits:crate::code_index_limits::current(),
            effective_limits:crate::code_index_limits::current(),
            active: 0,
            interactive: 0,
            next: 0,
            closing: false,
            queued: VecDeque::new(),
            descriptor_bytes: 0,
            cohorts: BTreeMap::new(),
            waiters: VecDeque::new(),
            tasks: VecDeque::new(),
            last_task_cohort: None,
            inflight: 0,
            data_bytes: 0,
            data_waiters: VecDeque::new(),
        }
    }
}
impl State {
    fn next(&mut self) -> Result<u64, String> {
        self.next = self
            .next
            .checked_add(1)
            .ok_or("unavailable: scheduler sequence exhausted")?;
        Ok(self.next)
    }
    fn status(&self) -> SchedulerStatus {
        SchedulerStatus {
            schema: "open-clank.index-scheduler/v1",
            revision: self.revision,
            effective_revision: self.effective_revision,
            desired: self.desired,
            effective: self.effective,
            desired_limits:self.desired_limits,
            effective_limits:self.effective_limits,
            hard_limits:SchedulerHardLimits{source:HARD_LIMITS,max_queued_jobs:MAX_QUEUED,
                max_descriptor_bytes:MAX_DESCRIPTOR_BYTES,max_retained_data_bytes:MAX_DATA_BYTES,
                max_generation_disk_bytes:crate::store::code_partitioned::MAX_DISK,
                max_source_records:crate::store::code_partitioned::MAX_SOURCE_RECORDS,
                max_resolution_records:crate::store::code_partitioned::MAX_RESOLUTIONS},
            active: self.active,
            queued: self.queued.len(),
            drain_pending: self.revision != self.effective_revision,
        }
    }
    fn apply_drained(&mut self) {
        if self.cohorts.is_empty() && self.active == 0
            && crate::code_index_limits::activate(self.desired_limits).is_ok() {
            self.effective_limits=self.desired_limits;
            self.effective = self.desired;
            self.effective_revision = self.revision;
        }
    }
    // Bounded numeric round-robin across cohorts, FIFO within a cohort. With
    // three cohorts, merely avoiding the previous cohort could starve the third.
    fn next_task(&mut self) -> Option<Task> {
        let next = self
            .tasks
            .iter()
            .map(|t| t.cohort)
            .filter(|id| self.last_task_cohort.is_none_or(|last| *id > last))
            .min()
            .or_else(|| self.tasks.iter().map(|t| t.cohort).min())?;
        let pos = self.tasks.iter().position(|t| t.cohort == next)?;
        self.last_task_cohort = Some(next);
        self.tasks.remove(pos)
    }
    fn admit(&mut self, id: u64) -> bool {
        if self.closing
            || self.revision != self.effective_revision
            || self.cohorts.len() >= self.effective
            || self.queued.front().is_none_or(|(front, _)| *front != id)
        {
            return false;
        }
        let (_, bytes) = self.queued.pop_front().unwrap();
        self.descriptor_bytes -= bytes;
        true
    }
}
#[derive(Default)]
struct Shared {
    state: Mutex<State>,
    wake: Condvar,
    notify: tokio::sync::Notify,
}
impl Shared {
    fn changed(&self) {
        self.wake.notify_all();
        self.notify.notify_waiters();
    }
}
#[derive(Default)]
pub struct IndexScheduler {
    shared: Arc<Shared>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}
pub struct Admission {
    scheduler: Arc<IndexScheduler>,
    id: u64,
    queued: bool,
}
pub struct Cohort {
    scheduler: Arc<IndexScheduler>,
    id: u64,
    cancel: Arc<AtomicBool>,
}
struct CpuLease {
    shared: Arc<Shared>,
}
/// Actual read-phase demand only; never retained by a completed query token.
/// The blocking worker owns this even when its asynchronous waiter is dropped.
pub struct InteractiveActivity {
    shared: Arc<Shared>,
}
impl Drop for InteractiveActivity {
    fn drop(&mut self) {
        let mut s=self.shared.state.lock().unwrap_or_else(|p|p.into_inner());
        s.interactive=s.interactive.checked_sub(1).expect("owned interactive activity");
        drop(s);self.shared.changed();
    }
}
impl Drop for CpuLease {
    fn drop(&mut self) {
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        s.active -= 1;
        s.apply_drained();
        drop(s);
        self.shared.changed();
    }
}
struct Local {
    scheduler: Arc<IndexScheduler>,
    cohort: u64,
    cancel: Arc<AtomicBool>,
    cpu: RefCell<Option<CpuLease>>,
}
thread_local! {static CURRENT:RefCell<Option<Rc<Local>>>=const{RefCell::new(None)};}
fn cpu(shared: &Arc<Shared>, cohort: u64, cancel: &AtomicBool) -> Result<CpuLease, String> {
    let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
    let id = s.next()?;
    if s.waiters.len() >= 64 {
        return Err("unavailable: indexing CPU queue full".into());
    }
    s.waiters.push_back((id, cohort));
    loop {
        if s.closing || cancel.load(Ordering::Acquire) || !s.cohorts.contains_key(&cohort) {
            s.waiters.retain(|(n, _)| *n != id);
            drop(s);
            shared.changed();
            return Err("cancelled: indexing execution".into());
        }
        if s.active < s.effective && s.waiters.front().is_some_and(|(n, _)| *n == id) {
            s.waiters.pop_front();
            s.active += 1;
            #[cfg(test)]
            {
                s.observation.cpu_peak = s.observation.cpu_peak.max(s.active);
            }
            drop(s);
            shared.changed();
            return Ok(CpuLease {
                shared: shared.clone(),
            });
        }
        s = shared.wake.wait(s).unwrap_or_else(|p| p.into_inner());
    }
}
impl IndexScheduler {
    /// Numeric test-only transition accounting, not sampling or a heap claim.
    #[cfg(test)]
    pub(crate) fn test_observation(&self, reset: bool) -> TestObservation {
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        let result = s.observation.clone();
        if reset {
            assert!(s.active == 0 && s.inflight == 0 && s.cohorts.is_empty());
            assert_eq!(s.data_bytes, 0);
            assert!(
                s.queued.is_empty()
                    && s.waiters.is_empty()
                    && s.tasks.is_empty()
                    && s.data_waiters.is_empty()
            );
            s.observation = TestObservation::default();
        }
        result
    }
    #[cfg(test)]
    pub(crate) fn test_accounting(&self) -> (usize, usize, usize, usize) {
        let s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        (
            s.data_bytes,
            s.inflight,
            s.data_waiters.len(),
            s.cohorts.len(),
        )
    }
    pub fn status(&self) -> SchedulerStatus {
        self.shared
            .state
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .status()
    }
    pub fn request(&self, request: &SchedulerRequest) -> Result<SchedulerStatus, String> {
        request.validate()?;
        match request {
            SchedulerRequest::Status { .. } => Ok(self.status()),
            SchedulerRequest::ConfigureResources {revision,max_threads,limits,..} =>
                self.configure_with_limits(*revision,usize::from(*max_threads),*limits),
            SchedulerRequest::Configure {
                revision,
                max_threads,
                ..
            } => self.configure(*revision, usize::from(*max_threads)),
        }
    }
    pub fn configure(&self, revision: u64, desired: usize) -> Result<SchedulerStatus, String> {
        self.configure_inner(revision,desired,None)
    }
    pub fn configure_with_limits(&self, revision:u64,desired:usize,limits:IndexLimits)->Result<SchedulerStatus,String>{
        limits.validate()?;
        self.configure_inner(revision,desired,Some(limits))
    }
    fn configure_inner(&self,revision:u64,desired:usize,limits:Option<IndexLimits>)->Result<SchedulerStatus,String>{
        if revision == 0 || revision > MAX_REVISION || !(1..=32).contains(&desired) {
            return Err("invalid: indexing scheduler configuration".into());
        }
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        if s.closing {
            return Err("unavailable: indexing scheduler closed".into());
        }
        let limits=limits.unwrap_or(s.desired_limits);
        if revision < s.revision {
            return Err("stale_configuration".into());
        }
        if revision == s.revision && (desired != s.desired || limits != s.desired_limits) {
            return Err("configuration_conflict".into());
        }
        s.revision = revision;
        s.desired = desired;
        s.desired_limits=limits;
        s.apply_drained();
        let status = s.status();
        drop(s);
        self.shared.changed();
        Ok(status)
    }
    pub fn reserve(self: &Arc<Self>, descriptor_bytes: usize) -> Result<Admission, String> {
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        if s.desired_limits!=s.effective_limits{return Err("configuration_pending: indexing resource limits are draining".into());}
        if s.closing
            || s.queued.len() >= MAX_QUEUED
            || descriptor_bytes > MAX_DESCRIPTOR_BYTES
            || s.descriptor_bytes
                .checked_add(descriptor_bytes)
                .is_none_or(|n| n > MAX_DESCRIPTOR_BYTES)
        {
            return Err("unavailable: indexing admission queue full".into());
        }
        let id = s.next()?;
        s.queued.push_back((id, descriptor_bytes));
        s.descriptor_bytes += descriptor_bytes;
        drop(s);
        self.shared.changed();
        Ok(Admission {
            scheduler: self.clone(),
            id,
            queued: true,
        })
    }
    pub fn wake(&self) {
        self.shared.changed();
    }
    pub fn is_current(self: &Arc<Self>) -> bool {
        CURRENT.with(|c| {
            c.borrow()
                .as_ref()
                .is_some_and(|c| Arc::ptr_eq(&c.scheduler, self))
        })
    }
    pub fn run<R>(self: &Arc<Self>, work: impl FnOnce() -> Result<R, String>) -> Result<R, String> {
        if self.is_current() {
            return work();
        }
        let cohort = self.reserve(0)?.wait_blocking()?;
        cohort.run(work)
    }
    fn local(self: &Arc<Self>) -> Result<Rc<Local>, String> {
        CURRENT
            .with(|c| {
                c.borrow()
                    .as_ref()
                    .filter(|c| Arc::ptr_eq(&c.scheduler, self))
                    .cloned()
            })
            .ok_or("invalid: indexing task outside cohort".into())
    }
    // Release coordinator CPU while waiting; never hold an FM lock here. The
    // returned coordinator must re-acquire a counted lease before doing work.
    pub fn parked<R>(self: &Arc<Self>, wait: impl FnOnce() -> R) -> Result<R, String> {
        let local = self.local()?;
        drop(local.cpu.borrow_mut().take());
        let result = wait();
        let permit = cpu(&self.shared, local.cohort, &local.cancel)?;
        *local.cpu.borrow_mut() = Some(permit);
        Ok(result)
    }
    pub fn interactive_activity(self:&Arc<Self>)->InteractiveActivity {
        let mut s=self.shared.state.lock().unwrap_or_else(|p|p.into_inner());
        s.interactive=s.interactive.checked_add(1).expect("bounded query lane activity");
        drop(s);self.shared.changed();InteractiveActivity{shared:self.shared.clone()}
    }
    /// Verification-only cooperative boundary. Call once before a file/batch,
    /// without FM/authority/result locks; a deadline always allows that next
    /// unit to proceed despite sustained interactive demand. Not preemptive.
    pub fn verification_checkpoint(self:&Arc<Self>)->Result<(),String>{
        self.verification_checkpoint_for(std::time::Duration::from_secs(1))
    }
    fn verification_checkpoint_for(self:&Arc<Self>,quantum:std::time::Duration)->Result<(),String>{
        let local=self.local()?;
        {
            let mut s=self.shared.state.lock().unwrap_or_else(|p|p.into_inner());
            if s.closing||local.cancel.load(Ordering::Acquire){return Err("cancelled: verification yield".into());}
            if s.interactive==0{return Ok(());}
            #[cfg(test)] {s.observation.interactive_waits+=1;}
        }
        let deadline=std::time::Instant::now()+quantum;
        self.parked(||{
            let mut s=self.shared.state.lock().unwrap_or_else(|p|p.into_inner());
            loop {
                if s.closing||local.cancel.load(Ordering::Acquire){return Err("cancelled: verification yield".into());}
                if s.interactive==0{return Ok(());}
                let remaining=deadline.saturating_duration_since(std::time::Instant::now());
                if remaining.is_zero(){
                    #[cfg(test)] {s.observation.interactive_deadlines+=1;}
                    return Ok(());
                }
                s=self.shared.wake.wait_timeout(s,remaining).unwrap_or_else(|p|p.into_inner()).0;
            }
        })?
    }
    pub fn try_data(self: &Arc<Self>, bytes: usize) -> Result<Option<DataLease>, String> {
        let local = self.local()?;
        if bytes > MAX_DATA_BYTES {
            return Err("budget_exceeded: indexing data reservation".into());
        }
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        if s.closing || local.cancel.load(Ordering::Acquire) {
            return Err("cancelled: indexing data reservation".into());
        }
        if !s.data_waiters.is_empty()
            || s.data_bytes
                .checked_add(bytes)
                .is_none_or(|n| n > MAX_DATA_BYTES)
        {
            return Ok(None);
        }
        s.data_bytes += bytes;
        Ok(Some(DataLease {
            reservation: Arc::new(DataReservation {
                shared: self.shared.clone(),
                bytes,
            }),
        }))
    }
    pub fn data(self: &Arc<Self>, bytes: usize) -> Result<DataLease, String> {
        if let Some(lease) = self.try_data(bytes)? {
            return Ok(lease);
        }
        let local = self.local()?;
        let id = {
            let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
            if s.data_waiters.len() >= 32 {
                return Err("unavailable: indexing data queue full".into());
            }
            let id = s.next()?;
            s.data_waiters.push_back(id);
            id
        };
        // FIFO prevents a stream of smaller newcomers starving an exclusive
        // retry. Callers must first drain/drop their own pending reservations.
        self.parked(|| {
            let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
            loop {
                if s.closing || local.cancel.load(Ordering::Acquire) {
                    s.data_waiters.retain(|candidate| *candidate != id);
                    drop(s);
                    self.shared.changed();
                    return Err("cancelled: indexing data reservation".into());
                }
                if s.data_waiters.front() == Some(&id)
                    && s.data_bytes
                        .checked_add(bytes)
                        .is_some_and(|n| n <= MAX_DATA_BYTES)
                {
                    s.data_waiters.pop_front();
                    s.data_bytes += bytes;
                    drop(s);
                    self.shared.changed();
                    return Ok(DataLease {
                        reservation: Arc::new(DataReservation {
                            shared: self.shared.clone(),
                            bytes,
                        }),
                    });
                }
                s = self.shared.wake.wait(s).unwrap_or_else(|p| p.into_inner());
            }
        })?
    }
    fn ensure_workers(&self) -> Result<(), String> {
        let desired = self.status().effective;
        let mut workers = self.workers.lock().unwrap_or_else(|p| p.into_inner());
        while workers.len() < desired {
            let shared = self.shared.clone();
            let worker = std::thread::Builder::new()
                .name(format!("code-cpu-{}", workers.len()))
                .spawn(move || loop {
                    let task = {
                        let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
                        while s.tasks.is_empty() && !s.closing {
                            s = shared.wake.wait(s).unwrap_or_else(|p| p.into_inner());
                        }
                        match s.next_task() {
                            Some(task) => task,
                            None => break,
                        }
                    };
                    let permit = cpu(&shared, task.cohort, &task.cancel).ok();
                    #[cfg(test)]
                    if permit.is_some() {
                        let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
                        s.observation.worker_active += 1;
                        s.observation.worker_peak =
                            s.observation.worker_peak.max(s.observation.worker_active);
                        s.observation.worker_attempts += 1;
                    }
                    let deliver = (task.run)(permit.is_some());
                    #[cfg(test)]
                    if permit.is_some() {
                        let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
                        s.observation.worker_active -= 1;
                    }
                    drop(permit);
                    // Delivery/discard owns the reservation too. Do not let a cohort/config
                    // drain become observable while that final action can still own payloads.
                    deliver();
                    let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
                    s.inflight -= 1;
                    if let Some((_, tasks)) = s.cohorts.get_mut(&task.cohort) {
                        *tasks -= 1;
                    }
                    drop(s);
                    shared.changed();
                })
                .map_err(|_| "unavailable: indexing worker creation".to_string())?;
            workers.push(worker);
        }
        Ok(())
    }
    pub fn submit<R: Send + 'static>(
        self: &Arc<Self>,
        data: DataLease,
        work: impl FnOnce() -> Result<R, String> + Send + 'static,
    ) -> Result<TaskHandle<R>, String> {
        if !Arc::ptr_eq(&data.reservation.shared, &self.shared) {
            return Err("invalid: indexing reservation owner differs".into());
        }
        let local = self.local()?;
        self.ensure_workers()?;
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        if s.closing || s.inflight >= 32 || local.cancel.load(Ordering::Acquire) {
            return Err("unavailable: indexing worker queue full".into());
        }
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let metrics = crate::code_metrics::current_context();
        let task_data = data.clone();
        let action = move |allowed: bool| -> Box<dyn FnOnce() + Send> {
            let result = if !allowed {
                Err("cancelled: indexing worker".into())
            } else {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match metrics {
                    Some(context) => context.enter(work),
                    None => work(),
                }))
                .unwrap_or_else(|_| Err("index_worker_failed: worker panic".into()))
            };
            Box::new(move || {
                let result = result.map(|value| TaskResult {
                    value,
                    _data: task_data,
                });
                let _ = tx.send(result);
            })
        };
        s.cohorts
            .get_mut(&local.cohort)
            .ok_or("cancelled: indexing cohort")?
            .1 += 1;
        s.inflight += 1;
        s.tasks.push_back(Task {
            cohort: local.cohort,
            cancel: local.cancel.clone(),
            run: Box::new(action),
        });
        drop(s);
        self.shared.changed();
        Ok(TaskHandle {
            rx,
            data,
            _scheduler: self.clone(),
        })
    }
    pub fn shutdown(&self) {
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        s.closing = true;
        for (cancel, _) in s.cohorts.values() {
            cancel.store(true, Ordering::Release);
        }
        drop(s);
        self.shared.changed();
    }
}
#[cfg(test)]
#[derive(Clone, Debug, Default, Serialize)]
pub(crate) struct TestObservation {
    pub cpu_peak: usize,
    pub worker_peak: usize,
    pub worker_active: usize,
    pub worker_attempts: usize,
    pub interactive_waits: usize,
    pub interactive_deadlines: usize,
}
impl Drop for IndexScheduler {
    fn drop(&mut self) {
        self.shutdown();
        for worker in self
            .workers
            .get_mut()
            .unwrap_or_else(|p| p.into_inner())
            .drain(..)
        {
            let _ = worker.join();
        }
    }
}
impl Admission {
    fn enter(&mut self, s: &mut State, cancel: Arc<AtomicBool>) -> Option<Cohort> {
        if !s.admit(self.id) {
            return None;
        }
        s.cohorts.insert(self.id, (cancel.clone(), 0));
        self.queued = false;
        Some(Cohort {
            scheduler: self.scheduler.clone(),
            id: self.id,
            cancel,
        })
    }
    pub async fn wait(mut self, cancel: &Arc<AtomicBool>) -> Result<Cohort, String> {
        loop {
            let shared = self.scheduler.shared.clone();
            let changed = shared.notify.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            {
                let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
                if s.closing || cancel.load(Ordering::Acquire) {
                    return Err("cancelled: queued index".into());
                }
                if let Some(cohort) = self.enter(&mut s, cancel.clone()) {
                    drop(s);
                    shared.changed();
                    return Ok(cohort);
                }
            }
            changed.await;
        }
    }
    fn wait_blocking(mut self) -> Result<Cohort, String> {
        let shared = self.scheduler.shared.clone();
        let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
        loop {
            if s.closing {
                return Err("cancelled: queued index".into());
            }
            if let Some(cohort) = self.enter(&mut s, Arc::new(AtomicBool::new(false))) {
                drop(s);
                shared.changed();
                return Ok(cohort);
            }
            s = shared.wake.wait(s).unwrap_or_else(|p| p.into_inner());
        }
    }
}
impl Drop for Admission {
    fn drop(&mut self) {
        if self.queued {
            let mut s = self
                .scheduler
                .shared
                .state
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            if let Some(i) = s.queued.iter().position(|(id, _)| *id == self.id) {
                let (_, bytes) = s.queued.remove(i).unwrap();
                s.descriptor_bytes -= bytes;
            }
            drop(s);
            self.scheduler.wake();
        }
    }
}
impl Cohort {
    pub fn run<R>(&self, work: impl FnOnce() -> Result<R, String>) -> Result<R, String> {
        let permit = cpu(&self.scheduler.shared, self.id, &self.cancel)?;
        let local = Rc::new(Local {
            scheduler: self.scheduler.clone(),
            cohort: self.id,
            cancel: self.cancel.clone(),
            cpu: RefCell::new(Some(permit)),
        });
        struct Restore(Option<Rc<Local>>);
        impl Drop for Restore {
            fn drop(&mut self) {
                CURRENT.with(|c| {
                    c.replace(self.0.take());
                });
            }
        }
        let _restore = Restore(CURRENT.with(|c| c.replace(Some(local))));
        work()
    }
}
impl Drop for Cohort {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::Release);
        self.scheduler.wake();
        let shared = &self.scheduler.shared;
        let mut s = shared.state.lock().unwrap_or_else(|p| p.into_inner());
        while s.cohorts.get(&self.id).is_some_and(|(_, n)| *n > 0) {
            s = shared.wake.wait(s).unwrap_or_else(|p| p.into_inner());
        }
        s.cohorts.remove(&self.id);
        s.apply_drained();
        drop(s);
        shared.changed();
    }
}
// Shared accounting follows queued work, executing work AND delivered results,
// even if the coordinator drops its handle. No naked result extraction API.
#[derive(Clone)]
pub struct DataLease {
    reservation: Arc<DataReservation>,
}
struct DataReservation {
    shared: Arc<Shared>,
    bytes: usize,
}
impl Drop for DataReservation {
    fn drop(&mut self) {
        let mut s = self.shared.state.lock().unwrap_or_else(|p| p.into_inner());
        s.data_bytes -= self.bytes;
        drop(s);
        self.shared.changed();
    }
}
pub struct TaskResult<R> {
    value: R,
    _data: DataLease,
}
impl<R> std::ops::Deref for TaskResult<R> {
    type Target = R;
    fn deref(&self) -> &R {
        &self.value
    }
}
impl<R> std::fmt::Debug for TaskResult<R> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ReservedTaskResult")
    }
}
pub struct TaskHandle<R> {
    rx: std::sync::mpsc::Receiver<Result<TaskResult<R>, String>>,
    data: DataLease,
    _scheduler: Arc<IndexScheduler>,
}
impl<R> TaskHandle<R> {
    pub fn wait(&self) -> Result<TaskResult<R>, String> {
        self._scheduler.parked(|| {
            self.rx
                .recv()
                .unwrap_or_else(|_| Err("index_worker_failed: worker result lost".into()))
        })?
    }
    pub fn reserved_bytes(&self) -> usize {
        self.data.reservation.bytes
    }
}

#[cfg(test)]
#[path = "code_index_scheduler_tests.rs"]
mod tests;
