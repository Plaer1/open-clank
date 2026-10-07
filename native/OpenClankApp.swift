import AppKit

// A normal AppKit lifetime owns the canonical local server. Closing the browser
// leaves the app running; Quit asks its child owner to stop only its generation.
final class AppDelegate: NSObject, NSApplicationDelegate {
    var owner: Process?
    var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Open Browser", action: #selector(openBrowser), keyEquivalent: "o")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Open Clank", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        menu.addItem(item)
        NSApp.mainMenu = menu
        guard let resources = Bundle.main.resourceURL else { fail("Application resources are missing."); return }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("runtime/openclank")
        process.arguments = ["__mac-app-owner"]
        process.currentDirectoryURL = resources.appendingPathComponent("runtime/_internal")
        var environment = ProcessInfo.processInfo.environment
        // A Finder launch and a Terminal launch use the same private closure.
        for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPEN_CLANK_PYTHON", "OPEN_CLANK_RUNTIME_PYTHON"] {
            environment.removeValue(forKey: name)
        }
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/OpenClank/runtime")
        do {
            try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
            let log = support.appendingPathComponent("macos-app.log")
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
            let handle = try FileHandle(forWritingTo: log)
            try handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
            process.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    guard let self = self, !self.quitting else { return }
                    if child.terminationStatus != 0 {
                        self.fail("Open Clank could not start or its server stopped. See ~/Library/Application Support/OpenClank/runtime/macos-app.log and server.log.")
                    } else { NSApp.terminate(nil) }
                }
            }
            try process.run()
            owner = process
        } catch { fail("Open Clank could not start: \(error.localizedDescription)") }
    }

    @objc func openBrowser() {
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:7777")!)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openBrowser()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitting = true
        guard let child = owner, child.isRunning else { return .terminateNow }
        child.terminate()
        DispatchQueue.global().async {
            child.waitUntilExit()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Open Clank"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
