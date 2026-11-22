import SwiftUI
import AppKit
import Combine

/// Menüleisten-App (kein Dock-Symbol, kein Hauptfenster):
///   Softphone / Telefonsteuerung · Anrufliste · Wähltastatur · Einstellungen
/// Anrufe erscheinen als Call-Toast unten rechts.
@main
struct CMEPhoneMacApp: App {
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var appDelegate
    @ObservedObject private var sip = SIPManager.shared
    @ObservedObject private var settings = PhoneSettings.shared
    @ObservedObject private var desk = DeskPhoneController.shared

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(DeskPhoneController.shared)
                .environmentObject(CallHistory.shared)
        } label: {
            Image(systemName: menuIcon)
        }
        .menuBarExtraStyle(.menu)

        // Kein Settings-Scene: das eigene Fenster (SettingsWindow) lässt sich auch beim Start und
        // beim erneuten Öffnen der App aus dem Finder zeigen.
        Settings { EmptyView() }
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
        if case .ok = sip.registration { return settings.macMode == .deskPhone ? "phone.arrow.up.right.fill" : "phone.fill" }
        return "phone.badge.exclamationmark"
    }
}

/// tel:/sip:/callto:-Links (z.B. aus Kontakte, Safari, Mail) und cmephone://dial/<nummer>.
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
            if url.host == "dial" { DialpadPanel.shared.show(number: url.lastPathComponent) }
        default: break
        }
    }

    /// Wählt je nach Modus über den Mac oder am Tischtelefon.
    @MainActor
    static func dial(_ number: String) {
        switch PhoneSettings.shared.macMode {
        case .softphone:
            CallCenter.shared.startOutgoing(number: number, video: false)
        case .deskPhone:
            DeskPhoneController.shared.dial(number)
            // Der Tischtelefon-Status meldet den Anruf nicht immer mit Nummer: hier merken
            CallHistory.shared.noteDeskDial(number)
        }
    }
}

final class MacAppDelegate: NSObject, NSApplicationDelegate {
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated {
            let settings = PhoneSettings.shared
            // Video gibt es in der Menüleisten-App nicht (kein Videofenster)
            settings.videoEnabled = false
            SIPManager.shared.addCallStateHandler { call, state in
                MainActor.assumeIsolated { CallHistory.shared.softphone(call: call, state: state) }
            }
            SIPManager.shared.start()
            ToastController.shared.setup()
            Provisioning.refresh()
            // Erster Start: Einstellungen zeigen, sonst sieht man außer dem Menüleisten-Symbol nichts
            if !settings.isConfigured { SettingsWindow.shared.show() }

            // Statusabo für das Tischtelefon, sobald der Mac angemeldet ist und der Modus passt
            Publishers.CombineLatest(SIPManager.shared.$registration.removeDuplicates(), settings.$macMode.removeDuplicates())
                .receive(on: RunLoop.main)
                .sink { reg, mode in
                    MainActor.assumeIsolated {
                        if case .ok = reg, mode == .deskPhone {
                            DeskPhoneController.shared.startMonitoring()
                        } else if mode == .softphone {
                            DeskPhoneController.shared.stopMonitoring()
                        }
                    }
                }
                .store(in: &cancellables)

            // Einstellungen aus cme-web regelmäßig auffrischen (z.B. neues Local-Push-Secret, Voicemail-Nummer)
            Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { _ in
                Task { @MainActor in Provisioning.refresh() }
            }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        MainActor.assumeIsolated { urls.forEach(URLRouter.handle) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    /// App im Finder/Launchpad erneut geöffnet: Einstellungen zeigen (es gibt kein Hauptfenster).
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        MainActor.assumeIsolated { SettingsWindow.shared.show() }
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            DeskPhoneController.shared.stopMonitoring()
            // Bewusst KEIN core.stop(): das würde die Anmeldung abmelden. Alle Geräte einer Rufnummer teilen sich
            // an CME einen Pool (eine Anmeldung über Flexisip) – ein Abmelden des Macs würde auch iPhone und iPad
            // bei CME austragen. Flexisip entfernt den Mac nach Ablauf selbst.
        }
    }
}

/// Einstellungsfenster (statt Settings-Scene, damit es sich überall öffnen lässt).
@MainActor
final class SettingsWindow {
    static let shared = SettingsWindow()
    private var window: NSWindow?

    func show() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 740, height: 580),
                             styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            w.title = "CME Phone – Einstellungen"
            w.isReleasedWhenClosed = false
            w.contentView = NSHostingView(rootView: MacSettingsView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(DeskPhoneController.shared))
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
