import AppKit
import Foundation

struct Request: Codable { let op: String; let path: String; let app_id: String? }
struct App: Codable { let id: String; let name: String; let bundle_id: String; let path: String; let is_default: Bool }
struct Discovery: Codable { let applications: [App] }

func output<T: Encodable>(_ value: T) {
  let data = try! JSONEncoder().encode(value)
  print(String(data: data, encoding: .utf8)!)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
let request = try! JSONDecoder().decode(Request.self, from: input)
let fileURL = URL(fileURLWithPath: request.path)
let urls = NSWorkspace.shared.urlsForApplications(toOpen: fileURL)
let defaultURL = NSWorkspace.shared.urlForApplication(toOpen: fileURL)?.standardizedFileURL
let apps: [App] = urls.compactMap { (url: URL) -> App? in
  guard let bundle = Bundle(url: url), let id = bundle.bundleIdentifier else { return nil }
  let name = (bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String)
    ?? (bundle.object(forInfoDictionaryKey: "CFBundleName") as? String)
    ?? url.deletingPathExtension().lastPathComponent
  return App(id: id, name: name, bundle_id: id, path: url.path, is_default: url.standardizedFileURL == defaultURL)
}.reduce(into: [App]()) { (result: inout [App], app: App) in
  if !result.contains(where: { $0.bundle_id == app.bundle_id }) { result.append(app) }
}

if request.op == "discover" {
  output(Discovery(applications: apps))
} else if request.op == "resolve", let wanted = request.app_id,
          let app = apps.first(where: { $0.id == wanted }) {
  output(app)
} else {
  output(["error": "The selected application is unavailable", "code": "application_unavailable"])
}
