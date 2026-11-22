import SwiftUI
import UIKit

@main
struct CMEPhoneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(SIPManager.shared)
                .environmentObject(PhoneSettings.shared)
                .environmentObject(LocalPushController.shared)
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Reihenfolge wichtig: CallKit-Provider und Local-Push-Delegate müssen stehen,
        // bevor iOS einen per Push gestarteten Anruf zustellt.
        _ = CallKitProvider.shared
        SIPManager.shared.start()
        LocalPushController.shared.load()
        return true
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        SIPManager.shared.core?.ensureRegistered()
    }
}
