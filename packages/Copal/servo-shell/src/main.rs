use std::env;

fn main() {
    println!("Copal Servo shell");
    let mut native_server = None;
    let (url, url_source) = resolve_target_url(&mut native_server);
    println!("target_url={url}");
    println!("copal_url_source={url_source}");

    #[cfg(feature = "servo-probe")]
    servo_probe::print_api_surface();

    #[cfg(feature = "servo-runtime")]
    {
        if let Err(error) = servo_runtime::run(&url) {
            eprintln!("servo_runtime_error={error}");
            std::process::exit(1);
        }
        return;
    }

    #[cfg(all(feature = "native-api", not(feature = "servo-runtime")))]
    {
        if env::var("COPAL_NATIVE_API_ONLY").ok().as_deref() == Some("1") {
            println!("native_api_only=1");
            loop {
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
        }
    }

    #[cfg(all(not(feature = "servo-probe"), not(feature = "servo-runtime")))]
    println!("servo_runtime=disabled; run `bun run servo:runtime-check` to cargo-check the runtime shell");
}

#[cfg(feature = "native-api")]
fn resolve_target_url(native_server: &mut Option<native_api::NativeApiServer>) -> (String, &'static str) {
    if let Ok(url) = env::var("COPAL_URL") {
        return (url, "env");
    }
    match native_api::NativeApiServer::start(native_api::NativeApiConfig::from_env()) {
        Ok(server) => {
            let url = server.url();
            println!("native_api_url={url}");
            *native_server = Some(server);
            (url, "native-api")
        },
        Err(error) => {
            eprintln!("native_api_error={error}");
            std::process::exit(1);
        },
    }
}

#[cfg(not(feature = "native-api"))]
fn resolve_target_url(_: &mut Option<()>) -> (String, &'static str) {
    (env::var("COPAL_URL").unwrap_or_else(|_| "http://127.0.0.1:3000".to_string()), "default-dev")
}

#[cfg(feature = "servo-probe")]
mod servo_probe {
    pub fn print_api_surface() {
        println!("servo_api=available");
        println!("servo_builder={}", std::any::type_name::<servo::ServoBuilder>());
        println!("webview_builder={}", std::any::type_name::<servo::WebViewBuilder>());
        println!("software_context={}", std::any::type_name::<servo::SoftwareRenderingContext>());
        println!("window_context={}", std::any::type_name::<servo::WindowRenderingContext>());
    }
}

#[cfg(feature = "servo-runtime")]
mod servo_runtime;

#[cfg(feature = "native-api")]
mod native_api;
