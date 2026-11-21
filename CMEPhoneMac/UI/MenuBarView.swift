import SwiftUI

/// Kompakte Bedienung aus der Menüleiste: Status, Schnellwahl, Tischtelefon-Knöpfe.
struct MenuBarView: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @Environment(\.openWindow) private var openWindow
    @State private var number = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("CME Phone").font(.headline)
                Spacer()
                MacRegistrationBadge(status: sip.registration)
            }

            Picker("", selection: $settings.macMode) {
                Text("Softphone").tag(MacMode.softphone)
                Text("Tischtelefon").tag(MacMode.deskPhone)
            }
            .pickerStyle(.segmented).labelsHidden()

            if settings.macMode == .deskPhone {
                HStack {
                    Image(systemName: "phone")
                    Text("\(settings.deskPhoneExtension): \(desk.status.state.label)")
                    if let n = desk.status.remoteNumber { Text("· \(n)").foregroundStyle(.secondary) }
                }.font(.callout)
                HStack {
                    Button("Annehmen") { desk.answer() }
                    Button("Auflegen") { desk.hangup() }
                    Button("Halten") { desk.hold() }
                }
            } else if let call = sip.calls.first {
                HStack {
                    Text(call.displayName.isEmpty ? call.remote : call.displayName)
                    Spacer()
                    if call.isIncoming && call.state == .IncomingReceived {
                        Button("Annehmen") { CallCenter.shared.requestAnswer(callId: call.id) }
                    }
                    Button("Auflegen") { CallCenter.shared.requestEnd(callId: call.id) }
                }
            }

            HStack {
                TextField("Nummer wählen", text: $number)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(dial)
                Button(action: dial) { Image(systemName: "phone.fill") }.disabled(number.isEmpty)
            }

            Divider()
            HStack {
                Button("Fenster öffnen") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
                SettingsLink { Text("Einstellungen …") }
                Spacer()
                Button("Beenden") { NSApp.terminate(nil) }
            }
        }
        .padding(12)
        .frame(width: 320)
    }

    private func dial() {
        let n = number.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        URLRouter.dial(n)
        number = ""
    }
}
