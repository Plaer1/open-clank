"""Bounded Windows desktop helper; never substitute another capture target.

Only explicit tool consent may launch this helper. Native API calls execute in
an owned child so a blocked PrintWindow or OCR operation can be terminated.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import sys

MAX_PIXELS = 16 * 1024 * 1024


class NativeCaptureError(ValueError):
    def __init__(self, message: str, code: str = "target_unavailable"):
        super().__init__(message)
        self.code = code


def _apis():
    if sys.platform != "win32":
        raise NativeCaptureError("Windows capture is unavailable", "unavailable")
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    def bind(dll, name, result, args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, args
        return fn
    bind(user, "GetDC", w.HDC, [w.HWND])
    bind(user, "ReleaseDC", ctypes.c_int, [w.HWND, w.HDC])
    bind(user, "GetWindowRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)])
    bind(user, "IsWindowVisible", w.BOOL, [w.HWND])
    bind(user, "IsIconic", w.BOOL, [w.HWND])
    bind(user, "GetWindowThreadProcessId", w.DWORD, [w.HWND, ctypes.POINTER(w.DWORD)])
    bind(user, "GetWindowDisplayAffinity", w.BOOL, [w.HWND, ctypes.POINTER(w.DWORD)])
    bind(user, "PrintWindow", w.BOOL, [w.HWND, w.HDC, w.UINT])
    bind(user, "SetThreadDpiAwarenessContext", w.HANDLE, [w.HANDLE])
    bind(user, "GetMonitorInfoW", w.BOOL, [w.HANDLE, ctypes.c_void_p])
    bind(user, "OpenInputDesktop", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD])
    bind(user, "CloseDesktop", w.BOOL, [w.HANDLE])
    bind(user, "GetUserObjectInformationW", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)])
    bind(gdi, "CreateCompatibleDC", w.HDC, [w.HDC])
    bind(gdi, "CreateCompatibleBitmap", w.HBITMAP, [w.HDC, ctypes.c_int, ctypes.c_int])
    bind(gdi, "SelectObject", w.HANDLE, [w.HDC, w.HANDLE])
    bind(gdi, "DeleteObject", w.BOOL, [w.HANDLE])
    bind(gdi, "DeleteDC", w.BOOL, [w.HDC])
    bind(gdi, "BitBlt", w.BOOL, [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD])
    bind(gdi, "PatBlt", w.BOOL, [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD])
    bind(gdi, "GetDIBits", ctypes.c_int, [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT])
    return user, gdi


def _interactive(user):
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        raise NativeCaptureError("interactive desktop access denied", "permission_required")
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = w.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)) or name.value.casefold() != "default":
            raise NativeCaptureError("interactive desktop is unavailable", "permission_required")
    finally:
        user.CloseDesktop(desktop)


def _metadata(user, request):
    _interactive(user)
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD), ("device", w.WCHAR * 32)]
    monitors = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    def collect(handle, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.monitor
            monitors.append({"id": info.device, "bounds": [r.left, r.top, r.right-r.left, r.bottom-r.top], "primary": bool(info.flags & 1)})
        return True
    callback = callback_type(collect)
    user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.c_void_p, callback_type, w.LPARAM]
    user.EnumDisplayMonitors.restype = w.BOOL
    if not user.EnumDisplayMonitors(None, None, callback, 0):
        raise NativeCaptureError("display enumeration failed")
    monitors.sort(key=lambda item: (not item["primary"], item["id"]))
    for index, item in enumerate(monitors, 1):
        item["index"] = index
    target = request.get("target", "display")
    if target == "display":
        identity = request.get("display")
        if type(identity) is not int or not 1 <= identity <= 32:
            raise NativeCaptureError("display identity is required", "invalid_display")
        match = next((m for m in monitors if m["index"] == identity), None)
    elif target == "region":
        region = request.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4 or any(type(v) is not int for v in region) or min(region[2:]) <= 0:
            raise NativeCaptureError("capture region is invalid", "invalid_region")
        x, y, width, height = region
        match = next((m for m in monitors if x >= m["bounds"][0] and y >= m["bounds"][1] and x+width <= m["bounds"][0]+m["bounds"][2] and y+height <= m["bounds"][1]+m["bounds"][3]), None)
    elif target == "window":
        hwnd = request.get("window_id")
        if type(hwnd) is not int or hwnd <= 0 or hwnd >= 2 ** (ctypes.sizeof(ctypes.c_void_p)*8):
            raise NativeCaptureError("window identity is required", "invalid_window")
        rect, pid, affinity = w.RECT(), w.DWORD(), w.DWORD()
        if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd) or not user.GetWindowRect(hwnd, ctypes.byref(rect)) or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise NativeCaptureError("capture window is not visible")
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        cloaked = w.DWORD()
        if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0 or cloaked.value:
            raise NativeCaptureError("capture window is unavailable")
        if user.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)) and affinity.value:
            raise NativeCaptureError("capture window is protected", "permission_required")
        bounds = [rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top]
        if not any(bounds[0] < m["bounds"][0]+m["bounds"][2] and bounds[0]+bounds[2] > m["bounds"][0] and bounds[1] < m["bounds"][1]+m["bounds"][3] and bounds[1]+bounds[3] > m["bounds"][1] for m in monitors):
            raise NativeCaptureError("capture window is off-screen")
        match = {"id": hwnd, "processId": pid.value, "bounds": bounds}
    else:
        raise NativeCaptureError("capture target is unsupported", "unsupported_target")
    if not match or min(match["bounds"][2:]) <= 0:
        raise NativeCaptureError("capture target is unavailable")
    return {**match, "visible": True, "viewport": match["bounds"], "crop": request.get("region"), "scale": 1, "coordinateSpace": "physical_pixels"}


def metadata(request):
    user, _ = _apis()
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise NativeCaptureError("capture DPI context unavailable", "unavailable")
    try:
        return _metadata(user, request)
    finally:
        user.SetThreadDpiAwarenessContext(previous)


def capture(request, output, expected):
    from PIL import Image
    user, gdi = _apis()
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise NativeCaptureError("capture DPI context unavailable", "unavailable")
    screen = dc = bitmap = old = None
    try:
        target = _metadata(user, request)
        if target != expected:
            raise NativeCaptureError("capture target changed")
        x, y, width, height = request["region"] if request.get("target") == "region" else target["bounds"]
        if width * height > MAX_PIXELS:
            raise NativeCaptureError("capture pixel size exceeds limit", "size_limit")
        screen = user.GetDC(None)
        dc = gdi.CreateCompatibleDC(screen) if screen else None
        bitmap = gdi.CreateCompatibleBitmap(screen, width, height) if dc else None
        if not bitmap:
            raise NativeCaptureError("capture surface unavailable", "unavailable")
        old = gdi.SelectObject(dc, bitmap)
        if not old or old == ctypes.c_void_p(-1).value:
            raise NativeCaptureError("capture surface unavailable", "unavailable")
        if not gdi.PatBlt(dc, 0, 0, width, height, 0x42):
            raise NativeCaptureError("capture surface could not initialize", "unavailable")
        if request.get("target") == "window":
            success = user.PrintWindow(request["window_id"], dc, 2)
        else:
            success = gdi.BitBlt(dc, 0, 0, width, height, screen, x, y, 0x40CC0020)
        if not success:
            raise NativeCaptureError("capture target could not render", "permission_required")
        if _metadata(user, request) != target:
            raise NativeCaptureError("capture target changed during rendering")
        class BitmapHeader(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG), ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD), ("image_size", w.DWORD), ("x", w.LONG), ("y", w.LONG), ("used", w.DWORD), ("important", w.DWORD)]
        header = BitmapHeader(ctypes.sizeof(BitmapHeader), width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width*height*4)
        gdi.SelectObject(dc, old)
        old = None
        if gdi.GetDIBits(screen, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise NativeCaptureError("capture pixels unavailable", "unavailable")
        image = Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
        if request.get("target") == "window" and image.getbbox() is None:
            raise NativeCaptureError("window did not supply capture pixels", "target_unavailable")
        image.save(output, "PNG")
    finally:
        if old and dc:
            gdi.SelectObject(dc, old)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if dc:
            gdi.DeleteDC(dc)
        if screen:
            user.ReleaseDC(None, screen)
        user.SetThreadDpiAwarenessContext(previous)


async def recognize(path):
    from PIL import Image
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise NativeCaptureError("install a Windows OCR language for the user", "ocr_unavailable")
    boxes = []
    with Image.open(path) as source:
        width, height = source.size
        if width*height > MAX_PIXELS:
            raise NativeCaptureError("image exceeds Windows OCR pixel limit", "ocr_unavailable")
        limit = OcrEngine.max_image_dimension
        if limit <= 256:
            raise NativeCaptureError("Windows OCR dimensions unavailable", "ocr_unavailable")
        # Keep capture resolution and original coordinates. Overlapping tiles
        # allow native OCR to process displays larger than its per-image limit.
        step = limit - 256
        for top in range(0, height, step):
            for left in range(0, width, step):
                right, bottom = min(left+step, width), min(top+step, height)
                crop = (max(0, left-128), max(0, top-128), min(width, right+128), min(height, bottom+128))
                tile = source.crop(crop).convert("RGBA")
                writer = DataWriter()
                try:
                    writer.write_bytes(tile.tobytes())
                    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8, tile.width, tile.height, BitmapAlphaMode.STRAIGHT)
                finally:
                    writer.close()
                try:
                    result = await engine.recognize_async(bitmap)
                    for line in result.lines:
                        words = list(line.words)
                        if not words:
                            continue
                        rects = [word.bounding_rect for word in words]
                        x, y = min(r.x for r in rects), min(r.y for r in rects)
                        box_width = max(r.x+r.width for r in rects)-x
                        box_height = max(r.y+r.height for r in rects)-y
                        x, y = x+crop[0], y+crop[1]
                        if not (left <= x+box_width/2 < right and top <= y+box_height/2 < bottom):
                            continue
                        boxes.append({"text": line.text, "confidence": None, "candidates": [line.text], "x": x, "y": y, "width": box_width, "height": box_height})
                finally:
                    bitmap.close()
    boxes.sort(key=lambda box: (box["y"], box["x"]))
    for order, box in enumerate(boxes):
        box["order"] = order
    return {"supportedLanguages": [language.language_tag for language in OcrEngine.available_recognizer_languages], "width": width, "height": height, "boxes": boxes}


def main():
    try:
        if sys.platform != "win32":
            raise NativeCaptureError("Windows capture unavailable", "unavailable")
        action = sys.argv[1]
        if action == "capture":
            capture(json.loads(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4]))
            value = {"state": "captured"}
        elif action == "ocr":
            value = asyncio.run(recognize(Path(sys.argv[2])))
        else:
            raise NativeCaptureError("unknown desktop helper action", "invalid_request")
        print(json.dumps(value))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", "ocr_unavailable" if len(sys.argv) > 1 and sys.argv[1] == "ocr" else "unavailable")
        print(json.dumps({"error": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
