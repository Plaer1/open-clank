use std::cell::Cell;
use std::env;
use std::error::Error;
use std::rc::Rc;

use euclid::Scale;
use servo::{
    Code, DevicePoint, EventLoopWaker, InputEvent, Key, KeyState, KeyboardEvent, Location,
    Modifiers, MouseButton, MouseButtonAction, MouseButtonEvent, MouseLeftViewportEvent,
    MouseMoveEvent, NamedKey, RenderingContext, Servo, ServoBuilder, WebView, WebViewBuilder,
    WheelDelta, WheelEvent, WheelMode, WindowRenderingContext,
};
use url::Url;
use winit::application::ApplicationHandler;
use winit::event::{ElementState, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, EventLoop, EventLoopProxy};
use winit::keyboard::{Key as WinitKey, NamedKey as WinitNamedKey};
use winit::raw_window_handle::{HasDisplayHandle, HasWindowHandle};
use winit::window::Window;

pub fn run(target_url: &str) -> Result<(), Box<dyn Error>> {
    rustls::crypto::aws_lc_rs::default_provider()
        .install_default()
        .map_err(|_| "rustls crypto provider install failed")?;

    let event_loop = EventLoop::with_user_event().build()?;
    let mut app = CopalServoApp::new(target_url.to_string(), EventLoopWakerHandle::new(&event_loop));
    event_loop.run_app(&mut app)?;
    Ok(())
}

struct RuntimeState {
    window: Window,
    servo: Servo,
    rendering_context: Rc<WindowRenderingContext>,
    webview: WebView,
    repaint_requested: Rc<Cell<bool>>,
    cursor_point: Cell<DevicePoint>,
    smoke_exit_after_first_paint: bool,
}

struct RepaintDelegate {
    repaint_requested: Rc<Cell<bool>>,
}

impl servo::WebViewDelegate for RepaintDelegate {
    fn notify_new_frame_ready(&self, _: WebView) {
        self.repaint_requested.set(true);
    }
}

enum CopalServoApp {
    Boot {
        target_url: String,
        waker: EventLoopWakerHandle,
    },
    Running(Rc<RuntimeState>),
}

impl CopalServoApp {
    fn new(target_url: String, waker: EventLoopWakerHandle) -> Self {
        Self::Boot { target_url, waker }
    }
}

impl ApplicationHandler<CopalWakeEvent> for CopalServoApp {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        let Self::Boot { target_url, waker } = self else {
            return;
        };

        let display_handle = event_loop.display_handle().expect("display handle");
        let window = event_loop
            .create_window(Window::default_attributes().with_title("Copal"))
            .expect("create Servo window");
        let window_handle = window.window_handle().expect("window handle");
        let rendering_context = Rc::new(
            WindowRenderingContext::new(display_handle, window_handle, window.inner_size())
                .expect("create Servo rendering context"),
        );
        let _ = rendering_context.make_current();

        let servo = ServoBuilder::default()
            .event_loop_waker(Box::new(waker.clone()))
            .build();
        servo.setup_logging();

        let url = Url::parse(target_url).expect("COPAL_URL must be an absolute URL");
        let repaint_requested = Rc::new(Cell::new(false));
        let delegate = Rc::new(RepaintDelegate {
            repaint_requested: repaint_requested.clone(),
        });
        let webview = WebViewBuilder::new(&servo, rendering_context.clone())
            .url(url)
            .hidpi_scale_factor(Scale::new(window.scale_factor() as f32))
            .delegate(delegate)
            .build();

        let state = Rc::new(RuntimeState {
            window,
            servo,
            rendering_context,
            webview,
            repaint_requested,
            cursor_point: Cell::new(DevicePoint::default()),
            smoke_exit_after_first_paint: env::var("COPAL_SERVO_SMOKE").ok().as_deref() == Some("1")
                || env::var("COPAL_NATIVE_SMOKE").ok().as_deref() == Some("1"),
        });

        state.window.request_redraw();
        *self = Self::Running(state);
    }

    fn user_event(&mut self, _event_loop: &ActiveEventLoop, _event: CopalWakeEvent) {
        if let Self::Running(state) = self {
            state.servo.spin_event_loop();
            if state.repaint_requested.replace(false) {
                state.window.request_redraw();
            }
        }
    }

    fn window_event(
        &mut self,
        event_loop: &ActiveEventLoop,
        _window_id: winit::window::WindowId,
        event: WindowEvent,
    ) {
        if let Self::Running(state) = self {
            state.servo.spin_event_loop();
            if state.repaint_requested.replace(false) {
                state.window.request_redraw();
            }

            match event {
                WindowEvent::CloseRequested => event_loop.exit(),
                WindowEvent::RedrawRequested => {
                    state.webview.paint();
                    state.rendering_context.present();
                    if state.smoke_exit_after_first_paint {
                        std::process::exit(0);
                    }
                },
                WindowEvent::Resized(size) => state.webview.resize(size),
                WindowEvent::CursorMoved { position, .. } => {
                    let point = DevicePoint::new(position.x as f32, position.y as f32);
                    state.cursor_point.set(point);
                    state.webview.notify_input_event(InputEvent::MouseMove(MouseMoveEvent::new(
                        point.into(),
                    )));
                },
                WindowEvent::CursorLeft { .. } => {
                    state.webview.notify_input_event(InputEvent::MouseLeftViewport(
                        MouseLeftViewportEvent::default(),
                    ));
                },
                WindowEvent::MouseInput { state: action, button, .. } => {
                    let Some(button) = convert_mouse_button(button) else {
                        return;
                    };
                    let action = match action {
                        ElementState::Pressed => MouseButtonAction::Down,
                        ElementState::Released => MouseButtonAction::Up,
                    };
                    state.webview.notify_input_event(InputEvent::MouseButton(MouseButtonEvent::new(
                        action,
                        button,
                        state.cursor_point.get().into(),
                    )));
                },
                WindowEvent::KeyboardInput { event, .. } => {
                    if let Some((key, code)) = convert_key(&event.logical_key) {
                        let state_value = match event.state {
                            ElementState::Pressed => KeyState::Down,
                            ElementState::Released => KeyState::Up,
                        };
                        state.webview.notify_input_event(InputEvent::Keyboard(
                            KeyboardEvent::new_without_event(
                                state_value,
                                key,
                                code,
                                Location::Standard,
                                Modifiers::empty(),
                                event.repeat,
                                false,
                            ),
                        ));
                    }
                },
                WindowEvent::MouseWheel { delta, .. } => {
                    let (x, y, mode) = match delta {
                        MouseScrollDelta::LineDelta(x, y) => {
                            ((x * 76.0) as f64, (y * 76.0) as f64, WheelMode::DeltaLine)
                        },
                        MouseScrollDelta::PixelDelta(delta) => {
                            (delta.x, delta.y, WheelMode::DeltaPixel)
                        },
                    };
                    state.webview.notify_input_event(InputEvent::Wheel(WheelEvent::new(
                        WheelDelta { x, y, z: 0.0, mode },
                        state.cursor_point.get().into(),
                    )));
                },
                _ => {},
            }
        }
    }
}

fn convert_mouse_button(button: winit::event::MouseButton) -> Option<MouseButton> {
    match button {
        winit::event::MouseButton::Left => Some(MouseButton::Left),
        winit::event::MouseButton::Right => Some(MouseButton::Right),
        winit::event::MouseButton::Middle => Some(MouseButton::Middle),
        winit::event::MouseButton::Back => Some(MouseButton::Back),
        winit::event::MouseButton::Forward => Some(MouseButton::Forward),
        winit::event::MouseButton::Other(value) => Some(MouseButton::Other(value)),
    }
}

fn convert_key(key: &WinitKey) -> Option<(Key, Code)> {
    match key {
        WinitKey::Character(value) => {
            let first = value.chars().next()?;
            Some((Key::Character(value.to_string()), code_for_character(first)))
        },
        WinitKey::Named(named) => named_key(named).map(|(key, code)| (Key::Named(key), code)),
        _ => None,
    }
}

fn named_key(named: &WinitNamedKey) -> Option<(NamedKey, Code)> {
    match named {
        WinitNamedKey::Backspace => Some((NamedKey::Backspace, Code::Backspace)),
        WinitNamedKey::Tab => Some((NamedKey::Tab, Code::Tab)),
        WinitNamedKey::Enter => Some((NamedKey::Enter, Code::Enter)),
        WinitNamedKey::Escape => Some((NamedKey::Escape, Code::Escape)),
        WinitNamedKey::Delete => Some((NamedKey::Delete, Code::Delete)),
        WinitNamedKey::ArrowLeft => Some((NamedKey::ArrowLeft, Code::ArrowLeft)),
        WinitNamedKey::ArrowRight => Some((NamedKey::ArrowRight, Code::ArrowRight)),
        WinitNamedKey::ArrowUp => Some((NamedKey::ArrowUp, Code::ArrowUp)),
        WinitNamedKey::ArrowDown => Some((NamedKey::ArrowDown, Code::ArrowDown)),
        WinitNamedKey::Home => Some((NamedKey::Home, Code::Home)),
        WinitNamedKey::End => Some((NamedKey::End, Code::End)),
        WinitNamedKey::PageUp => Some((NamedKey::PageUp, Code::PageUp)),
        WinitNamedKey::PageDown => Some((NamedKey::PageDown, Code::PageDown)),
        _ => None,
    }
}

fn code_for_character(value: char) -> Code {
    match value.to_ascii_lowercase() {
        'a' => Code::KeyA,
        'b' => Code::KeyB,
        'c' => Code::KeyC,
        'd' => Code::KeyD,
        'e' => Code::KeyE,
        'f' => Code::KeyF,
        'g' => Code::KeyG,
        'h' => Code::KeyH,
        'i' => Code::KeyI,
        'j' => Code::KeyJ,
        'k' => Code::KeyK,
        'l' => Code::KeyL,
        'm' => Code::KeyM,
        'n' => Code::KeyN,
        'o' => Code::KeyO,
        'p' => Code::KeyP,
        'q' => Code::KeyQ,
        'r' => Code::KeyR,
        's' => Code::KeyS,
        't' => Code::KeyT,
        'u' => Code::KeyU,
        'v' => Code::KeyV,
        'w' => Code::KeyW,
        'x' => Code::KeyX,
        'y' => Code::KeyY,
        'z' => Code::KeyZ,
        '0' => Code::Digit0,
        '1' => Code::Digit1,
        '2' => Code::Digit2,
        '3' => Code::Digit3,
        '4' => Code::Digit4,
        '5' => Code::Digit5,
        '6' => Code::Digit6,
        '7' => Code::Digit7,
        '8' => Code::Digit8,
        '9' => Code::Digit9,
        ' ' => Code::Space,
        '-' | '_' => Code::Minus,
        '=' | '+' => Code::Equal,
        '[' | '{' => Code::BracketLeft,
        ']' | '}' => Code::BracketRight,
        '\\' | '|' => Code::Backslash,
        ';' | ':' => Code::Semicolon,
        '\'' | '"' => Code::Quote,
        ',' | '<' => Code::Comma,
        '.' | '>' => Code::Period,
        '/' | '?' => Code::Slash,
        '`' | '~' => Code::Backquote,
        _ => Code::Unidentified,
    }
}

#[derive(Clone)]
struct EventLoopWakerHandle(EventLoopProxy<CopalWakeEvent>);

#[derive(Debug)]
struct CopalWakeEvent;

impl EventLoopWakerHandle {
    fn new(event_loop: &EventLoop<CopalWakeEvent>) -> Self {
        Self(event_loop.create_proxy())
    }
}

impl EventLoopWaker for EventLoopWakerHandle {
    fn clone_box(&self) -> Box<dyn EventLoopWaker> {
        Box::new(Self(self.0.clone()))
    }

    fn wake(&self) {
        if let Err(error) = self.0.send_event(CopalWakeEvent) {
            eprintln!("servo_wake_error={error}");
        }
    }
}
