#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use openclank_desktop_admission::{navigation_is_allowed, Backend, ShellResult};
use std::env;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tauri::webview::{NewWindowResponse, PageLoadEvent, WebviewWindowBuilder};
use tauri::{RunEvent, WebviewUrl, WindowEvent};

fn main() {
    if let Err(error) = run() {
        eprintln!("OPEN_CLANK_SHELL_EVENT fatal error={error}");
        std::process::exit(1);
    }
}

fn run() -> ShellResult<()> {
    if !cfg!(target_os = "macos") {
        return Err("the WKWebView admission shell is macOS-only".into());
    }

    let backend = Arc::new(Mutex::new(Backend::prepare()?));
    let origin = backend
        .lock()
        .map_err(|_| "desktop backend lock was poisoned")?
        .origin()
        .clone();
    let admission_expected_path = env::var("OPEN_CLANK_SHELL_ADMISSION_EXPECT_PATH")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| value.starts_with('/') && !value.contains(['?', '#']));
    let admission_observed = Arc::new(AtomicBool::new(false));

    let setup_origin = origin.clone();
    let setup_expected_path = admission_expected_path.clone();
    let setup_observed = admission_observed.clone();
    let app = tauri::Builder::default()
        // Deliberately no invoke_handler and no plugins. The external origin is
        // not named by a capability, so it receives zero native API surface.
        .setup(move |app| {
            let navigation_origin = setup_origin.clone();
            let page_origin = setup_origin.clone();
            let page_expected_path = setup_expected_path.clone();
            let page_observed = setup_observed.clone();
            let window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::External(setup_origin.clone()),
            )
            .title("Open Clank")
            .inner_size(1280.0, 820.0)
            .min_inner_size(900.0, 600.0)
            .visible(false)
            .on_navigation(move |candidate| {
                let allowed = navigation_is_allowed(&navigation_origin, candidate);
                if !allowed {
                    eprintln!(
                        "OPEN_CLANK_SHELL_EVENT navigation-denied scheme={} host={}",
                        candidate.scheme(),
                        candidate.host_str().unwrap_or("")
                    );
                }
                allowed
            })
            .on_new_window(|_, _| NewWindowResponse::Deny)
            .on_page_load(move |window, payload| {
                let allowed = navigation_is_allowed(&page_origin, payload.url());
                eprintln!(
                    "OPEN_CLANK_SHELL_EVENT page-load event={:?} allowed={} path={}",
                    payload.event(),
                    allowed,
                    payload.url().path()
                );
                if payload.event() != PageLoadEvent::Finished || !allowed {
                    return;
                }
                let _ = window.show();
                let expected_route_finished = page_expected_path.as_deref().is_some_and(
                    |expected| {
                        if expected == "/" {
                            matches!(payload.url().path(), "/" | "/login")
                        } else {
                            expected == payload.url().path()
                        }
                    },
                );
                if expected_route_finished {
                    page_observed.store(true, Ordering::SeqCst);
                    eprintln!(
                        "OPEN_CLANK_SHELL_EVENT admission-auth-origin-proved origin={} path={}",
                        page_origin,
                        payload.url().path()
                    );
                    let _ = window.close();
                }
            })
            .build()?;

            if setup_expected_path.is_some() {
                let handle = app.handle().clone();
                let watchdog_observed = setup_observed.clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_secs(30));
                    if !watchdog_observed.load(Ordering::SeqCst) {
                        eprintln!(
                            "OPEN_CLANK_SHELL_EVENT admission-timeout auth-origin-proved=false"
                        );
                        handle.exit(3);
                    }
                });
            }
            let _ = window.set_focus();
            eprintln!(
                "OPEN_CLANK_SHELL_EVENT window-created engine=WKWebView origin={} native-api=disabled",
                setup_origin
            );
            Ok(())
        })
        .build(tauri::generate_context!())?;

    let event_backend = backend.clone();
    let exit_code = app.run_return(move |handle, event| match event {
        RunEvent::WindowEvent { label, event, .. }
            if label == "main" && matches!(event, WindowEvent::CloseRequested { .. }) =>
        {
            eprintln!("OPEN_CLANK_SHELL_EVENT window-close-requested label=main");
            if let Ok(mut backend) = event_backend.lock() {
                let _ = backend.shutdown();
            }
            handle.exit(0);
        }
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            if let Ok(mut backend) = event_backend.lock() {
                let _ = backend.shutdown();
            }
        }
        _ => {}
    });
    if let Ok(mut backend) = backend.lock() {
        backend.shutdown()?;
    }
    if admission_expected_path.is_some() && !admission_observed.load(Ordering::SeqCst) {
        return Err("WKWebView did not finish the expected authenticated-origin route".into());
    }
    eprintln!("OPEN_CLANK_SHELL_EVENT app-exit code={exit_code}");
    if exit_code != 0 {
        return Err(format!("Tauri event loop exited with code {exit_code}").into());
    }
    Ok(())
}
