import Foundation

/// Version und Build zum Anzeigen, z.B. "0.2.0 · Build 412 · 3f9c2ab · 24.11. 10:05".
/// Build-Nummer, Git-Hash ("+" = mit uncommitteten Änderungen gebaut) und Zeit setzt build.command.
enum BuildInfo {
    static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }
    static var details: String {
        let info = Bundle.main.object(forInfoDictionaryKey: "CMEBuildInfo") as? String ?? ""
        if !info.isEmpty && info != "Xcode" { return info }
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"
        return "Build \(build) (Xcode)"
    }
    static var text: String { "\(version) · \(details)" }
}
