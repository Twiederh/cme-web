import Foundation
import Combine

/// Aufnahme-Knopf für das laufende Gespräch.
/// 1. Externe Gespräche: cme-recorder (SIPREC am Trunk) – Ein/Aus über cme-web, für Softphone und Tischtelefon.
/// 2. Interne Gespräche im Softphone-Modus: lokale Aufnahme in der App (WAV), wenn eingeschaltet.
@MainActor
final class RecordingController: ObservableObject {
    static let shared = RecordingController()

    enum Source: Equatable { case none, server(String), local(String) }

    @Published private(set) var source: Source = .none
    @Published private(set) var isOn = false
    @Published private(set) var busy = false
    @Published private(set) var message = ""

    private var ext = ""
    private var localCallId: String?
    private var timer: Timer?

    /// Für ein laufendes Gespräch beobachten. ext = eigene Nebenstelle (Softphone) bzw. die des Tischtelefons.
    /// localCallId = SIP-Call-ID im Softphone-Modus (für lokale Aufnahme), nil bei Telefonsteuerung.
    func track(ext: String, localCallId: String?) {
        guard ext != self.ext || localCallId != self.localCallId || timer == nil else { return }
        self.ext = ext
        self.localCallId = localCallId
        message = ""
        poll()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    func stop() {
        timer?.invalidate(); timer = nil
        source = .none; isOn = false; busy = false; message = ""
        ext = ""; localCallId = nil
    }

    /// Kann der Knopf etwas tun?
    var available: Bool { source != .none }

    var help: String {
        switch source {
        case .server: return isOn ? "Aufnahme läuft (cme-recorder) – klicken zum Beenden" : "Gespräch aufnehmen (cme-recorder)"
        case .local: return isOn ? "Lokale Aufnahme läuft – klicken zum Beenden" : "Gespräch lokal aufnehmen"
        case .none: return message.isEmpty ? "Keine Aufnahme möglich" : message
        }
    }

    private func poll() {
        let s = PhoneSettings.shared
        let localId = localCallId
        if s.hasCmeWeb, !ext.isEmpty {
            let ext = self.ext
            Task { @MainActor in
                do {
                    let calls = try await ClientAPI.calls(ext: ext)
                    guard ext == self.ext else { return }
                    if let c = calls.first {
                        self.source = .server(c.id)
                        if !self.busy { self.isOn = c.recording }
                        self.message = ""
                    } else {
                        self.fallbackLocal(localId)
                    }
                } catch {
                    self.message = error.localizedDescription
                    self.fallbackLocal(localId)
                }
            }
        } else {
            fallbackLocal(localId)
        }
    }

    private func fallbackLocal(_ callId: String?) {
        if let callId, PhoneSettings.shared.localRecording,
           let info = SIPManager.shared.calls.first(where: { $0.id == callId }) {
            source = .local(callId)
            if !busy { isOn = info.isRecording }
        } else {
            source = .none
            isOn = false
            if message.isEmpty {
                message = PhoneSettings.shared.macMode == .deskPhone || callId == nil
                    ? "Aufnahme nur bei externen Gesprächen (cme-recorder)" : "Lokale Aufnahme ist ausgeschaltet"
            }
        }
    }

    func toggle() {
        let want = !isOn
        switch source {
        case .none:
            return
        case .local(let id):
            SIPManager.shared.setLocalRecording(callId: id, on: want)
            isOn = want
        case .server(let id):
            busy = true
            isOn = want
            let ext = self.ext
            Task { @MainActor in
                defer { self.busy = false }
                do {
                    try await ClientAPI.setRecording(id: id, ext: ext, on: want)
                } catch {
                    self.isOn = !want
                    self.message = error.localizedDescription
                    SIPManager.shared.log("Aufnahme: \(error.localizedDescription)")
                }
            }
        }
    }
}
