import AppKit
import Darwin

// A normal AppKit lifetime owns the canonical local server. Closing the browser
// leaves the app running; Quit asks its child owner to stop only its generation.
final class AppDelegate: NSObject, NSApplicationDelegate {
    var owner: Process?
    var quitting = false
    var browserURL: URL?
    var exitStatus: Int32 = 0

    func configuredPort(resources: URL, environment: [String: String]) throws -> Int {
        let helper = Process()
        helper.executableURL = resources.appendingPathComponent("runtime/openclank")
        helper.arguments = ["__mac-launch-configuration"]
        helper.currentDirectoryURL = resources.appendingPathComponent("runtime/_internal")
        helper.environment = environment
        let output = Pipe()
        helper.standardOutput = output
        helper.standardError = FileHandle.nullDevice
        let finished = DispatchSemaphore(value: 0)
        helper.terminationHandler = { _ in finished.signal() }
        try helper.run()
        if finished.wait(timeout: .now() + 30) == .timedOut {
            helper.terminate()
            if finished.wait(timeout: .now() + 5) == .timedOut {
                kill(helper.processIdentifier, SIGKILL)
                helper.waitUntilExit()
            }
            throw NSError(domain: "OpenClankLaunch", code: 1)
        }
        let data = output.fileHandleForReading.readData(ofLength: 64)
        guard helper.terminationStatus == 0, data.count <= 6,
              let rendered = String(data: data, encoding: .utf8),
              rendered.dropLast().utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
              let port = Int(rendered.dropLast()), (1...65535).contains(port),
              rendered == "\(port)\n" else {
            throw NSError(domain: "OpenClankLaunch", code: 2)
        }
        return port
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let resources = Bundle.main.resourceURL else { fail("Application resources are missing."); return }
        var environment = ProcessInfo.processInfo.environment
        for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPEN_CLANK_PYTHON", "OPEN_CLANK_RUNTIME_PYTHON"] {
            environment.removeValue(forKey: name)
        }
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        let port: Int
        do { port = try configuredPort(resources: resources, environment: environment) }
        catch {
            fail("Open Clank launch configuration is invalid or could not be read. Check the per-user Mac launch profile and APP_PORT.")
            return
        }
        browserURL = URL(string: "http://127.0.0.1:\(port)")
        let menu = NSMenu()
        let item = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Open Browser", action: #selector(openBrowser), keyEquivalent: "o")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Open Clank", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        menu.addItem(item)
        NSApp.mainMenu = menu
        let process = Process()
        process.executableURL = resources.appendingPathComponent("runtime/openclank")
        process.arguments = ["__mac-app-owner"]
        process.currentDirectoryURL = resources.appendingPathComponent("runtime/_internal")
        process.environment = environment
        let home = environment["HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.homeDirectoryForCurrentUser
        let support = home
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
        } catch { fail("Open Clank could not start. Check the per-user runtime log.") }
    }

    @objc func openBrowser() {
        if let url = browserURL { NSWorkspace.shared.open(url) }
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
        exitStatus = 1
        let alert = NSAlert()
        alert.messageText = "Open Clank"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        // NSApplication.terminate otherwise exits successfully even on fail().
        // Owned child cleanup has completed in applicationShouldTerminate.
        exit(exitStatus)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
