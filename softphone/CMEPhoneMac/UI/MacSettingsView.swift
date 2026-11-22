import SwiftUI
import AppKit
import linphonesw

struct MacSettingsView: View {
    var body: some View {
        TabView {
            AccountTab().tabItem { Label("Konto", systemImage: "person.crop.circle") }
            GeneralTab().tabItem { Label("Anrufe", systemImage: "phone.bubble") }
            MediaTab().tabItem { Label("Audio", systemImage: "waveform") }
            DeskPhoneTab().tabItem { Label("Tischtelefon", systemImage: "phone") }
            LogTab().tabItem { Label("Protokoll", systemImage: "text.alignleft") }
        }
        .frame(width: 580, height: 560)
        // Menüleisten-App: Fenster nach vorne holen
        .onAppear { NSApp.activate(ignoringOtherApps: true) }
    }
}

private struct AccountTab: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var sip: SIPManager
    @State private var manual = false

    var body: some View {
        Form {
            Section {
                TextField("cme-web-Adresse", text: $settings.cmeWebURL, prompt: Text("https://cme.wiederhol.de"))
                SecureField("App-Schlüssel", text: $settings.appToken)
                TextField("Nebenstelle des Macs", text: $settings.extensionNumber)
                TextField("Anzeigename", text: $settings.displayName)
                HStack {
                    Text(settings.provisioningStatus.isEmpty ? "Noch nicht abgerufen" : settings.provisioningStatus)
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Von cme-web holen und anmelden") {
                        Provisioning.refresh()
                        sip.applyAccount()
                    }
                    .disabled(!settings.hasCmeWeb || settings.extensionNumber.isEmpty)
                }
            } header: { Text("cme-web") } footer: {
                Text("cme-web → Tab Softphone: Adresse und App-Schlüssel. In cme-web den Mac als Telefon vom Typ „CME Phone“ mit Gerätename \(settings.deviceName) anlegen. "
                     + "Gateway, Anlage und Voicemail-Nummer kommen dann automatisch. Auch bei Telefonsteuerung braucht der Mac eine eigene Nebenstelle – darüber läuft das Statusabo.")
            }
            Section("Status") {
                HStack {
                    MacRegistrationBadge(status: sip.registration)
                    Spacer()
                    Text(settings.useGateway ? "über \(settings.gatewayHost):\(String(settings.gatewayPort))" : "direkt an \(settings.cmeHost)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !settings.voicemailNumber.isEmpty {
                    LabeledContent("Voicemail", value: settings.voicemailNumber)
                }
            }
            DisclosureGroup("Ohne cme-web / manuell", isExpanded: $manual) {
                TextField("CME-Adresse", text: $settings.cmeHost)
                Toggle("Über Push-Gateway (Flexisip) statt direkt an CME", isOn: $settings.useGateway)
                if settings.useGateway {
                    TextField("Gateway", text: $settings.gatewayHost)
                    Stepper("Port \(String(settings.gatewayPort))", value: $settings.gatewayPort, in: 1...65535)
                }
                Picker("Transport", selection: $settings.transport) {
                    ForEach(SIPTransport.allCases) { Text($0.rawValue.uppercased()).tag($0) }
                }
                SecureField("Passwort (nur falls CME authentifiziert)", text: $settings.password)
                Button("Übernehmen und neu anmelden") {
                    sip.applyMediaSettings()
                    sip.applyAccount()
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct GeneralTab: View {
    @EnvironmentObject private var settings: PhoneSettings

    var body: some View {
        Form {
            Section {
                Toggle("Rückruf: 0 vor externe Nummern (mehr als 4 Stellen)", isOn: $settings.callbackPrefixZero)
                LabeledContent("Beispiel") {
                    Text("07111234567 → \(DialRule.callback("07111234567", prefixZero: settings.callbackPrefixZero)) · "
                         + "+49711… bleibt · 21 bleibt")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } header: { Text("Wählregel") } footer: {
                Text("Gilt für Rückrufe aus der Anrufliste. Nummern mit + wählt CME selbst um (E.164).")
            }
            Section {
                Toggle("Interne Gespräche lokal aufnehmen (nur Softphone)", isOn: $settings.localRecording)
                HStack {
                    Text("Ordner: Musik/CME Phone").foregroundStyle(.secondary)
                    Spacer()
                    Button("Im Finder zeigen") { NSWorkspace.shared.open(SIPManager.recordingsFolder) }
                }
            } header: { Text("Aufnahme") } footer: {
                Text("Externe Gespräche nimmt der cme-recorder auf (cme-web → Aufzeichnung) – der Aufnahme-Knopf im Call-Toast schaltet ihn ein und aus, auch bei Telefonsteuerung. "
                     + "Bitte vorher die Zustimmung aller Beteiligten einholen (§ 201 StGB).")
            }
            Section("Wähltastatur") {
                Picker("Tasten", selection: $settings.dialpadOutlined) {
                    Text("Gefüllt").tag(false)
                    Text("Umrandet").tag(true)
                }
                .pickerStyle(.segmented)
            }
            Section("Call-Toast") {
                Toggle("Anrufe am Tischtelefon anzeigen (Telefonsteuerung)", isOn: $settings.deskPhoneNotify)
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
                List {
                    ForEach($settings.audioCodecs) { $c in Toggle(c.displayName, isOn: $c.enabled) }
                        .onMove { settings.audioCodecs.move(fromOffsets: $0, toOffset: $1) }
                }
                .frame(minHeight: CGFloat(settings.audioCodecs.count) * 28)
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

private struct DeskPhoneTab: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @State private var rawURI = ""

    var body: some View {
        Form {
            Section {
                TextField("IP / Hostname des Telefons", text: $settings.deskPhoneHost)
                TextField("Nebenstelle des Telefons", text: $settings.deskPhoneExtension)
                TextField("Benutzer (authentication credential)", text: $settings.deskPhoneUser)
                SecureField("Passwort", text: $settings.deskPhonePassword)
                Stepper("Leitung \(settings.deskPhoneLine)", value: $settings.deskPhoneLine, in: 1...16)
            } header: { Text("Cisco-Telefon") } footer: {
                Text("Steuerung über Cisco XML (POST /CGI/Execute). CME prüft die Zugangsdaten über „url authentication“ – siehe softphone/cme/deskphone-control.cfg.")
            }
            Section {
                TextField("Annehmen", text: $settings.keyAnswer)
                TextField("Auflegen", text: $settings.keyHangup)
                TextField("Ablehnen", text: $settings.keyDecline)
                TextField("An Voicemail (z.B. iDivert)", text: $settings.keyVoicemail)
                TextField("Halten", text: $settings.keyHold)
            } header: { Text("Tasten-URIs") } footer: {
                Text("Üblich: Key:Speaker, Key:Headset, Key:Hold, Key:Soft1…Soft4. Welche wirken, hängt von Modell und Firmware ab – unten ausprobieren.")
            }
            Section("Status") {
                LabeledContent("Leitung", value: desk.status.state.label)
                LabeledContent("Statusabo", value: desk.subscriptionState)
                Picker("Event-Paket", selection: $settings.statusEvent) {
                    Text("dialog (RFC 4235)").tag(StatusEventPackage.dialog)
                    Text("presence (PIDF)").tag(StatusEventPackage.presence)
                }
                Button("Statusabo neu starten") { desk.startMonitoring() }
            }
            Section("Ausprobieren") {
                HStack {
                    TextField("Execute-URI, z.B. Key:Soft2", text: $rawURI)
                        .onSubmit { if !rawURI.isEmpty { desk.execute(rawURI) } }
                    Button("Ausführen") { desk.execute(rawURI) }.disabled(rawURI.isEmpty)
                }
                Button("Testwahl: Tischtelefon ruft diesen Mac an") { desk.dial(settings.extensionNumber) }
                    .disabled(settings.extensionNumber.isEmpty || settings.deskPhoneHost.isEmpty)
                if !desk.lastResult.isEmpty {
                    Text(desk.lastResult).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct LogTab: View {
    @EnvironmentObject private var sip: SIPManager
    var body: some View {
        VStack(alignment: .trailing) {
            ScrollView {
                Text(sip.lastLog.reversed().joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            Button("Kopieren") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(sip.lastLog.joined(separator: "\n"), forType: .string)
            }
            .padding(8)
        }
    }
}

struct MacRegistrationBadge: View {
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
