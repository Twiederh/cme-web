import SwiftUI
import AppKit

/// Inhalt des Menüleisten-Menüs.
struct MenuBarContent: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @EnvironmentObject private var history: CallHistory

    var body: some View {
        Text(statusLine)

        Divider()

        // Umschalten zwischen den Betriebsarten (Häkchen am aktiven Modus)
        Picker("Modus", selection: $settings.macMode) {
            Text("Softphone").tag(MacMode.softphone)
            Text("Telefonsteuerung").tag(MacMode.deskPhone)
        }
        .pickerStyle(.inline)
        .labelsHidden()

        Divider()

        Menu("Anrufliste") {
            if let last = history.lastDialed {
                Button("Wahlwiederholung: \(last.title)") { URLRouter.dial(last.number) }
                    .keyboardShortcut("r")
                Divider()
            }
            if history.entries.isEmpty {
                Text("Keine Anrufe")
            } else {
                ForEach(history.entries) { e in
                    Button {
                        callBack(e)
                    } label: {
                        Label("\(e.title)   \(Self.when(e.date))\(e.viaDeskPhone ? " · Tischtelefon" : "")",
                              systemImage: e.symbol)
                    }
                    .help("Rückruf: \(DialRule.callback(e.number, prefixZero: settings.callbackPrefixZero))")
                }
            }
            Divider()
            Toggle("Rückruf mit 0 für externe Nummern (mehr als 4 Stellen)", isOn: $settings.callbackPrefixZero)
            Button("Liste leeren") { history.clear() }.disabled(history.entries.isEmpty)
        }

        Button("Wähltastatur …") { DialpadPanel.shared.show() }
            .keyboardShortcut("d")

        Button("Einstellungen …") { SettingsWindow.shared.show() }
            .keyboardShortcut(",")

        Divider()

        Button("CME Phone beenden") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func callBack(_ e: CallHistory.Entry) {
        // Gewählte Nummern unverändert wiederholen, Anrufer mit Wählregel zurückrufen
        let number = e.direction == .outgoing ? e.number : DialRule.callback(e.number, prefixZero: settings.callbackPrefixZero)
        URLRouter.dial(number)
    }

    private var statusLine: String {
        switch settings.macMode {
        case .softphone:
            return "Softphone \(settings.extensionNumber) – \(sip.registration.label)"
        case .deskPhone:
            let ext = settings.deskPhoneExtension.isEmpty ? "?" : settings.deskPhoneExtension
            if case .ok = sip.registration { return "Tischtelefon \(ext) – \(desk.status.state.label)" }
            return "Tischtelefon \(ext) – \(sip.registration.label)"
        }
    }

    private static func when(_ d: Date) -> String {
        if Calendar.current.isDateInToday(d) { return d.formatted(date: .omitted, time: .shortened) }
        if Calendar.current.isDateInYesterday(d) { return "gestern " + d.formatted(date: .omitted, time: .shortened) }
        return d.formatted(.dateTime.day().month(.twoDigits).hour().minute())
    }
}
