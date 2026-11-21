import SwiftUI
import linphonesw

struct MacSettingsView: View {
    var body: some View {
        TabView {
            AccountTab().tabItem { Label("Konto", systemImage: "person.crop.circle") }
            MediaTab().tabItem { Label("Audio & Video", systemImage: "waveform") }
            DeskPhoneTab().tabItem { Label("Tischtelefon", systemImage: "phone") }
        }
        .frame(width: 560, height: 520)
    }
}

private struct AccountTab: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var sip: SIPManager

    var body: some View {
        Form {
            Section {
                TextField("Nebenstelle des Macs", text: $settings.extensionNumber)
                TextField("Anzeigename", text: $settings.displayName)
                TextField("CME-Adresse", text: $settings.cmeHost)
                SecureField("Passwort (nur falls CME authentifiziert)", text: $settings.password)
            } header: { Text("Cisco CME") } footer: {
                Text("Auch im Tischtelefon-Modus meldet sich der Mac mit einer eigenen Nebenstelle an – darüber läuft das Statusabo.")
            }
            Section("Verbindung") {
                Toggle("Über Flexisip-Gateway statt direkt an CME", isOn: $settings.useGateway)
                if settings.useGateway {
                    TextField("Gateway-Host", text: $settings.gatewayHost)
                    Stepper("Port \(settings.gatewayPort)", value: $settings.gatewayPort, in: 1...65535)
                }
                Picker("Transport", selection: $settings.transport) {
                    ForEach(SIPTransport.allCases) { Text($0.rawValue.uppercased()).tag($0) }
                }
                Picker("Medienverschlüsselung", selection: $settings.mediaEncryption) {
                    ForEach(MediaEncryptionSetting.allCases) { Text($0.label).tag($0) }
                }
            }
            Section {
                HStack {
                    MacRegistrationBadge(status: sip.registration)
                    Spacer()
                    Button("Übernehmen und neu anmelden") {
                        sip.applyMediaSettings()
                        sip.applyAccount()
                    }
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct MediaTab: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var sip: SIPManager
    @State private var inputId = ""
    @State private var outputId = ""

    var body: some View {
        Form {
            Section("Audiogeräte") {
                Picker("Mikrofon", selection: $inputId) {
                    ForEach(sip.audioDevices.filter { $0.hasCapability(capability: .CapabilityRecord) }, id: \.id) {
                        Text($0.deviceName).tag($0.id)
                    }
                }
                Picker("Lautsprecher / Headset", selection: $outputId) {
                    ForEach(sip.audioDevices.filter { $0.hasCapability(capability: .CapabilityPlay) }, id: \.id) {
                        Text($0.deviceName).tag($0.id)
                    }
                }
                Button("Geräte übernehmen") {
                    sip.selectAudio(input: sip.audioDevices.first { $0.id == inputId },
                                    output: sip.audioDevices.first { $0.id == outputId })
                }
            }
            Section("Audio-Codecs (ziehen = Priorität)") {
                CodecRows(codecs: $settings.audioCodecs)
            }
            Section("Video") {
                Toggle("Video", isOn: $settings.videoEnabled)
                if settings.videoEnabled { CodecRows(codecs: $settings.videoCodecs) }
            }
            Section {
                Button("Übernehmen") { sip.applyMediaSettings() }
            }
        }
        .formStyle(.grouped)
        .onAppear {
            inputId = sip.core?.inputAudioDevice?.id ?? ""
            outputId = sip.core?.outputAudioDevice?.id ?? ""
        }
    }
}

private struct CodecRows: View {
    @Binding var codecs: [CodecEntry]
    var body: some View {
        List {
            ForEach($codecs) { $c in Toggle(c.displayName, isOn: $c.enabled) }
                .onMove { codecs.move(fromOffsets: $0, toOffset: $1) }
        }
        .frame(minHeight: CGFloat(codecs.count) * 28)
    }
}

private struct DeskPhoneTab: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController

    var body: some View {
        Form {
            Section {
                TextField("IP / Hostname des Telefons", text: $settings.deskPhoneHost)
                TextField("Nebenstelle des Telefons", text: $settings.deskPhoneExtension)
                TextField("Benutzer (authentication credential)", text: $settings.deskPhoneUser)
                SecureField("Passwort", text: $settings.deskPhonePassword)
                Stepper("Leitung \(settings.deskPhoneLine)", value: $settings.deskPhoneLine, in: 1...16)
            } header: { Text("Cisco-Telefon") } footer: {
                Text("Steuerung über Cisco XML (POST /CGI/Execute). CME prüft die Zugangsdaten über \"url authentication\" – siehe cme/deskphone-control.cfg.")
            }
            Section {
                TextField("Annehmen", text: $settings.keyAnswer)
                TextField("Auflegen", text: $settings.keyHangup)
                TextField("Ablehnen", text: $settings.keyDecline)
                TextField("Halten", text: $settings.keyHold)
            } header: { Text("Tasten-URIs") } footer: {
                Text("Üblich: Key:Speaker, Key:Headset, Key:Hold, Key:Soft1…Soft4. Im Tischtelefon-Bereich unter \"Experte\" ausprobieren.")
            }
            Section("Status") {
                Picker("Event-Paket", selection: $settings.statusEvent) {
                    Text("dialog (RFC 4235)").tag(StatusEventPackage.dialog)
                    Text("presence (PIDF)").tag(StatusEventPackage.presence)
                }
                Toggle("Mitteilung bei Anruf am Tischtelefon", isOn: $settings.deskPhoneNotify)
                Button("Statusabo neu starten") { desk.startMonitoring() }
            }
            Section {
                Button("Testwahl: Tischtelefon ruft diesen Mac an") { desk.dial(settings.extensionNumber) }
                    .disabled(settings.extensionNumber.isEmpty || settings.deskPhoneHost.isEmpty)
                if !desk.lastResult.isEmpty { Text(desk.lastResult).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .formStyle(.grouped)
    }
}
