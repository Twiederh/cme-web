import SwiftUI
import UIKit

/// Die App hat keine eigene Telefon-Oberfläche: Anrufe laufen über die Telefon-App (CallKit,
/// „Zuletzt“, Kontakte, Siri, tel:-Links), eingerichtet wird in der Einstellungen-App.
/// Öffnet man die App trotzdem (z.B. über das App-Symbol im Anrufbildschirm), zeigt sie nur
/// den Status und während eines Gesprächs den Aufnahme-Knopf.
struct ContentView: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @ObservedObject private var rec = RecordingController.shared
    @State private var showLog = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 28) {
                Spacer()
                Image(systemName: "phone.circle.fill")
                    .font(.system(size: 72))
                    .foregroundStyle(.green)
                VStack(spacing: 6) {
                    Text(settings.extensionNumber.isEmpty ? "CME Phone" : "Nebenstelle \(settings.extensionNumber)")
                        .font(.title2.bold())
                    RegistrationBadge(status: sip.registration)
                    if !settings.provisioningStatus.isEmpty {
                        Text(settings.provisioningStatus).font(.caption).foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                }

                if let call = sip.calls.first {
                    VStack(spacing: 14) {
                        Text(call.displayName.isEmpty ? call.remote : call.displayName).font(.headline)
                        if call.isPaused {
                            Text("Gehalten – an einem anderen Gerät mit „Fortsetzen“ übernehmen")
                                .font(.caption).foregroundStyle(.orange).multilineTextAlignment(.center)
                        }
                        HStack(spacing: 28) {
                            RoundButton(symbol: rec.isOn ? "record.circle.fill" : "record.circle",
                                        color: rec.isOn ? .red : .gray, label: rec.isOn ? "Aufnahme läuft" : "Aufnehmen",
                                        enabled: rec.available && !rec.busy) { rec.toggle() }
                            RoundButton(symbol: call.isPaused ? "play.fill" : "pause.fill",
                                        color: call.isPaused ? .orange : .gray,
                                        label: call.isPaused ? "Fortsetzen" : "Halten", enabled: true) {
                                CallKitProvider.shared.requestHold(callId: call.id, held: !call.isPaused)
                            }
                            RoundButton(symbol: "phone.down.fill", color: .red, label: "Auflegen", enabled: true) {
                                CallKitProvider.shared.requestEnd(callId: call.id)
                            }
                        }
                        if !rec.available { Text(rec.help).font(.caption).foregroundStyle(.secondary) }
                    }
                    .padding()
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
                } else {
                    Text("Telefoniert wird über die Telefon-App: „Zuletzt“, Kontakte, Siri oder Links mit Telefonnummern. "
                         + "Eingehende Anrufe klingeln wie normale Anrufe.")
                        .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }

                Button {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                } label: {
                    Label("Einstellungen öffnen", systemImage: "gearshape")
                }
                .buttonStyle(.bordered)
                Spacer()
                // Build-Nummer, Git-Hash ("+" = mit uncommitteten Änderungen) und Bauzeit, z.B. für Tests
                Text(BuildInfo.text).font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
            }
            .padding(24)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showLog = true } label: { Image(systemName: "text.alignleft") }
                }
            }
            .sheet(isPresented: $showLog) { LogView() }
        }
        .onChange(of: sip.calls.first?.id) { _, id in
            if let id { rec.track(ext: settings.extensionNumber, localCallId: id) } else { rec.stop() }
        }
    }
}

private struct RoundButton: View {
    let symbol: String, color: Color, label: String, enabled: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(spacing: 6) {
                Image(systemName: symbol).font(.title2).foregroundStyle(.white)
                    .frame(width: 64, height: 64).background(color, in: Circle())
                Text(label).font(.caption).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
    }
}

struct RegistrationBadge: View {
    let status: SIPManager.RegistrationStatus
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(status.label).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }
    private var color: Color {
        switch status {
        case .ok: return .green
        case .progress: return .orange
        case .failed: return .red
        default: return .gray
        }
    }
}

struct LogView: View {
    @EnvironmentObject private var sip: SIPManager
    var body: some View {
        NavigationStack {
            List(Array(sip.lastLog.enumerated().reversed()), id: \.offset) { _, line in
                Text(line).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
            }
            .navigationTitle("Protokoll")
            .toolbar {
                ShareLink(item: sip.lastLog.joined(separator: "\n")) { Image(systemName: "square.and.arrow.up") }
            }
        }
    }
}
