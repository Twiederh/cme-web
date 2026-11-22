import SwiftUI

/// Fernbedienung für das Cisco-Tischtelefon.
struct DeskPhoneView: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @State private var digits = ""
    @State private var rawURI = ""

    var body: some View {
        Form {
            Section("Status") {
                LabeledContent("Telefon", value: settings.deskPhoneHost.isEmpty ? "nicht eingestellt" : settings.deskPhoneHost)
                LabeledContent("Nebenstelle", value: settings.deskPhoneExtension.isEmpty ? "–" : settings.deskPhoneExtension)
                LabeledContent("Leitung") {
                    HStack {
                        Circle().fill(color).frame(width: 10, height: 10)
                        Text(desk.status.state.label)
                    }
                }
                if let n = desk.status.remoteNumber {
                    LabeledContent("Gegenstelle", value: [desk.status.remoteName, n].compactMap { $0 }.joined(separator: " · "))
                }
                LabeledContent("Statusabo", value: desk.subscriptionState)
            }

            Section("Steuerung") {
                HStack {
                    Button { desk.answer() } label: { Label("Annehmen", systemImage: "phone.fill") }
                        .tint(.green).buttonStyle(.borderedProminent)
                    Button { desk.hangup() } label: { Label("Auflegen", systemImage: "phone.down.fill") }
                        .tint(.red).buttonStyle(.borderedProminent)
                    Button { desk.decline() } label: { Label("Ablehnen", systemImage: "xmark") }
                    Button { desk.hold() } label: { Label("Halten", systemImage: "pause.fill") }
                }
                .disabled(desk.busy || settings.deskPhoneHost.isEmpty)

                HStack {
                    TextField("Ziffern senden (DTMF), z.B. 1234#", text: $digits)
                        .onSubmit(sendDigits)
                    Button("Senden", action: sendDigits).disabled(digits.isEmpty)
                }
            }

            Section {
                HStack {
                    TextField("Execute-URI, z.B. Key:Soft2 oder Key:Line1", text: $rawURI)
                        .onSubmit { if !rawURI.isEmpty { desk.execute(rawURI) } }
                    Button("Ausführen") { desk.execute(rawURI) }.disabled(rawURI.isEmpty)
                }
                Button("Statusabo neu starten") { desk.startMonitoring() }
                if !desk.lastResult.isEmpty {
                    Text(desk.lastResult).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            } header: { Text("Experte") } footer: {
                Text("Welche Tasten-URIs wirken, hängt vom Telefonmodell und der Firmware ab. Funktionierende Werte unter Einstellungen → Tischtelefon den Knöpfen zuordnen.")
            }
        }
        .formStyle(.grouped)
    }

    private var color: Color {
        switch desk.status.state {
        case .idle: return .green
        case .ringing: return .orange
        case .busy, .dialing: return .red
        case .unknown: return .gray
        }
    }

    private func sendDigits() {
        guard !digits.isEmpty else { return }
        desk.sendDigits(digits)
        digits = ""
    }
}
