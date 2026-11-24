"""macOS framework primitives for desktop control (source-only, not yet routed).

The caller owns approvals, target/observation leases and event sequencing. These
functions never prompt or post input implicitly. They cannot verify that an event
changed the intended application; callers must report delivery as unverified.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import c_bool, c_double, c_int, c_long, c_uint, c_uint32, c_uint64, c_void_p


class Point(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class Size(ctypes.Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class Rect(ctypes.Structure):
    _fields_ = [("origin", Point), ("size", Size)]


def _frameworks():
    if sys.platform != "darwin":
        raise RuntimeError("macOS frameworks unavailable")
    q = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    q.AXIsProcessTrusted.restype = c_bool
    q.CGPreflightScreenCaptureAccess.restype = c_bool
    q.CGRequestScreenCaptureAccess.restype = c_bool
    q.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, Point, c_uint32]
    q.CGEventCreateMouseEvent.restype = c_void_p
    q.CGEventCreateKeyboardEvent.argtypes = [c_void_p, ctypes.c_ushort, c_bool]
    q.CGEventCreateKeyboardEvent.restype = c_void_p
    q.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ushort)]
    q.CGEventCreateScrollWheelEvent.argtypes = [c_void_p, c_uint32, c_uint32]
    q.CGEventCreateScrollWheelEvent.restype = c_void_p
    q.CGEventPost.argtypes = [c_uint32, c_void_p]
    q.CGMainDisplayID.restype = c_uint32
    q.CGDisplayPixelsWide.argtypes = [c_uint32]; q.CGDisplayPixelsWide.restype = ctypes.c_size_t
    q.CGDisplayPixelsHigh.argtypes = [c_uint32]; q.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    q.CGDisplayBounds.argtypes = [c_uint32]; q.CGDisplayBounds.restype = Rect
    q.CGWindowListCreateImage.argtypes = [Rect, c_uint32, c_uint32, c_uint32]
    q.CGWindowListCreateImage.restype = c_void_p
    q.CGImageGetWidth.argtypes = [c_void_p]; q.CGImageGetWidth.restype = ctypes.c_size_t
    q.CGImageGetHeight.argtypes = [c_void_p]; q.CGImageGetHeight.restype = ctypes.c_size_t
    q.CGImageGetBytesPerRow.argtypes = [c_void_p]; q.CGImageGetBytesPerRow.restype = ctypes.c_size_t
    q.CGImageGetDataProvider.argtypes = [c_void_p]; q.CGImageGetDataProvider.restype = c_void_p
    q.CGDataProviderCopyData.argtypes = [c_void_p]; q.CGDataProviderCopyData.restype = c_void_p
    cf.CFDataGetLength.argtypes = [c_void_p]; cf.CFDataGetLength.restype = c_long
    cf.CFDataGetBytePtr.argtypes = [c_void_p]; cf.CFDataGetBytePtr.restype = c_void_p
    cf.CFRelease.argtypes = [c_void_p]
    return q, cf


def permission_status() -> dict:
    """Read permission status without opening a system prompt."""
    try:
        q, _ = _frameworks()
        return {"accessibility": bool(q.AXIsProcessTrusted()), "screen_capture": bool(q.CGPreflightScreenCaptureAccess())}
    except (OSError, AttributeError, RuntimeError):
        return {"accessibility": False, "screen_capture": False}


def request_screen_capture_permission() -> bool:
    """Explicit user-initiated permission request; never called by a probe."""
    q, _ = _frameworks()
    return bool(q.CGRequestScreenCaptureAccess())


def primary_bounds() -> tuple[float, float, float, float]:
    q, _ = _frameworks()
    r = q.CGDisplayBounds(q.CGMainDisplayID())
    return r.origin.x, r.origin.y, r.size.width, r.size.height


def primary_display_id() -> int:
    return int(_frameworks()[0].CGMainDisplayID())


def primary_geometry() -> tuple[float, float, float, float, int, int]:
    q, _ = _frameworks()
    display = q.CGMainDisplayID()
    r = q.CGDisplayBounds(display)
    return (r.origin.x, r.origin.y, r.size.width, r.size.height,
            int(q.CGDisplayPixelsWide(display)), int(q.CGDisplayPixelsHigh(display)))


def capture_primary_raw() -> dict:
    """Copy main-display image bytes with native row stride; no encoded format is implied."""
    q, cf = _frameworks()
    if not q.CGPreflightScreenCaptureAccess():
        raise PermissionError("screen capture permission required")
    rect = q.CGDisplayBounds(q.CGMainDisplayID())
    image = q.CGWindowListCreateImage(rect, 1, 0, 0)
    if not image:
        raise RuntimeError("screen capture unavailable")
    data = None
    try:
        width, height = int(q.CGImageGetWidth(image)), int(q.CGImageGetHeight(image))
        stride = int(q.CGImageGetBytesPerRow(image))
        data = q.CGDataProviderCopyData(q.CGImageGetDataProvider(image))
        if not data:
            raise RuntimeError("screen capture data unavailable")
        length = int(cf.CFDataGetLength(data)); ptr = cf.CFDataGetBytePtr(data)
        if width <= 0 or height <= 0 or stride <= 0 or length < stride * height or length > 512 * 1024 * 1024 or not ptr:
            raise RuntimeError("invalid screen capture data")
        return {"width": width, "height": height, "bytes_per_row": stride, "data": ctypes.string_at(ptr, length)}
    finally:
        if data: cf.CFRelease(data)
        cf.CFRelease(image)


def post_mouse(event_type: int, x: float, y: float, button: int) -> None:
    """Post a permitted CG mouse event; caller must manage held-button cleanup."""
    if event_type not in (1, 2, 3, 4, 5, 6, 7, 25, 26, 27) or button not in (0, 1, 2):
        raise ValueError("unsupported mouse event")
    q, cf = _frameworks()
    if not q.AXIsProcessTrusted(): raise PermissionError("accessibility permission required")
    event = q.CGEventCreateMouseEvent(None, event_type, Point(x, y), button)
    if not event: raise RuntimeError("mouse event unavailable")
    try: q.CGEventPost(0, event)
    finally: cf.CFRelease(event)


def post_key(keycode: int, down: bool) -> None:
    """Post a hardware key event; caller must release every held key."""
    if type(keycode) is not int or not 0 <= keycode <= 127 or type(down) is not bool:
        raise ValueError("invalid key event")
    q, cf = _frameworks()
    if not q.AXIsProcessTrusted(): raise PermissionError("accessibility permission required")
    event = q.CGEventCreateKeyboardEvent(None, keycode, down)
    if not event: raise RuntimeError("key event unavailable")
    try: q.CGEventPost(0, event)
    finally: cf.CFRelease(event)



def post_unicode(char: str, down: bool) -> None:
    if not isinstance(char, str) or len(char) != 1 or type(down) is not bool:
        raise ValueError("invalid Unicode event")
    q, cf = _frameworks()
    if not q.AXIsProcessTrusted(): raise PermissionError("accessibility permission required")
    data = char.encode('utf-16-le'); units = (ctypes.c_ushort * (len(data) // 2)).from_buffer_copy(data)
    event = q.CGEventCreateKeyboardEvent(None, 0, down)
    if not event: raise RuntimeError("Unicode event unavailable")
    try:
        q.CGEventKeyboardSetUnicodeString(event, len(units), units)
        q.CGEventPost(0, event)
    finally: cf.CFRelease(event)


def post_scroll(vertical: int, horizontal: int) -> None:
    if type(vertical) is not int or type(horizontal) is not int or max(abs(vertical), abs(horizontal)) > 20:
        raise ValueError("invalid scroll event")
    q, cf = _frameworks()
    if not q.AXIsProcessTrusted(): raise PermissionError("accessibility permission required")
    event = q.CGEventCreateScrollWheelEvent(None, 1, 2, c_int(vertical), c_int(horizontal))
    if not event: raise RuntimeError("scroll event unavailable")
    try: q.CGEventPost(0, event)
    finally: cf.CFRelease(event)


# AX objects are owned CoreFoundation references. All returned references must
# be released by the helper when their snapshot expires.
class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [(name, c_uint32) for name in ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32),
        ('nfiles', c_uint32), ('pgid', c_uint32), ('pjobc', c_uint32), ('tdev', c_uint32), ('tpgid', c_uint32), ('nice', c_int),
        ('start_sec', c_uint64), ('start_usec', c_uint64)]


def process_info(pid: int):
    if sys.platform != 'darwin' or type(pid) is not int or pid <= 1:
        return None
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    lib.proc_pidinfo.argtypes = [c_int, c_int, c_uint64, c_void_p, c_int]
    lib.proc_pidinfo.restype = c_int
    info = _ProcBSDInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    lib.proc_pidpath.argtypes = [c_int, c_void_p, c_uint32]; lib.proc_pidpath.restype = c_int
    path = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, path, len(path)) <= 0: return None
    name = bytes(info.name or info.comm).split(b'\0', 1)[0].decode('utf-8', 'replace')
    return {'pid': pid, 'name': name, 'exe': path.value.decode('utf-8', 'replace'),
            'uid': int(info.uid), 'start': int(info.start_sec) * 1_000_000 + int(info.start_usec)}


def processes():
    if sys.platform != 'darwin': raise RuntimeError('macOS unavailable')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    lib.proc_listallpids.argtypes = [c_void_p, c_int]; lib.proc_listallpids.restype = c_int
    ids = (c_int * 4096)()
    count = lib.proc_listallpids(ids, ctypes.sizeof(ids))
    if count < 0 or count >= len(ids): raise RuntimeError('process list unavailable')
    import os
    return [info for pid in ids[:count] if (info := process_info(pid)) and info['uid'] == os.getuid()]


def _ax():
    q, cf = _frameworks()
    q.AXUIElementCreateApplication.argtypes = [c_int]; q.AXUIElementCreateApplication.restype = c_void_p
    q.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, ctypes.POINTER(c_void_p)]; q.AXUIElementCopyAttributeValue.restype = c_int
    q.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]; q.AXUIElementSetAttributeValue.restype = c_int
    q.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]; q.AXUIElementPerformAction.restype = c_int
    q.AXUIElementCopyActionNames.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]; q.AXUIElementCopyActionNames.restype = c_int
    q.AXValueCreate.argtypes = [c_int, c_void_p]; q.AXValueCreate.restype = c_void_p
    q.AXUIElementSetMessagingTimeout.argtypes = [c_void_p, ctypes.c_float]; q.AXUIElementSetMessagingTimeout.restype = c_int
    cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]; cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, c_long, c_uint32]; cf.CFStringGetCString.restype = c_bool
    cf.CFGetTypeID.argtypes = [c_void_p]; cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    cf.CFArrayGetCount.argtypes = [c_void_p]; cf.CFArrayGetCount.restype = c_long
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]; cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFRetain.argtypes = [c_void_p]; cf.CFRetain.restype = c_void_p
    cf.CFEqual.argtypes = [c_void_p, c_void_p]; cf.CFEqual.restype = c_bool
    return q, cf


def _cf_string(cf, text):
    value = cf.CFStringCreateWithCString(None, text.encode('utf-8'), 0x08000100)
    if not value: raise RuntimeError('CFString unavailable')
    return value


def ax_copy(element, attribute):
    q, cf = _ax(); key = _cf_string(cf, attribute); value = c_void_p()
    try:
        if q.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value)) != 0:
            return None
        return value.value
    finally:
        cf.CFRelease(key)


def ax_text(element, attribute):
    _, cf = _ax(); value = ax_copy(element, attribute)
    if not value: return ''
    try:
        if cf.CFGetTypeID(value) != cf.CFStringGetTypeID(): return ''
        buf = ctypes.create_string_buffer(4096)
        return buf.value.decode('utf-8', 'replace')[:500] if cf.CFStringGetCString(value, buf, len(buf), 0x08000100) else ''
    finally:
        cf.CFRelease(value)


def ax_array(element, attribute, limit=1200):
    _, cf = _ax(); value = ax_copy(element, attribute)
    if not value: return []
    try:
        if cf.CFGetTypeID(value) != cf.CFArrayGetTypeID(): return []
        count = max(0, min(int(cf.CFArrayGetCount(value)), limit))
        return [cf.CFRetain(cf.CFArrayGetValueAtIndex(value, i)) for i in range(count)]
    finally:
        cf.CFRelease(value)


def ax_application(pid):
    q, _ = _ax(); app = q.AXUIElementCreateApplication(pid)
    if not app: raise RuntimeError('AX application unavailable')
    q.AXUIElementSetMessagingTimeout(app, ctypes.c_float(2.0))
    return app


def ax_retain(obj):
    return _ax()[1].CFRetain(obj)


def ax_equal(left, right):
    return bool(_ax()[1].CFEqual(left, right))


def ax_release(obj):
    if obj: _ax()[1].CFRelease(obj)


def ax_perform(element, action):
    q, cf = _ax(); name = _cf_string(cf, action)
    try: return q.AXUIElementPerformAction(element, name) == 0
    finally: cf.CFRelease(name)


def ax_set_text(element, text, attribute='AXValue'):
    q, cf = _ax(); key = _cf_string(cf, attribute); value = _cf_string(cf, text)
    try: return q.AXUIElementSetAttributeValue(element, key, value) == 0
    finally: cf.CFRelease(value); cf.CFRelease(key)


def ax_action_names(element):
    q, cf = _ax(); value = c_void_p()
    if q.AXUIElementCopyActionNames(element, ctypes.byref(value)) != 0 or not value.value: return []
    try:
        if cf.CFGetTypeID(value) != cf.CFArrayGetTypeID(): return []
        names = []
        for i in range(min(128, max(0, int(cf.CFArrayGetCount(value))))):
            item = cf.CFArrayGetValueAtIndex(value, i)
            if item and cf.CFGetTypeID(item) == cf.CFStringGetTypeID():
                buf = ctypes.create_string_buffer(512)
                if cf.CFStringGetCString(item, buf, len(buf), 0x08000100): names.append(buf.value.decode('utf-8', 'replace'))
        return names
    finally: cf.CFRelease(value)


def ax_select_text(element, start: int, end: int):
    value = ax_text(element, 'AXValue')
    if not 0 <= start <= end <= len(value): return False
    # AX ranges are UTF-16 code units; the shared protocol counts characters.
    location = len(value[:start].encode('utf-16-le')) // 2
    length = len(value[start:end].encode('utf-16-le')) // 2
    class CFRange(ctypes.Structure):
        _fields_ = [('location', c_long), ('length', c_long)]
    q, cf = _ax(); key = _cf_string(cf, 'AXSelectedTextRange'); span = CFRange(location, length)
    ref = q.AXValueCreate(4, ctypes.byref(span))
    if not ref: cf.CFRelease(key); return False
    try: return q.AXUIElementSetAttributeValue(element, key, ref) == 0
    finally: cf.CFRelease(ref); cf.CFRelease(key)



def _process_command(pid):
    """Read KERN_PROCARGS2 and return executable plus exact argv, or fail closed."""
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(c_int), c_uint, c_void_p, ctypes.POINTER(ctypes.c_size_t), c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = c_int
    mib = (c_int * 3)(1, 49, pid); length = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(length), None, 0) or not 4 < length.value <= 1024 * 1024:
        return None
    data = ctypes.create_string_buffer(length.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(length), None, 0): return None
    raw = data.raw[:length.value]; argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    if not 1 <= argc <= 64: return None
    end = raw.find(b'\0', 4)
    if end < 0: return None
    executable = raw[4:end].decode('utf-8', 'strict'); offset = end + 1
    while offset < len(raw) and raw[offset] == 0: offset += 1
    argv = []
    for _ in range(argc):
        end = raw.find(b'\0', offset)
        if end < 0: return None
        argv.append(raw[offset:end].decode('utf-8', 'strict')); offset = end + 1
    return executable, argv


def open_verified_helper(pid: int, kind: str):
    """Acquire an identity-bound Mach task port only for our exact helper."""
    import os
    if sys.platform != 'darwin' or kind not in ('control', 'a11y'): return None
    try:
        before = process_info(pid); command = _process_command(pid)
        if not before or before['uid'] != os.getuid() or not command: return None
        exe, args = command
        if (os.path.realpath(exe) != os.path.realpath(sys.executable) or len(args) != 4 or
            os.path.realpath(args[0]) != os.path.realpath(sys.executable) or
            args[1:] != ['-m', 'src.openclank.computer_control.macos_helper', kind]): return None
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        current = c_uint32.in_dll(libc, 'mach_task_self_').value
        libc.task_for_pid.argtypes = [c_uint32, c_int, ctypes.POINTER(c_uint32)]; libc.task_for_pid.restype = c_int
        libc.task_terminate.argtypes = [c_uint32]; libc.task_terminate.restype = c_int
        libc.mach_port_deallocate.argtypes = [c_uint32, c_uint32]; libc.mach_port_deallocate.restype = c_int
        port = c_uint32()
        if libc.task_for_pid(current, pid, ctypes.byref(port)) != 0 or not port.value: return None
        if process_info(pid) != before:
            libc.mach_port_deallocate(current, port.value); return None
        return {'platform': 'macos', 'pid': pid, 'start': before['start'], 'kind': kind,
                'port': port.value, 'mach': libc, 'current_task': current}
    except (OSError, AttributeError, ValueError, UnicodeError):
        return None


def helper_alive(entry):
    current = process_info(entry['pid'])
    return current is not None and current['start'] == entry['start']


def terminate_helper(entry):
    """A Mach task port identifies the original process; input release is unknown."""
    try: return bool(helper_alive(entry) and entry['mach'].task_terminate(entry['port']) == 0)
    except (OSError, KeyError, AttributeError): return False


def close_helper(entry):
    try: entry['mach'].mach_port_deallocate(entry['current_task'], entry['port'])
    except (OSError, KeyError, AttributeError): pass


class LaunchSocket:
    """Private, time-limited Unix socket with kernel-provided client PID."""
    def __init__(self, secret, kind, timeout=15.0):
        import os, socket, tempfile, threading, time
        self.secret, self.kind = secret, kind
        self.deadline = time.monotonic() + timeout
        self.done, self.cancelled, self.lock = threading.Event(), threading.Event(), threading.Lock()
        self.entry = None
        self.directory = tempfile.mkdtemp(prefix='oc-', dir='/tmp')
        os.chmod(self.directory, 0o700)
        self.name = os.path.join(self.directory, 's')
        try:
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(self.name); os.chmod(self.name, 0o600)
            self.server.listen(1); self.server.settimeout(0.1)
        except BaseException:
            try: self.server.close()
            except Exception: pass
            try: os.rmdir(self.directory)
            except OSError: pass
            raise
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def _run(self):
        import hmac, os, socket, struct, time
        conn = None
        try:
            while not self.cancelled.is_set() and time.monotonic() < self.deadline:
                try: conn, _ = self.server.accept(); break
                except socket.timeout: continue
            if conn is None: return
            conn.settimeout(0.1)
            pid = struct.unpack('i', conn.getsockopt(0, 2, ctypes.sizeof(c_int)))[0]  # SOL_LOCAL / LOCAL_PEERPID
            libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            uid, gid = c_uint(), c_uint()
            libc.getpeereid.argtypes = [c_int, ctypes.POINTER(c_uint), ctypes.POINTER(c_uint)]
            if libc.getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0 or uid.value != os.getuid(): return
            received = bytearray()
            while not self.cancelled.is_set() and time.monotonic() < self.deadline and len(received) < 128:
                try:
                    chunk = conn.recv(128 - len(received))
                    if not chunk: return
                    received.extend(chunk)
                    if b'\n' in received: break
                except socket.timeout: continue
            if not hmac.compare_digest(bytes(received), (self.secret + '\n').encode('ascii')): return
            entry = open_verified_helper(pid, self.kind)
            if entry is None: return
            with self.lock:
                if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                    close_helper(entry)
                else:
                    self.entry = entry
                    try: conn.sendall(b'1')
                    except OSError: close_helper(entry); self.entry = None
        except (OSError, ValueError, AttributeError):
            return
        finally:
            if conn: conn.close()
            self.server.close()
            try: os.unlink(self.name)
            except OSError: pass
            try: os.rmdir(self.directory)
            except OSError: pass
            self.done.set()
            self.cancelled.wait(max(0.0, self.deadline - time.monotonic()))
            self.cancel()

    def take(self):
        import time
        self.done.wait(max(0.0, self.deadline - time.monotonic()))
        with self.lock:
            if not self.done.is_set() or self.cancelled.is_set() or time.monotonic() >= self.deadline: return None
            entry, self.entry = self.entry, None
            return entry

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.entry is not None: close_helper(self.entry); self.entry = None


def helper_argv(mode: str):
    if sys.platform != 'darwin' or mode not in ('control', 'a11y'):
        return None
    return [sys.executable, '-m', 'src.openclank.computer_control.macos_helper', mode]


def replace_clipboard_text(text: str) -> None:
    """Eager AppKit pasteboard replacement; caller has explicit action approval."""
    if sys.platform != "darwin": raise RuntimeError("macOS frameworks unavailable")
    appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]; objc.objc_getClass.restype = c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]; objc.sel_registerName.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
    cf.CFStringCreateWithCString.restype = c_void_p; cf.CFRelease.argtypes = [c_void_p]
    msg = ctypes.cast(objc.objc_msgSend, c_void_p).value
    if not msg: raise RuntimeError("pasteboard unavailable")
    get = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(msg)
    clear = ctypes.CFUNCTYPE(c_long, c_void_p, c_void_p)(msg)
    write = ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p, c_void_p, c_void_p)(msg)
    board = get(objc.objc_getClass(b"NSPasteboard"), objc.sel_registerName(b"generalPasteboard"))
    if not board: raise RuntimeError("pasteboard unavailable")
    value = cf.CFStringCreateWithCString(None, text.encode("utf-8", "strict"), 0x08000100)
    if not value: raise RuntimeError("pasteboard text unavailable")
    try:
        kind = c_void_p.in_dll(appkit, "NSPasteboardTypeString").value
        if not kind: raise RuntimeError("pasteboard type unavailable")
        clear(board, objc.sel_registerName(b"clearContents"))
        if not write(board, objc.sel_registerName(b"setString:forType:"), value, kind):
            raise RuntimeError("pasteboard write unavailable")
    finally:
        cf.CFRelease(value)


def clipboard_available() -> bool:
    """Read-only framework availability; it does not access the pasteboard."""
    if sys.platform != "darwin": return False
    try:
        appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.argtypes = [ctypes.c_char_p]; objc.objc_getClass.restype = c_void_p
        return bool(objc.objc_getClass(b"NSPasteboard") and c_void_p.in_dll(appkit, "NSPasteboardTypeString").value)
    except (OSError, AttributeError, ValueError):
        return False
