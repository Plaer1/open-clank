use super::*;
use std::sync::{mpsc, Barrier};

fn wait_interactive_park(scheduler:&IndexScheduler) {
    let state=scheduler.shared.state.lock().unwrap();
    let (state,timeout)=scheduler.shared.wake.wait_timeout_while(state,std::time::Duration::from_secs(2),|s|s.observation.interactive_waits==0||s.active!=0).unwrap();
    assert!(!timeout.timed_out()&&state.active==0);
}

#[test]
fn interactive_activity_drop_and_unwind_leave_no_priority_claim() {
    let scheduler=Arc::new(IndexScheduler::default());let original=scheduler.status();
    let first=scheduler.interactive_activity();let second=scheduler.interactive_activity();
    assert_eq!(scheduler.shared.state.lock().unwrap().interactive,2);assert_eq!(scheduler.status(),original);
    drop(first);assert_eq!(scheduler.shared.state.lock().unwrap().interactive,1);drop(second);
    let _=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{let _activity=scheduler.interactive_activity();panic!("owned query unwinds");}));
    assert_eq!(scheduler.shared.state.lock().unwrap().interactive,0);assert_eq!(scheduler.status(),original);
}

#[test]
fn interactive_checkpoint_parks_cpu_resumes_and_retains_configuration_drain() {
    let scheduler=Arc::new(IndexScheduler::default());let activity=scheduler.interactive_activity();
    let worker=scheduler.clone();let (done,seen)=mpsc::channel();
    let thread=std::thread::spawn(move||worker.run(||{worker.verification_checkpoint()?;done.send(worker.status().active).unwrap();Ok(())}));
    wait_interactive_park(&scheduler);
    let changed=scheduler.configure(1,1).unwrap();assert_eq!((changed.desired,changed.effective,changed.active,changed.drain_pending),(1,2,0,true));
    drop(activity);assert_eq!(seen.recv_timeout(std::time::Duration::from_secs(2)).unwrap(),1);thread.join().unwrap().unwrap();
    let status=scheduler.status();assert_eq!((status.active,status.desired,status.effective,status.drain_pending),(0,1,1,false));
}

#[test]
fn interactive_checkpoint_cancellation_and_shutdown_wake_without_guard_drop() {
    for shutdown in [false,true] {
        let scheduler=Arc::new(IndexScheduler::default());let _activity=scheduler.interactive_activity();let worker=scheduler.clone();
        let thread=std::thread::spawn(move||worker.run(||worker.verification_checkpoint()));
        wait_interactive_park(&scheduler);
        if shutdown{scheduler.shutdown();}else{
            let state=scheduler.shared.state.lock().unwrap();assert_eq!(state.cohorts.len(),1);
            state.cohorts.values().next().unwrap().0.store(true,Ordering::Release);drop(state);scheduler.wake();
        }
        assert!(thread.join().unwrap().unwrap_err().starts_with("cancelled:"));assert_eq!(scheduler.status().active,0);
        assert!(scheduler.shared.state.lock().unwrap().cohorts.is_empty());
    }
}

#[test]
fn interactive_checkpoint_deadline_guarantees_progress_under_sustained_queries() {
    let scheduler=Arc::new(IndexScheduler::default());let _activity=scheduler.interactive_activity();
    let mut files=0;
    scheduler.run(||{for _ in 0..3{scheduler.verification_checkpoint_for(std::time::Duration::from_millis(10))?;files+=1;}Ok(())}).unwrap();
    assert_eq!(files,3);let state=scheduler.shared.state.lock().unwrap();
    assert_eq!((state.interactive,state.observation.interactive_waits,state.observation.interactive_deadlines),(1,3,3));
    assert_eq!(state.observation.cpu_peak,1);
}

#[test]
fn interactive_checkpoint_idle_path_keeps_existing_cpu_lease() {
    let scheduler=Arc::new(IndexScheduler::default());
    scheduler.run(||{scheduler.verification_checkpoint()?;assert_eq!(scheduler.status().active,1);Ok(())}).unwrap();
    let state=scheduler.shared.state.lock().unwrap();assert_eq!(state.observation.interactive_waits,0);assert_eq!(state.active,0);
}

#[test]
fn strict_wire_revision_default_and_no_cold_workers() {
    let scheduler = Arc::new(IndexScheduler::default());
    let status = scheduler.status();
    assert_eq!(
        (
            status.desired,
            status.effective,
            status.revision,
            status.active,
            status.queued
        ),
        (2, 2, 0, 0, 0)
    );
    assert!(scheduler.workers.lock().unwrap().is_empty());
    for raw in [
        r#"{"action":"configure","origin":"app","revision":1,"max_threads":true}"#,
        r#"{"action":"configure","origin":"app","revision":1,"max_threads":1.5}"#,
        r#"{"action":"configure","origin":"app","revision":1,"max_threads":"2"}"#,
        r#"{"action":"status","origin":"app","max_threads":2}"#,
    ] {
        assert!(serde_json::from_str::<SchedulerRequest>(raw).is_err());
    }
    for (request, valid) in [
        (
            SchedulerRequest::Status {
                origin: "agent".into(),
            },
            false,
        ),
        (
            SchedulerRequest::Configure {
                origin: "app".into(),
                revision: 0,
                max_threads: 2,
            },
            false,
        ),
        (
            SchedulerRequest::Configure {
                origin: "app".into(),
                revision: 1,
                max_threads: 0,
            },
            false,
        ),
        (
            SchedulerRequest::Configure {
                origin: "app".into(),
                revision: 1,
                max_threads: 33,
            },
            false,
        ),
    ] {
        assert_eq!(request.validate().is_ok(), valid);
        assert!(scheduler.request(&request).is_err());
        assert_eq!(scheduler.status(), status);
    }
    assert_eq!(scheduler.configure(1, 4).unwrap().effective, 4);
    let saved = scheduler.status();
    assert_eq!(scheduler.configure(1, 4).unwrap(), saved);
    assert!(scheduler.configure(1, 3).is_err());
    assert!(scheduler.configure(0, 4).is_err());
    assert!(scheduler.configure(MAX_REVISION + 1, 4).is_err());
    assert_eq!(scheduler.status(), saved);
    assert!(scheduler.workers.lock().unwrap().is_empty());
    assert_eq!(
        serde_json::to_value(saved)
            .unwrap()
            .as_object()
            .unwrap()
            .len(),
        8
    );
}

#[test]
fn cohort_queue_count_bytes_and_drop_are_bounded() {
    let scheduler = Arc::new(IndexScheduler::default());
    let mut queue = Vec::new();
    for _ in 0..MAX_QUEUED {
        queue.push(scheduler.reserve(1).unwrap());
    }
    assert_eq!(scheduler.status().queued, 32);
    assert!(scheduler.reserve(1).is_err());
    drop(queue);
    assert_eq!(scheduler.status().queued, 0);
    let whole = scheduler.reserve(MAX_DESCRIPTOR_BYTES).unwrap();
    assert!(scheduler.reserve(1).is_err());
    drop(whole);
    assert!(scheduler.reserve(MAX_DESCRIPTOR_BYTES + 1).is_err());
    assert_eq!(scheduler.status().queued, 0);
    assert!(scheduler.workers.lock().unwrap().is_empty());
}

#[test]
fn actual_one_two_and_four_worker_execution_counts_coordinator_and_yields() {
    for count in [1, 2, 4] {
        let scheduler = Arc::new(IndexScheduler::default());
        scheduler.configure(1, count).unwrap();
        let barrier = Arc::new(Barrier::new(count));
        let peak = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        scheduler
            .run(|| {
                assert_eq!(scheduler.status().active, 1, "coordinator counts");
                let mut handles = Vec::new();
                for n in 0..count {
                    let b = barrier.clone();
                    let observed = scheduler.clone();
                    let peak = peak.clone();
                    let data = scheduler.data(1)?;
                    handles.push(scheduler.submit(data, move || {
                        let active = observed.status().active;
                        assert!(active <= count);
                        peak.fetch_max(active, Ordering::SeqCst);
                        b.wait();
                        Ok(n)
                    })?);
                }
                let mut values = Vec::new();
                for handle in handles {
                    values.push(*handle.wait()?);
                }
                assert_eq!(values, (0..count).collect::<Vec<_>>());
                assert_eq!(scheduler.status().active, 1);
                Ok(())
            })
            .unwrap();
        assert_eq!(peak.load(Ordering::SeqCst), count);
        assert_eq!(scheduler.status().active, 0);
        assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 0);
    }
}

#[test]
fn configuration_waits_for_cohort_drain_then_applies_latest_before_new_admission() {
    let scheduler = Arc::new(IndexScheduler::default());
    let (entered, seen) = mpsc::channel();
    let (release, released) = mpsc::channel();
    let first = scheduler.clone();
    let old = std::thread::spawn(move || {
        first.run(|| {
            entered.send(()).unwrap();
            released.recv().unwrap();
            Ok(())
        })
    });
    seen.recv().unwrap();
    let pending = scheduler.configure(1, 4).unwrap();
    assert!(pending.drain_pending);
    assert_eq!(pending.effective, 2);
    let newest = scheduler.configure(2, 1).unwrap();
    assert_eq!((newest.desired, newest.effective), (1, 2));
    assert!(scheduler.configure(1, 3).is_err());
    let admission = scheduler.reserve(0).unwrap();
    let (second_enter, second_seen) = mpsc::channel();
    let next = std::thread::spawn(move || {
        let cohort = admission.wait_blocking().unwrap();
        cohort
            .run(|| {
                let status = cohort.scheduler.status();
                assert_eq!(
                    (status.revision, status.effective_revision, status.effective),
                    (2, 2, 1)
                );
                second_enter.send(()).unwrap();
                Ok(())
            })
            .unwrap();
    });
    assert!(second_seen.try_recv().is_err());
    assert_eq!(scheduler.status().queued, 1);
    release.send(()).unwrap();
    old.join().unwrap().unwrap();
    second_seen.recv().unwrap();
    next.join().unwrap();
    let done = scheduler.status();
    assert_eq!((done.active, done.queued), (0, 0));
    assert!(!done.drain_pending);
}

#[test]
fn memory_is_reserved_before_submission_and_panic_releases_all_resources() {
    let scheduler = Arc::new(IndexScheduler::default());
    scheduler
        .run(|| {
            let all = scheduler.data(MAX_DATA_BYTES)?;
            assert!(scheduler.try_data(1)?.is_none());
            assert!(scheduler.try_data(MAX_DATA_BYTES + 1).is_err());
            drop(all);
            let data = scheduler.data(7)?;
            let handle = scheduler.submit::<()>(data, || panic!("synthetic worker panic"))?;
            assert!(handle.wait().unwrap_err().contains("worker panic"));
            drop(handle);
            assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 0);
            let next = scheduler.submit(scheduler.data(3)?, || Ok(17))?;
            assert_eq!(*next.wait()?, 17);
            drop(next);
            Ok(())
        })
        .unwrap();
    let state = scheduler.shared.state.lock().unwrap();
    assert_eq!((state.active, state.inflight, state.data_bytes), (0, 0, 0));
    assert!(state.waiters.is_empty());
    assert!(state.cohorts.is_empty());
}

#[test]
fn metrics_context_is_explicitly_propagated_to_actual_workers_before_snapshot() {
    use crate::code_metrics::{capture_code_metrics, span, Stage};
    let scheduler = Arc::new(IndexScheduler::default());
    let (result, metrics) = capture_code_metrics(|| {
        scheduler.run(|| {
            let mut work = Vec::new();
            for _ in 0..2 {
                work.push(scheduler.submit(scheduler.data(1)?, || {
                    let mut stage = span(Stage::Extract);
                    stage.add(1, 7);
                    Ok(())
                })?);
            }
            for handle in work {
                handle.wait()?;
            }
            Ok(())
        })
    });
    result.unwrap();
    let stage = &metrics.stages[Stage::Extract as usize].counters;
    assert_eq!((stage.calls, stage.items, stage.bytes), (2, 2, 14));
    assert_eq!(scheduler.status().active, 0);
}

#[tokio::test]
async fn queued_cancel_and_closed_owner_do_not_admit_or_restart() {
    let scheduler = Arc::new(IndexScheduler::default());
    let ticket = scheduler.reserve(1).unwrap();
    let cancelled = Arc::new(AtomicBool::new(true));
    assert!(ticket.wait(&cancelled).await.is_err());
    assert_eq!(scheduler.status().queued, 0);
    scheduler.shutdown();
    assert!(scheduler.reserve(0).is_err());
    assert!(scheduler.configure(1, 2).is_err());
    assert!(scheduler.workers.lock().unwrap().is_empty());
}

#[tokio::test]
async fn admitted_async_owner_retains_the_same_external_cancellation_token() {
    let scheduler = Arc::new(IndexScheduler::default());
    let cancelled = Arc::new(AtomicBool::new(false));
    let cohort = scheduler
        .reserve(0)
        .unwrap()
        .wait(&cancelled)
        .await
        .unwrap();
    assert!(Arc::ptr_eq(&cancelled, &cohort.cancel));
    cancelled.store(true, Ordering::Release);
    scheduler.wake();
    assert!(cohort.run(|| Ok(())).unwrap_err().starts_with("cancelled:"));
    drop(cohort);
    assert_eq!(scheduler.status().active, 0);
}

struct PayloadDrop(Arc<AtomicBool>);
impl Drop for PayloadDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

#[test]
fn dropped_queued_handle_retains_payload_charge_until_cancelled_task_discard() {
    let scheduler = Arc::new(IndexScheduler::default());
    scheduler.configure(1, 1).unwrap();
    let dropped = Arc::new(AtomicBool::new(false));
    scheduler
        .run(|| {
            let payload = PayloadDrop(dropped.clone());
            let handle = scheduler.submit(scheduler.data(29)?, move || {
                drop(payload);
                Ok(())
            })?;
            // Coordinator holds the only CPU lease: work cannot start yet.
            drop(handle);
            assert!(!dropped.load(Ordering::SeqCst));
            assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 29);
            Ok(())
        })
        .unwrap();
    assert!(dropped.load(Ordering::SeqCst));
    let state = scheduler.shared.state.lock().unwrap();
    assert_eq!((state.data_bytes, state.inflight), (0, 0));
    assert!(state.cohorts.is_empty());
}

#[test]
fn dropped_running_handle_and_pending_configuration_wait_for_actual_payload_discard() {
    let scheduler = Arc::new(IndexScheduler::default());
    let dropped = Arc::new(AtomicBool::new(false));
    let (entered, seen) = mpsc::channel();
    let (release, released) = mpsc::channel();
    scheduler
        .run(|| {
            let payload = PayloadDrop(dropped.clone());
            let handle = scheduler.submit(scheduler.data(31)?, move || {
                entered.send(()).unwrap();
                released.recv().unwrap();
                Ok(payload)
            })?;
            scheduler.parked(|| seen.recv().unwrap())?;
            drop(handle);
            assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 31);
            assert!(!dropped.load(Ordering::SeqCst));
            let pending = scheduler.configure(1, 1)?;
            assert!(pending.drain_pending);
            assert_eq!(pending.effective, 2);
            release.send(()).unwrap();
            Ok(())
        })
        .unwrap();
    assert!(dropped.load(Ordering::SeqCst));
    let state = scheduler.shared.state.lock().unwrap();
    assert_eq!(
        (state.data_bytes, state.inflight, state.effective_revision),
        (0, 0, 1)
    );
    assert!(state.cohorts.is_empty());
}

#[test]
fn delivered_result_keeps_reservation_after_handle_and_cohort_drop() {
    let scheduler = Arc::new(IndexScheduler::default());
    let dropped = Arc::new(AtomicBool::new(false));
    let result = scheduler
        .run(|| {
            let payload = PayloadDrop(dropped.clone());
            let handle = scheduler.submit(scheduler.data(37)?, move || Ok(payload))?;
            let result = handle.wait()?;
            drop(handle);
            assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 37);
            Ok(result)
        })
        .unwrap();
    assert!(!dropped.load(Ordering::SeqCst));
    assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 37);
    drop(result);
    assert!(dropped.load(Ordering::SeqCst));
    assert_eq!(scheduler.shared.state.lock().unwrap().data_bytes, 0);
}

#[test]
fn task_ready_queue_round_robins_all_cohorts_and_retains_each_cohort_fifo() {
    let mut state = State::default();
    for id in [4, 4, 4, 8, 8, 12, 12] {
        state.tasks.push_back(Task {
            cohort: id,
            cancel: Arc::new(AtomicBool::new(false)),
            run: Box::new(|_| Box::new(|| {})),
        });
    }
    let mut order = Vec::new();
    while let Some(task) = state.next_task() {
        order.push(task.cohort);
    }
    assert_eq!(order, [4, 8, 12, 4, 8, 12, 4]);
}

#[test]
fn exclusive_data_waiter_is_not_starved_by_smaller_newcomer() {
    let scheduler = Arc::new(IndexScheduler::default());
    let held = scheduler.run(|| scheduler.data(1)).unwrap();
    let (large_enter, large_seen) = mpsc::channel();
    let (release, released) = mpsc::channel();
    let first = scheduler.clone();
    let large = std::thread::spawn(move || {
        first.run(|| {
            let _all = first.data(MAX_DATA_BYTES)?;
            large_enter.send(()).unwrap();
            released.recv().unwrap();
            Ok(())
        })
    });
    let wait_for_queue = |count| {
        let mut state = scheduler.shared.state.lock().unwrap();
        while state.data_waiters.len() < count {
            state = scheduler.shared.wake.wait(state).unwrap();
        }
    };
    wait_for_queue(1);
    let (small_enter, small_seen) = mpsc::channel();
    let next = scheduler.clone();
    let small = std::thread::spawn(move || {
        next.run(|| {
            let _one = next.data(1)?;
            small_enter.send(()).unwrap();
            Ok(())
        })
    });
    wait_for_queue(2);
    assert!(small_seen.try_recv().is_err());
    drop(held);
    large_seen.recv().unwrap();
    assert!(small_seen.try_recv().is_err());
    release.send(()).unwrap();
    large.join().unwrap().unwrap();
    small_seen.recv().unwrap();
    small.join().unwrap().unwrap();
    let state = scheduler.shared.state.lock().unwrap();
    assert_eq!(state.data_bytes, 0);
    assert!(state.data_waiters.is_empty());
}

#[test]
fn task_reservation_cannot_be_charged_to_a_different_scheduler() {
    let first = Arc::new(IndexScheduler::default());
    let second = Arc::new(IndexScheduler::default());
    first
        .run(|| {
            let lease = first.data(3)?;
            second.run(|| {
                assert!(second
                    .submit(lease, || Ok(()))
                    .err()
                    .unwrap()
                    .contains("owner differs"));
                Ok(())
            })
        })
        .unwrap();
    assert_eq!(first.shared.state.lock().unwrap().data_bytes, 0);
    assert!(second.workers.lock().unwrap().is_empty());
}
