import Foundation
import UIKit
import UserNotifications

/// Mitteilung „Thomas Handy – Gehalten, tippen zum Übernehmen“: Hält ein anderes Gerät der Shared Line ein Gespräch
/// (Tischtelefon, Mac, Gigaset), meldet cme-web das per normalem Push (Kategorie HELD_CALL, Daten unter "held").
/// "Übernehmen" startet über CallKit ein INVITE mit "Replaces" – CME verbindet den Anrufer mit dem iPhone.
/// Wird das Gespräch dort fortgesetzt oder beendet, kommt ein stiller Push ("heldEnd") und die Mitteilung verschwindet.
@MainActor
final class HeldCallNotifications: NSObject {
    static let shared = HeldCallNotifications()
    nonisolated static let category = "HELD_CALL"
    nonisolated static let takeOverAction = "TAKEOVER"

    /// Zuletzt von iOS vergebener Push-Token (hex) – wird bei jeder Einstellungs-Aktualisierung an cme-web gemeldet.
    private(set) var token = ""
    private var sentFor = ""

    private var env: String {
        #if DEBUG
        return "sandbox"
        #else
        return "prod"
        #endif
    }

    func setup(_ application: UIApplication) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let take = UNNotificationAction(identifier: Self.takeOverAction, title: "Übernehmen", options: [.foreground])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Self.category, actions: [take], intentIdentifiers: [], options: [])
        ])
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        application.registerForRemoteNotifications()
    }

    func didRegister(token data: Data) {
        token = data.map { String(format: "%02x", $0) }.joined()
        sendToken()
    }

    /// An cme-web melden (nur Mobilgeräte über das Push-Gateway mit Gerätenamen); nach Änderungen erneut.
    func sendToken() {
        let s = PhoneSettings.shared
        guard !token.isEmpty, !s.jabberMode, !s.deviceLabel.isEmpty, s.hasCmeWeb else { return }
        let key = "\(s.deviceLabel)|\(token)|\(env)|\(s.cmeWebURL)"
        guard key != sentFor else { return }
        let device = s.deviceLabel, token = token, env = env
        Task { @MainActor in
            do {
                try await ClientAPI.registerPush(device: device, token: token, env: env)
                self.sentFor = key
                SIPManager.shared.log("Mitteilungen: Push-Token an cme-web gemeldet (\(env))")
            } catch {
                SIPManager.shared.log("Mitteilungen: Push-Token nicht gemeldet: \(error.localizedDescription)")
            }
        }
    }

    /// Stiller Push "heldEnd": Mitteilung zu diesem Gespräch entfernen.
    func handleBackground(_ userInfo: [AnyHashable: Any]) -> Bool {
        guard let end = userInfo["heldEnd"] as? [String: Any], let callId = end["callId"] as? String else { return false }
        removeNotifications(callId: callId)
        return true
    }

    private func removeNotifications(callId: String) {
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { list in
            let ids = list.filter { n in
                (n.request.content.userInfo["held"] as? [String: Any])?["callId"] as? String == callId
            }.map { $0.request.identifier }
            center.removeDeliveredNotifications(withIdentifiers: ids)
        }
    }

    nonisolated private static func held(from userInfo: [AnyHashable: Any]) -> SIPManager.HeldLineCall? {
        guard let h = userInfo["held"] as? [String: Any],
              let callId = h["callId"] as? String, let uri = h["remoteURI"] as? String, !uri.isEmpty else { return nil }
        return SIPManager.HeldLineCall(callId: callId, localTag: h["localTag"] as? String ?? "",
                                       remoteTag: h["remoteTag"] as? String ?? "", remoteURI: uri,
                                       remoteName: h["remoteName"] as? String ?? "", holder: h["holder"] as? String ?? "")
    }
}

extension HeldCallNotifications: UNUserNotificationCenterDelegate {
    /// Auch bei geöffneter App als Banner zeigen.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let content = response.notification.request.content
        let action = response.actionIdentifier
        let take = action == HeldCallNotifications.takeOverAction || action == UNNotificationDefaultActionIdentifier
        if content.categoryIdentifier == HeldCallNotifications.category, take,
           let h = Self.held(from: content.userInfo) {
            Task { @MainActor in
                SIPManager.shared.start()
                CallKitProvider.shared.startTakeover(h)
            }
        }
        completionHandler()
    }
}
