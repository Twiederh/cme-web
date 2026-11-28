import AppKit
@preconcurrency import UserNotifications

/// „Anna wartet in der Lobby von team“: cme-web meldet Gäste in der Lobby eines Jitsi-Raums (Tab Jitsi →
/// Benachrichtigen). Der Mac hat keinen Push, deshalb fragt die App per Long-Poll (/client/v1/events). Ein Klick
/// auf die Mitteilung öffnet den Raum im Browser.
@MainActor
final class LobbyWatcher: NSObject {
    static let shared = LobbyWatcher()
    nonisolated static let category = "JITSI_LOBBY"
    nonisolated static let openAction = "OPEN"
    private var task: Task<Void, Never>?

    func start() {
        guard task == nil else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Self.category,
                                   actions: [UNNotificationAction(identifier: Self.openAction, title: "Raum öffnen", options: [.foreground])],
                                   intentIdentifiers: [], options: [])
        ])
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        task = Task { await self.loop() }
    }

    private func loop() async {
        var since = 0
        var ext = ""
        var failures = 0
        while !Task.isCancelled {
            let s = PhoneSettings.shared
            let now = s.extensionNumber.trimmingCharacters(in: .whitespaces)
            if now != ext { ext = now; since = 0 }
            guard s.hasCmeWeb, !ext.isEmpty else {
                try? await Task.sleep(for: .seconds(30)); continue
            }
            do {
                let r = try await ClientAPI.events(ext: ext, since: since, wait: since == 0 ? 0 : 25)
                if since == 0 { SIPManager.shared.log("Lobby: warte auf Mitteilungen für \(ext)") }
                for e in r.events where e.type == "lobby" { show(e) }
                since = max(r.last, 1)
                failures = 0
            } catch {
                failures += 1
                if failures == 1 { SIPManager.shared.log("Lobby: cme-web nicht erreichbar: \(error.localizedDescription)") }
                try? await Task.sleep(for: .seconds(min(60, 5 * failures)))
            }
        }
    }

    private func show(_ e: ClientAPI.AppEvent) {
        let room = e.room ?? ""
        let c = UNMutableNotificationContent()
        c.title = "Lobby \(room)"
        c.body = "\(e.name ?? "Jemand") wartet – klicken zum Öffnen"
        c.sound = .default
        c.categoryIdentifier = Self.category
        c.threadIdentifier = "lobby"
        if let u = e.url { c.userInfo = ["url": u] }
        SIPManager.shared.log("Lobby: \(e.name ?? "?") wartet in \(room)")
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "lobby-\(e.id)", content: c, trigger: nil))
    }
}

extension LobbyWatcher: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let a = response.actionIdentifier
        if a == Self.openAction || a == UNNotificationDefaultActionIdentifier,
           let s = response.notification.request.content.userInfo["url"] as? String,
           let url = URL(string: s), url.scheme == "https" {
            Task { @MainActor in NSWorkspace.shared.open(url) }
        }
        completionHandler()
    }
}
