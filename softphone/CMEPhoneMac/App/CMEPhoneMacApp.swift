import SwiftUI
import AppKit
import Combine

@main
struct CMEPhoneMacApp: App {
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var appDelegate
    @ObservedObject private var sip = SIPManager.shared
    @ObservedObject private var settings = PhoneSettings.shared
    @ObservedObject private var desk = DeskPhoneController.shared

    var body: some Scene {
        WindowGroup("CME Phone", id: "main") {
            MacMainView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(DeskPhoneController.shared)
                .environmentObject(CallCenter.shared)
                .frame(minWidth: 640, minHeight: 460)
                .onOpenURL { URLRouter.handle($0) }
        }
        .handlesExternalEvents(matching: ["*"])
        .commands {
            CommandGroup(after: .newItem) {
                Button("Tischtelefon: Annehmen") { DeskPhoneController.shared.answer() }
                    .keyboardShortcut("a", modifiers: [.command, .shift])
                Button("Tischtelefon: Auflegen") { DeskPhoneController.shared.hangup() }
                    .keyboardShortcut("e", modifiers: [.command, .shift])
            }
        }

        MenuBarExtra {
            MenuBarView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(DeskPhoneController.shared)
        } label: {
            Image(systemName: menuIcon)
        }
        .menuBarExtraStyle(.window)

        Settings {
            MacSettingsView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(DeskPhoneController.shared)
        }
    }

    private var menuIcon: String {
        if !sip.calls.isEmpty { return "phone.connection.fill" }
        if settings.macMode == .deskPhone {
            switch desk.status.state {
            case .ringing: return "phone.badge.waveform.fill"
            case .busy, .dialing: return "phone.connection.fill"
            default: break
            }
        }
        if case .ok = sip.registration { return "phone.fill" }
        return "phone.badge.exclamationmark"
    }
}

/// tel:/sip:/callto:-Links (z.B. aus Kontakte, Safari, Mail) und cmephone://-Aufrufe.
enum URLRouter {
    @MainActor
    static func handle(_ url: URL) {
        guard let scheme = url.scheme?.lowercased() else { return }
        switch scheme {
        case "tel", "sip", "callto", "sips":
            var s = url.absoluteString
            if let r = s.range(of: ":") { s = String(s[r.upperBound...]) }
            s = s.replacingOccurrences(of: "//", with: "")
            s = s.removingPercentEncoding ?? s
            if let semi = s.firstIndex(of: ";") { s = String(s[..<semi]) }
            let number = scheme.hasPrefix("sip") ? s : s.filter { "0123456789*#+".contains($0) }
            guard !number.isEmpty else { return }
            dial(number)
        case "cmephone":
            // cmephone://call  -> nur Fenster nach vorne (wird von CallCenter genutzt)
            // cmephone://dial/0711123
            if url.host == "dial" { dial(url.lastPathComponent) }
        default: break
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    @MainActor
    static func dial(_ number: String, video: Bool = false) {
        switch PhoneSettings.shared.macMode {
        case .softphone: CallCenter.shared.startOutgoing(number: number, video: video)
        case .deskPhone: DeskPhoneController.shared.dial(number)
        }
    }
}

final class MacAppDelegate: NSObject, NSApplicationDelegate {
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated {
            CallCenter.shared.setup()
            SIPManager.shared.start()

            // Statusabo für das Tischtelefon starten, sobald der Mac angemeldet ist.
            SIPManager.shared.$registration
                .removeDuplicates()
                .sink { state in
                    MainActor.assumeIsolated {
                        if case .ok = state, PhoneSettings.shared.macMode == .deskPhone {
                            DeskPhoneController.shared.startMonitoring()
                        }
                    }
                }
                .store(in: &cancellables)

            // Anruffenster nach vorne holen (öffnet notfalls ein neues Hauptfenster).
            CallCenter.shared.$showCallWindow
                .removeDuplicates()
                .filter { $0 }
                .sink { _ in
                    if let url = URL(string: "cmephone://call") { NSWorkspace.shared.open(url) }
                }
                .store(in: &cancellables)
        }
    }

    /// App läuft in der Menüleiste weiter, auch wenn das Fenster zu ist.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            DeskPhoneController.shared.stopMonitoring()
            SIPManager.shared.core?.stop()
        }
    }
}
