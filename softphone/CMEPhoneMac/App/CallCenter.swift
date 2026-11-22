import Foundation
import AppKit
import linphonesw

/// macOS-Gegenstück zu CallKitProvider (CallKit gibt es auf macOS nicht).
/// Gleiche Methodennamen, damit der gemeinsame SIPManager unverändert bleibt.
/// Angezeigt werden Anrufe im Call-Toast unten rechts (ToastController beobachtet SIPManager.calls);
/// den Klingelton spielt liblinphone.
@MainActor
final class CallCenter: NSObject, ObservableObject {
    static let shared = CallCenter()
    private var sip: SIPManager { SIPManager.shared }

    private override init() { super.init() }

    // MARK: - API wie CallKitProvider (wird von SIPManager aufgerufen)

    func reportIncoming(callId: String, handle: String, displayName: String?, hasVideo: Bool,
                        completion: (() -> Void)? = nil) {
        NSApp.requestUserAttention(.criticalRequest)
        completion?()
    }
    func reportOutgoingStarted(callId: String, call: Call) {}
    func reportOutgoingConnected(callId: String) {}
    func reportEnded(callId: String, failed: Bool) {}
    func forget(callId: String) {}

    // MARK: - Aktionen

    func startOutgoing(number: String, video: Bool) {
        _ = sip.placeCall(to: number, video: false)
    }
    func requestAnswer(callId: String) { sip.accept(callId: callId) }
    func requestEnd(callId: String) { sip.terminate(callId: callId) }
    func requestHold(callId: String, held: Bool) { sip.setHeld(callId: callId, held: held) }
    func requestMute(callId: String, muted: Bool) { sip.setMuted(muted) }
    func requestDTMF(callId: String, digits: String) { sip.sendDTMF(digits, callId: callId) }
}
