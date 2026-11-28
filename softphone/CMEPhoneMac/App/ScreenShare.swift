import Foundation
import Combine
import ScreenCaptureKit
import CMESIP

/// Bildschirm teilen im Gespräch: Statt der Kamera geht der ausgewählte Bildschirm, eine App oder ein Fenster
/// über den vorhandenen Videostrom an die Gegenstelle (kein BFCP, also mit jeder Gegenstelle, die Video kann).
/// Die Auswahl macht der Systemdialog von macOS (SCContentSharingPicker); dafür braucht die App keine
/// Freigabe „Bildschirmaufnahme“ in den Systemeinstellungen.
@MainActor
final class ScreenShare: NSObject, ObservableObject, SCContentSharingPickerObserver {
    static let shared = ScreenShare()

    /// Gespräch, in dem gerade geteilt wird
    @Published private(set) var callId: String?
    private var pendingCall: String?
    private var callsSub: AnyCancellable?

    /// SCContentFilter über Threads reichen (der Rückruf des Dialogs kommt nicht unbedingt auf dem Main-Thread)
    private struct FilterBox: @unchecked Sendable { let filter: SCContentFilter }

    private override init() {
        super.init()
        let p = SCContentSharingPicker.shared
        var cfg = SCContentSharingPickerConfiguration()
        cfg.allowedPickerModes = [.singleWindow, .singleApplication, .singleDisplay]
        cfg.excludedBundleIDs = [Bundle.main.bundleIdentifier ?? ""]
        p.defaultConfiguration = cfg
        p.maximumStreamCount = 1
        p.add(self)
        // Gespräch weg: Freigabe aufräumen
        callsSub = SIPManager.shared.$calls.sink { [weak self] calls in
            guard let self, let id = self.callId, !calls.contains(where: { $0.id == id }) else { return }
            self.callEnded(id)
        }
    }

    func isSharing(_ id: String) -> Bool { callId == id }

    /// Auswahldialog zeigen; die Freigabe startet, sobald etwas ausgewählt ist
    func start(callId id: String) {
        pendingCall = id
        let p = SCContentSharingPicker.shared
        p.isActive = true
        p.present()
    }

    func stop() {
        guard let id = callId else { return }
        callId = nil
        SCContentSharingPicker.shared.isActive = false
        let err = cmesip_set_screen(id, false)
        cmesip_screen_filter(nil)
        SIPManager.shared.log(err == 0 ? "Bildschirmfreigabe beendet, wieder Kamera" : "Bildschirmfreigabe beenden: Fehler \(err)")
    }

    /// Gespräch beendet: Freigabe aufräumen (ohne Umschalten, das Gespräch ist weg)
    func callEnded(_ id: String) {
        guard callId == id else { return }
        callId = nil
        SCContentSharingPicker.shared.isActive = false
        cmesip_screen_filter(nil)
    }

    private func apply(_ filter: SCContentFilter) {
        guard let id = pendingCall ?? callId else { return }
        cmesip_screen_filter(Unmanaged.passUnretained(filter).toOpaque())
        let err = cmesip_set_screen(id, true)
        if err == 0 {
            callId = id
            SIPManager.shared.log("Bildschirmfreigabe: \(Self.describe(filter))")
        } else {
            cmesip_screen_filter(nil)
            SIPManager.shared.log(err == ENOTSUP ? "Bildschirm teilen geht nur in einem Gespräch mit Video"
                                                 : "Bildschirmfreigabe: Fehler \(err)")
        }
        pendingCall = nil
    }

    private static func describe(_ f: SCContentFilter) -> String {
        switch f.style {
        case .window: return "Fenster"
        case .application: return "App"
        case .display: return "Bildschirm"
        default: return "Auswahl"
        }
    }

    // MARK: SCContentSharingPickerObserver

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                                          for stream: SCStream?) {
        let box = FilterBox(filter: filter)
        Task { @MainActor in self.apply(box.filter) }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor in
            self.pendingCall = nil
            if self.callId == nil { SCContentSharingPicker.shared.isActive = false }
        }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        let text = error.localizedDescription
        Task { @MainActor in
            self.pendingCall = nil
            SIPManager.shared.log("Bildschirmfreigabe: \(text)")
        }
    }
}
