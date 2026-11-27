import SwiftUI
import UIKit
import Intents
import Combine

@main
struct CMEPhoneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(LocalPushController.shared)
                // Anrufen aus "Zuletzt", Kontakte, Siri (INStartCallIntent)
                .onContinueUserActivity("INStartCallIntent") { CallRouter.handle(activity: $0) }
                .onContinueUserActivity("INStartAudioCallIntent") { CallRouter.handle(activity: $0) }
                .onContinueUserActivity("INStartVideoCallIntent") { CallRouter.handle(activity: $0) }
                // tel:-Links, wenn CME Phone die Standard-App für Anrufe ist (iOS 18.2+)
                .onOpenURL { CallRouter.handle(url: $0) }
        }
    }
}

@MainActor
enum CallRouter {
    static func handle(activity: NSUserActivity) {
        guard let intent = activity.interaction?.intent else { return }
        var handle: String?
        var video = false
        if let i = intent as? INStartCallIntent {
            handle = i.contacts?.first?.personHandle?.value
            video = i.callCapability == .videoCall
        } else if let i = intent as? INStartAudioCallIntent {
            handle = i.contacts?.first?.personHandle?.value
        } else if let i = intent as? INStartVideoCallIntent {
            handle = i.contacts?.first?.personHandle?.value
            video = true
        }
        guard let number = handle.map(clean), !number.isEmpty else { return }
        CallKitProvider.shared.startOutgoing(number: number, video: video)
    }

    static func handle(url: URL) {
        guard let scheme = url.scheme?.lowercased(), ["tel", "telprompt", "sip", "cmephone"].contains(scheme) else { return }
        var s = url.absoluteString
        if let r = s.range(of: ":") { s = String(s[r.upperBound...]) }
        s = s.replacingOccurrences(of: "//", with: "").removingPercentEncoding ?? s
        if scheme == "cmephone", s.hasPrefix("dial/") { s = String(s.dropFirst(5)) }
        if let semi = s.firstIndex(of: ";") { s = String(s[..<semi]) }
        let number = clean(s)
        guard !number.isEmpty else { return }
        CallKitProvider.shared.startOutgoing(number: number, video: false)
    }

    /// "+49 711 1234-5" -> "+4971112345"; sip:21@... -> 21
    static func clean(_ s: String) -> String {
        var v = s
        if v.lowercased().hasPrefix("sip:") { v = String(v.dropFirst(4)) }
        if let at = v.firstIndex(of: "@") { v = String(v[..<at]) }
        let plus = v.trimmingCharacters(in: .whitespaces).hasPrefix("+")
        let digits = v.filter { "0123456789*#".contains($0) }
        return (plus ? "+" : "") + digits
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    private var cancellables = Set<AnyCancellable>()

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Reihenfolge wichtig: CallKit-Provider und Local-Push-Delegate müssen stehen,
        // bevor iOS einen per Push gestarteten Anruf zustellt.
        _ = CallKitProvider.shared
        // Anrufliste der App (iPad/Vision Pro haben keine Telefon-App mit „Zuletzt“)
        SIPManager.shared.addCallStateHandler { call, state in
            MainActor.assumeIsolated {
                let id = call.callLog?.callId ?? ""
                switch state {
                case .Connected, .StreamsRunning: CallHistory.shared.softphoneConnected(id: id)
                case .Released:
                    CallHistory.shared.softphoneEnded(
                        id: id, incoming: call.dir == .Incoming, number: call.remoteAddress?.username ?? "",
                        name: call.remoteAddress?.displayName ?? "",
                        start: call.callLog.map { Date(timeIntervalSince1970: TimeInterval($0.startDate)) } ?? Date(),
                        duration: call.callLog?.duration ?? 0)
                default: break
                }
            }
        }
        SIPManager.shared.start()
        LocalPushController.shared.load()
        MainActor.assumeIsolated {
            // Mitteilungen „gehalten – übernehmen“ (normaler Push, Token an cme-web)
            HeldCallNotifications.shared.setup(application)
            // Anmeldestatus in der Einstellungen-App anzeigen
            SIPManager.shared.$registration
                .removeDuplicates()
                .sink { PhoneSettings.shared.publishStatus($0.label) }
                .store(in: &cancellables)
            // Änderungen aus der Einstellungen-App übernehmen
            NotificationCenter.default.publisher(for: UserDefaults.didChangeNotification)
                .debounce(for: .milliseconds(400), scheduler: RunLoop.main)
                .sink { _ in MainActor.assumeIsolated { Self.settingsChanged() } }
                .store(in: &cancellables)
            Provisioning.refresh()
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        MainActor.assumeIsolated { HeldCallNotifications.shared.didRegister(token: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        MainActor.assumeIsolated { SIPManager.shared.log("Mitteilungen: kein Push-Token (\(error.localizedDescription))") }
    }

    /// Stiller Push "heldEnd": Gespräch läuft wieder bzw. ist beendet – Mitteilung entfernen.
    func application(_ application: UIApplication, didReceiveRemoteNotification userInfo: [AnyHashable: Any],
                     fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
        let handled = MainActor.assumeIsolated { HeldCallNotifications.shared.handleBackground(userInfo) }
        completionHandler(handled ? .newData : .noData)
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        MainActor.assumeIsolated {
            Self.settingsChanged()
            Provisioning.refresh()
        }
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        SIPManager.shared.core?.ensureRegistered()
    }

    @MainActor private static var lastBundle = ""

    @MainActor
    private static func settingsChanged() {
        let s = PhoneSettings.shared
        let fp = s.settingsBundleFingerprint
        guard fp != lastBundle else { return }
        lastBundle = fp
        let before = (s.cmeWebURL, s.appToken)
        if s.readSettingsBundle() {
            SIPManager.shared.applyMediaSettings()
            SIPManager.shared.applyAccount()
        }
        if (s.cmeWebURL, s.appToken) != before { Provisioning.refresh() }
        LocalPushController.shared.apply()
    }
}
