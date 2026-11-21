import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var localPush: LocalPushController

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Nebenstelle (z.B. 21)", text: $settings.extensionNumber).keyboardType(.numberPad)
                    TextField("Anzeigename", text: $settings.displayName)
                    TextField("CME-Adresse", text: $settings.cmeHost)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Passwort (nur falls CME authentifiziert)", text: $settings.password)
                } header: { Text("Cisco CME") } footer: {
                    RegistrationBadge(status: sip.registration)
                }

                Section {
                    Toggle("Über Push-Gateway (Flexisip)", isOn: $settings.useGateway)
                    if settings.useGateway {
                        TextField("Gateway-Host", text: $settings.gatewayHost)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Stepper("Port \(settings.gatewayPort)", value: $settings.gatewayPort, in: 1...65535)
                    }
                    Picker("Transport", selection: $settings.transport) {
                        ForEach(SIPTransport.allCases) { Text($0.rawValue.uppercased()).tag($0) }
                    }
                } header: { Text("Verbindung") } footer: {
                    Text("Ohne Gateway registriert sich die App direkt an CME – dann klingelt sie nur, solange sie im Vordergrund ist.")
                }

                Section {
                    Toggle("Local Push im Heim-WLAN", isOn: $settings.localPushEnabled)
                    if settings.localPushEnabled {
                        TextField("SSIDs (Komma-getrennt)", text: $settings.localPushSSIDs)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        TextField("Relay-Host", text: $settings.relayHost)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Stepper("Relay-Port \(settings.relayPort)", value: $settings.relayPort, in: 1...65535)
                        SecureField("Relay-Secret", text: $settings.relaySecret)
                    }
                } header: { Text("Eingehende Anrufe") } footer: {
                    Text("Status: \(localPush.status)\(sip.voipToken.map { "\nPush-Token: \($0.prefix(12))…" } ?? "")")
                }

                Section {
                    CodecList(codecs: $settings.audioCodecs)
                } header: { Text("Audio-Codecs (Reihenfolge = Priorität)") } footer: {
                    Text("CME kann Opus nicht umkodieren. Opus kommt nur zustande, wenn die Gegenstelle es ebenfalls anbietet (z.B. ein zweites Softphone).")
                }

                Section {
                    Toggle("Video", isOn: $settings.videoEnabled)
                    if settings.videoEnabled { CodecList(codecs: $settings.videoCodecs) }
                } header: { Text("Video") } footer: {
                    Text("Cisco 8845/8865 sprechen H.264. H.265 nur zwischen Softphones.")
                }

                Section("Sicherheit") {
                    Picker("Medienverschlüsselung", selection: $settings.mediaEncryption) {
                        ForEach(MediaEncryptionSetting.allCases) { Text($0.label).tag($0) }
                    }
                }

                Section {
                    Button("Übernehmen und neu anmelden") {
                        sip.applyMediaSettings()
                        sip.applyAccount()
                        localPush.apply()
                    }
                    Button("Codecs zurücksetzen", role: .destructive) {
                        settings.audioCodecs = PhoneSettings.defaultAudio
                        settings.videoCodecs = PhoneSettings.defaultVideo
                    }
                }
            }
            .navigationTitle("Einstellungen")
            .toolbar { EditButton() }
        }
    }
}

private struct CodecList: View {
    @Binding var codecs: [CodecEntry]
    var body: some View {
        ForEach($codecs) { $codec in
            Toggle(codec.displayName, isOn: $codec.enabled)
        }
        .onMove { from, to in codecs.move(fromOffsets: from, toOffset: to) }
    }
}
