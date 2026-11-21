import Foundation
import NetworkExtension

/// Konfiguriert die Local-Push-Extension (NEAppPushManager) für die Heim-WLANs.
/// Im Heim-WLAN hält iOS die Extension am Leben; sie hält eine TCP-Verbindung zum push-relay.
/// Kommt ein Anruf, meldet die Extension ihn per `reportIncomingCall`, iOS startet/weckt die App
/// und ruft `appPushManager(_:didReceiveIncomingCallWithUserInfo:)` auf.
@MainActor
final class LocalPushController: NSObject, ObservableObject {
    static let shared = LocalPushController()

    @Published private(set) var isActive = false
    @Published private(set) var status = "–"

    private var manager: NEAppPushManager?
    private var deviceToken: String?
    private let settings = PhoneSettings.shared

    /// Muss beim App-Start aufgerufen werden, damit der Delegate gesetzt ist, bevor iOS den Anruf zustellt.
    func load() {
        NEAppPushManager.loadAllFromPreferences { [weak self] managers, error in
            Task { @MainActor in
                guard let self else { return }
                if let error { self.status = "Laden: \(error.localizedDescription)" }
                self.manager = managers?.first
                self.manager?.delegate = self
                self.isActive = self.manager?.isActive ?? false
                self.apply()
            }
        }
    }

    func update(deviceToken: String) {
        self.deviceToken = deviceToken
        apply()
    }

    /// Schreibt die aktuelle Konfiguration (SSIDs, Relay, Token) in die Systemeinstellungen.
    func apply() {
        guard settings.localPushEnabled, !settings.ssidList.isEmpty, !settings.relayHost.isEmpty else {
            if let manager {
                manager.removeFromPreferences { _ in }
                self.manager = nil
                status = "Deaktiviert"
            }
            return
        }
        guard let token = deviceToken ?? manager?.providerConfiguration[SharedConfig.ProviderKey.deviceToken] as? String else {
            status = "Warte auf Push-Token (erst einmal anmelden)"
            return
        }

        let m = manager ?? NEAppPushManager()
        m.localizedDescription = "CME Phone – Heimnetz"
        m.providerBundleIdentifier = "de.wiederhol.cmephone.localpush"
        m.delegate = self
        m.matchSSIDs = settings.ssidList
        m.providerConfiguration = [
            SharedConfig.ProviderKey.relayHost: settings.relayHost,
            SharedConfig.ProviderKey.relayPort: settings.relayPort,
            SharedConfig.ProviderKey.relaySecret: settings.relaySecret,
            SharedConfig.ProviderKey.deviceToken: token,
        ]
        m.isEnabled = true
        m.saveToPreferences { [weak self] error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.status = "Speichern: \(error.localizedDescription)"
                } else {
                    self.manager = m
                    self.status = "Konfiguriert für \(self.settings.ssidList.joined(separator: ", "))"
                }
            }
        }
    }
}

extension LocalPushController: NEAppPushDelegate {
    nonisolated func appPushManager(_ manager: NEAppPushManager, didReceiveIncomingCallWithUserInfo userInfo: [AnyHashable: Any] = [:]) {
        let callId = userInfo[SharedConfig.CallInfoKey.callId] as? String ?? UUID().uuidString
        let from = userInfo[SharedConfig.CallInfoKey.fromURI] as? String ?? ""
        let name = userInfo[SharedConfig.CallInfoKey.displayName] as? String
        let video = userInfo[SharedConfig.CallInfoKey.hasVideo] as? Bool ?? false
        let number = Self.number(fromURI: from)

        // CallKit sofort und synchron informieren (Pflicht bei Local Push wie bei PushKit).
        let work: @MainActor () -> Void = {
            CallKitProvider.shared.reportIncoming(callId: callId, handle: number, displayName: name, hasVideo: video)
            SIPManager.shared.processPush(callId: callId)
        }
        if Thread.isMainThread { MainActor.assumeIsolated(work) } else { DispatchQueue.main.sync { MainActor.assumeIsolated(work) } }
    }

    nonisolated static func number(fromURI uri: String) -> String {
        // sip:0711123456@192.168.11.4;user=phone -> 0711123456
        var s = uri
        if let r = s.range(of: "sip:") ?? s.range(of: "sips:") { s = String(s[r.upperBound...]) }
        if let at = s.firstIndex(of: "@") { s = String(s[..<at]) }
        return s.isEmpty ? "Unbekannt" : s
    }
}
