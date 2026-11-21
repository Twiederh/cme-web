import Foundation
import AppKit
import UserNotifications
import linphonesw

/// macOS-Gegenstück zu CallKitProvider (CallKit gibt es auf macOS nicht).
/// Gleiche Methodennamen, damit der gemeinsame SIPManager unverändert bleibt.
/// Eingehende Anrufe: Mitteilung mit "Annehmen"/"Ablehnen" + Anruffenster nach vorne.
@MainActor
final class CallCenter: NSObject, ObservableObject {
    static let shared = CallCenter()

    /// Wird gesetzt, wenn das Anruffenster gezeigt werden soll (vom App-Code beobachtet).
    @Published var showCallWindow = false

    private let center = UNUserNotificationCenter.current()
    private var sip: SIPManager { SIPManager.shared }

    private enum Category {
        static let softphone = "INCOMING_CALL"
        static let deskPhone = "DESKPHONE_RINGING"
    }
    private enum Action {
        static let answer = "ANSWER"
        static let decline = "DECLINE"
        static let answerVideo = "ANSWER_VIDEO"
    }
    private static let deskPhoneNotificationId = "deskphone-ringing"

    private override init() {
        super.init()
    }

    func setup() {
        center.delegate = self
        let answer = UNNotificationAction(identifier: Action.answer, title: "Annehmen", options: [.foreground])
        let video = UNNotificationAction(identifier: Action.answerVideo, title: "Mit Video", options: [.foreground])
        let decline = UNNotificationAction(identifier: Action.decline, title: "Ablehnen", options: [.destructive])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Category.softphone, actions: [answer, video, decline], intentIdentifiers: []),
            UNNotificationCategory(identifier: Category.deskPhone, actions: [answer, decline], intentIdentifiers: []),
        ])
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            Task { @MainActor in SIPManager.shared.log("Mitteilungen \(granted ? "erlaubt" : "nicht erlaubt")") }
        }
    }

    // MARK: - API wie CallKitProvider (wird von SIPManager aufgerufen)

    func reportIncoming(callId: String, handle: String, displayName: String?, hasVideo: Bool,
                        completion: (() -> Void)? = nil) {
        let content = UNMutableNotificationContent()
        content.title = (displayName?.isEmpty == false ? displayName! : handle)
        content.subtitle = hasVideo ? "Eingehender Videoanruf" : "Eingehender Anruf"
        content.body = handle
        content.categoryIdentifier = Category.softphone
        content.userInfo = ["call-id": callId]
        content.sound = nil   // liblinphone spielt auf macOS den Klingelton selbst
        content.interruptionLevel = .timeSensitive
        center.add(UNNotificationRequest(identifier: callId, content: content, trigger: nil))
        showCallWindow = true
        NSApp.requestUserAttention(.criticalRequest)
        completion?()
    }

    func reportOutgoingStarted(callId: String, call: Call) { showCallWindow = true }
    func reportOutgoingConnected(callId: String) {}

    func reportEnded(callId: String, failed: Bool) {
        center.removeDeliveredNotifications(withIdentifiers: [callId])
        center.removePendingNotificationRequests(withIdentifiers: [callId])
    }

    func forget(callId: String) {
        if sip.calls.isEmpty { showCallWindow = false }
    }

    // MARK: - Aktionen aus der UI (direkt, ohne CallKit)

    func startOutgoing(number: String, video: Bool) {
        showCallWindow = true
        if sip.placeCall(to: number, video: video) == nil { showCallWindow = !sip.calls.isEmpty }
    }
    func requestAnswer(callId: String) {
        center.removeDeliveredNotifications(withIdentifiers: [callId])
        sip.accept(callId: callId)
    }
    func requestEnd(callId: String) { sip.terminate(callId: callId) }
    func requestHold(callId: String, held: Bool) { sip.setHeld(callId: callId, held: held) }
    func requestMute(callId: String, muted: Bool) { sip.setMuted(muted) }
    func requestDTMF(callId: String, digits: String) { sip.sendDTMF(digits, callId: callId) }

    // MARK: - Tischtelefon

    func notifyDeskPhoneRinging(number: String?, name: String?) {
        let content = UNMutableNotificationContent()
        content.title = name?.isEmpty == false ? name! : (number ?? "Unbekannt")
        content.subtitle = "Anruf am Tischtelefon \(PhoneSettings.shared.deskPhoneExtension)"
        if let number { content.body = number }
        content.categoryIdentifier = Category.deskPhone
        content.sound = .default
        content.interruptionLevel = .timeSensitive
        center.add(UNNotificationRequest(identifier: Self.deskPhoneNotificationId, content: content, trigger: nil))
    }

    func clearDeskPhoneNotification() {
        center.removeDeliveredNotifications(withIdentifiers: [Self.deskPhoneNotificationId])
    }
}

extension CallCenter: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let category = response.notification.request.content.categoryIdentifier
        let callId = response.notification.request.content.userInfo["call-id"] as? String
        let action = response.actionIdentifier
        Task { @MainActor in
            if category == Category.deskPhone {
                switch action {
                case Action.answer: DeskPhoneController.shared.answer()
                case Action.decline: DeskPhoneController.shared.decline()
                default: break
                }
            } else if let callId {
                switch action {
                case Action.answer: self.requestAnswer(callId: callId)
                case Action.answerVideo:
                    PhoneSettings.shared.videoEnabled = true
                    self.requestAnswer(callId: callId)
                case Action.decline: self.requestEnd(callId: callId)
                default: self.showCallWindow = true
                }
            }
            NSApp.activate(ignoringOtherApps: true)
            completionHandler()
        }
    }
}
