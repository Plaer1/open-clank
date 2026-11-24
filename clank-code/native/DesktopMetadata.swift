import Foundation
import CoreGraphics

struct Display: Codable { let id: UInt32; let index: Int; let bounds: [Double]; let scale: Double; let visible: Bool }
struct Window: Codable { let id: UInt32; let bounds: [Double]; let displayID: UInt32; let displayIndex: Int; let scale: Double; let visible: Bool }
struct Result: Codable { let displays: [Display]; let windows: [Window] }

func rect(_ value: CGRect) -> [Double] { [value.origin.x, value.origin.y, value.width, value.height] }
var displayIDs = [CGDirectDisplayID](repeating: 0, count: 32)
var displayCount: UInt32 = 0
CGGetActiveDisplayList(32, &displayIDs, &displayCount)
let activeDisplayIDs = displayIDs.prefix(Int(displayCount)).filter { CGDisplayIsOnline($0) != 0 }
let displays = activeDisplayIDs.enumerated().map { offset, id -> Display in
    let bounds = CGDisplayBounds(id)
    let scale = bounds.width > 0 ? Double(CGDisplayPixelsWide(id)) / bounds.width : 1
    return Display(id: id, index: offset + 1, bounds: rect(bounds), scale: scale, visible: CGDisplayIsOnline(id) != 0)
}
let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
let windows = raw.compactMap { item -> Window? in
    guard let number = item[kCGWindowNumber as String] as? NSNumber,
          let frame = item[kCGWindowBounds as String] as? [String: Any],
          let x = frame["X"] as? NSNumber, let y = frame["Y"] as? NSNumber,
          let w = frame["Width"] as? NSNumber, let h = frame["Height"] as? NSNumber else { return nil }
    let windowRect = CGRect(x: x.doubleValue, y: y.doubleValue, width: w.doubleValue, height: h.doubleValue)
    guard !displays.isEmpty else { return nil }
    let ranked = displays.enumerated().map { offset, display -> (Double, Int, Display) in
        let d = CGRect(x: display.bounds[0], y: display.bounds[1], width: display.bounds[2], height: display.bounds[3])
        let overlap = windowRect.intersection(d)
        return (max(0, overlap.width) * max(0, overlap.height), offset, display)
    }.sorted { $0.0 != $1.0 ? $0.0 > $1.0 : ($0.2.index < $1.2.index) }
    guard let selected = ranked.first(where: { $0.0 > 0 })?.2 else { return nil }
    return Window(id: number.uint32Value, bounds: [x.doubleValue, y.doubleValue, w.doubleValue, h.doubleValue], displayID: selected.id, displayIndex: selected.index, scale: selected.scale, visible: true)
}
do { FileHandle.standardOutput.write(try JSONEncoder().encode(Result(displays: displays, windows: windows))) }
catch { exit(1) }
