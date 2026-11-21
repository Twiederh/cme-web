import Foundation
import CallKit
import AVFoundation
import linphonesw

/// Plattformneutraler Name, den der gemeinsame Kern (SIPManager) verwendet.
/// Auf macOS gibt es stattdessen eine eigene `CallCenter`-Klasse ohne CallKit.
typealias CallCenter = CallKitProvider

/// CallKit-Integration: jeder SIP-Anruf (Call-ID) bekommt eine CallKit-UUID.
/// Alle Aktionen (Annehmen, Auflegen, Halten, Stumm, DTMF) laufen über CXProvider,
/// damit Sperrbildschirm, Freisprechen, CarPlay, Bluetooth und "Zuletzt" in der Telefon-App funktionieren.
@MainActor
final class CallKitProvider: NSObject {
    static let shared = CallKitProvider()

    private let provider: CXProvider
    private let controller = CXCallController()
    private var uuidByCallId: [String: UUID] = [:]
    private var callIdByUUID: [UUID: String] = [:]
    /// Anrufe, die per Push gemeldet wurden, bevor das INVITE angekommen ist und der Nutzer schon angenommen hat.
    private var pendingAnswer: Set<String> = []

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = true
        config.maximumCallGroups = 2
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.phoneNumber, .generic]
        config.includesCallsInRecents = true
        config.iconTemplateImageData = nil
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: .main)
    }

    private var sip: SIPManager { SIPManager.shared }

    // MARK: - Eingehend

    /// Meldet einen eingehenden Anruf. Mehrfachaufrufe mit derselben Call-ID werden ignoriert
    /// (VoIP-Push, Local Push und das eigentliche INVITE können alle eintreffen).
    func reportIncoming(callId: String, handle: String, displayName: String?, hasVideo: Bool,
                        completion: (() -> Void)? = nil) {
        if let existing = uuidByCallId[callId] {
            // Ggf. Anzeige aktualisieren (Push kannte evtl. nur die Nummer)
            let update = CXCallUpdate()
            update.remoteHandle = CXHandle(type: .phoneNumber, value: handle)
            if let displayName, !displayName.isEmpty { update.localizedCallerName = displayName }
            update.hasVideo = hasVideo
            provider.reportCall(with: existing, updated: update)
            if pendingAnswer.remove(callId) != nil { sip.accept(callId: callId) }
            completion?()
            return
        }
        let uuid = UUID()
        uuidByCallId[callId] = uuid
        callIdByUUID[uuid] = callId

        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: handle)
        update.localizedCallerName = (displayName?.isEmpty == false) ? displayName : handle
        update.hasVideo = hasVideo
        update.supportsDTMF = true
        update.supportsHolding = true
        update.supportsGrouping = false
        update.supportsUngrouping = false

        provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.sip.log("CallKit lehnt Anruf ab: \(error.localizedDescription)")
                    self?.sip.terminate(callId: callId)
                    self?.forget(callId: callId)
                }
            }
            completion?()
        }
    }

    // MARK: - Ausgehend

    func startOutgoing(number: String, video: Bool) {
        let uuid = UUID()
        let handle = CXHandle(type: .phoneNumber, value: number)
        let action = CXStartCallAction(call: uuid, handle: handle)
        action.isVideo = video
        controller.request(CXTransaction(action: action)) { [weak self] error in
            if let error { Task { @MainActor in self?.sip.log("CallKit Start: \(error.localizedDescription)") } }
        }
    }

    func reportOutgoingStarted(callId: String, call: Call) {
        guard let uuid = uuidByCallId[callId] else { return }
        provider.reportOutgoingCall(with: uuid, startedConnectingAt: Date())
    }

    func reportOutgoingConnected(callId: String) {
        guard let uuid = uuidByCallId[callId] else { return }
        provider.reportOutgoingCall(with: uuid, connectedAt: Date())
    }

    // MARK: - Ende

    func reportEnded(callId: String, failed: Bool) {
        guard let uuid = uuidByCallId[callId] else { return }
        provider.reportCall(with: uuid, endedAt: Date(), reason: failed ? .failed : .remoteEnded)
    }

    func forget(callId: String) {
        if let uuid = uuidByCallId.removeValue(forKey: callId) { callIdByUUID.removeValue(forKey: uuid) }
        pendingAnswer.remove(callId)
    }

    // MARK: - UI-Aktionen (über CallKit, damit System-UI synchron bleibt)

    func requestEnd(callId: String) { request(CXEndCallAction(call: uuid(callId))) }
    func requestAnswer(callId: String) { request(CXAnswerCallAction(call: uuid(callId))) }
    func requestHold(callId: String, held: Bool) { request(CXSetHeldCallAction(call: uuid(callId), onHold: held)) }
    func requestMute(callId: String, muted: Bool) { request(CXSetMutedCallAction(call: uuid(callId), muted: muted)) }
    func requestDTMF(callId: String, digits: String) {
        request(CXPlayDTMFCallAction(call: uuid(callId), digits: digits, type: .singleTone))
    }

    private func uuid(_ callId: String) -> UUID { uuidByCallId[callId] ?? UUID() }

    private func request(_ action: CXAction) {
        controller.request(CXTransaction(action: action)) { [weak self] error in
            if let error { Task { @MainActor in self?.sip.log("CallKit-Aktion: \(error.localizedDescription)") } }
        }
    }
}

extension CallKitProvider: CXProviderDelegate {
    // Delegate-Queue ist .main (siehe init) – daher synchron auf dem MainActor.
    nonisolated func providerDidReset(_ provider: CXProvider) {
        MainActor.assumeIsolated {
            for call in self.sip.core?.calls ?? [] { try? call.terminate() }
            self.uuidByCallId.removeAll(); self.callIdByUUID.removeAll()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        MainActor.assumeIsolated {
            guard let call = self.sip.placeCall(to: action.handle.value, video: action.isVideo),
                  let callId = call.callLog?.callId else { action.fail(); return }
            self.uuidByCallId[callId] = action.callUUID
            self.callIdByUUID[action.callUUID] = callId
            let update = CXCallUpdate()
            update.remoteHandle = action.handle
            update.hasVideo = action.isVideo
            update.supportsDTMF = true
            update.supportsHolding = true
            provider.reportCall(with: action.callUUID, updated: update)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        MainActor.assumeIsolated {
            guard let callId = self.callIdByUUID[action.callUUID] else { action.fail(); return }
            if self.sip.call(withId: callId) == nil {
                // Push war schneller als das INVITE: annehmen, sobald es eintrifft.
                self.pendingAnswer.insert(callId)
            } else {
                self.sip.accept(callId: callId)
            }
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        MainActor.assumeIsolated {
            if let callId = self.callIdByUUID[action.callUUID] {
                if self.sip.call(withId: callId) != nil {
                    self.sip.terminate(callId: callId)
                } else {
                    self.sip.log("Ende für unbekannten Call \(callId.prefix(8))")
                }
                self.forget(callId: callId)
            }
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXSetHeldCallAction) {
        MainActor.assumeIsolated {
            if let callId = self.callIdByUUID[action.callUUID] { self.sip.setHeld(callId: callId, held: action.isOnHold) }
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        MainActor.assumeIsolated {
            self.sip.setMuted(action.isMuted)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXPlayDTMFCallAction) {
        MainActor.assumeIsolated {
            self.sip.sendDTMF(action.digits, callId: self.callIdByUUID[action.callUUID])
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) {
        action.fulfill()
    }

    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated { self.sip.core?.activateAudioSession(actived: true) }
    }

    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated { self.sip.core?.activateAudioSession(actived: false) }
    }
}
