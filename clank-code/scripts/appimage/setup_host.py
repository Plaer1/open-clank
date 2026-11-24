import json
import subprocess
import sys
import threading

import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, WebKit2
from install import BASE, IMAGE, integrate, prepare_runtime

state = dict(platform="linux", phase="welcome", mode="integrated", destination=str(IMAGE))
GLib.set_prgname("open-clank-appimage")
window = Gtk.Window(title="Open Clank Setup")
# A custom GTK titlebar negotiates client-side decoration on Wayland.
# The WebView paints the applet strip and supplies its controls.
titlebar = Gtk.Box()
titlebar.set_no_show_all(True)
window.set_titlebar(titlebar)
titlebar.hide()
window.set_decorated(False)
window.set_default_size(760, 460)
window.set_size_request(390, 280)
eye = GdkPixbuf.Pixbuf.new_from_file(str(BASE / "open-clank.png"))
window.set_icon_list([eye.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
                      for size in (24, 48, 96, 128)])
manager = WebKit2.UserContentManager()
view = WebKit2.WebView.new_with_user_content_manager(manager)
view.get_settings().set_allow_file_access_from_file_urls(True)
page = (BASE / "ui/static/brand/open-clank/installer/index.html").as_uri()


def show(phase, **extra):
    state.update(phase=phase, **extra)
    view.evaluate_javascript("setup.render(" + json.dumps(state) + ")", -1, None, None, None, None, None)


def work(launch):
    show("working", message="Opening Open Clank…" if launch else "Installing application files…", canCancel=False)
    def run():
        try:
            prepare_runtime()
            if launch:
                result = subprocess.run([sys.executable, str(BASE / "install.py"), "server", "start", "--open-browser"],
                                        capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip()[-1400:])
                GLib.idle_add(Gtk.main_quit)
            else:
                integrate()
                GLib.idle_add(lambda: show("complete", message="Find Open Clank (AppImage) under Education."))
        except Exception as err:
            details = str(err)
            GLib.idle_add(lambda: show("error", message="Setup could not finish.", details=details, canRetry=True))
    threading.Thread(target=run, daemon=True).start()


def intent(manager, event):
    if view.get_uri() != page:
        return
    value = json.loads(event.get_js_value().to_string())
    action, phase = value["type"], state["phase"]
    if action == "minimize":
        window.iconify()
    elif action == "notice-link" and phase == "credits":
        url = value.get("url", "")
        if url.startswith(("https://", "http://")):
            Gtk.show_uri_on_window(window, url, Gdk.CURRENT_TIME)
    elif action == "maximize":
        toggle_maximize()
    elif action == "close" and phase != "working":
        Gtk.main_quit()
    elif action == "mode" and phase == "welcome":
        show("welcome", mode=value["id"])
    elif (action, phase) in (("terms", "welcome"), ("credits", "terms")):
        show(action)
    elif action == "review" and phase == "credits":
        show("review", existing=IMAGE.exists())
    elif action == "back" and phase in ("terms", "credits", "review"):
        show({"terms": "welcome", "credits": "terms", "review": "credits"}[phase])
    elif action == "retry" and phase == "error":
        show("review", existing=IMAGE.exists())
    elif (action, phase) in (("launch", "complete"), ("portable", "review"), ("install", "review")):
        work(action != "install")


def loaded(view, event):
    if event == WebKit2.LoadEvent.FINISHED:
        if sys.argv[1:] == ["--launch"]:
            work(True)
        else:
            show("welcome")


manager.register_script_message_handler("setup")
manager.connect("script-message-received::setup", intent)
view.connect("load-changed", loaded)
window.connect("delete-event", lambda *_: state["phase"] == "working")
window.connect("destroy", lambda *_: Gtk.main_quit())
def toggle_maximize():
    if window.is_maximized():
        window.unmaximize()
    else:
        window.maximize()


def title_press(_, event):
    if event.button != 1:
        return False
    if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
        toggle_maximize()
    else:
        window.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
    return True


def window_state(_, event):
    active = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
    if view.get_uri() == page:
        view.evaluate_javascript("window.setMaximized?.(" + json.dumps(active) + ")",
                                 -1, None, None, None, None, None)
    return False


# Forward the real pointer press and compositor serial when moving on Wayland.
# Overlay only the draggable title surface, leaving the HTML controls exposed.
overlay = Gtk.Overlay()
overlay.add(view)
drag = Gtk.EventBox()
drag.set_visible_window(False)
drag.set_above_child(True)
drag.set_valign(Gtk.Align.START)
drag.set_margin_end(104)
drag.set_size_request(-1, 39)
drag.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
drag.connect("button-press-event", title_press)
overlay.add_overlay(drag)
window.connect("window-state-event", window_state)
window.add(overlay)
view.load_uri(page)
window.show_all()
Gtk.main()
