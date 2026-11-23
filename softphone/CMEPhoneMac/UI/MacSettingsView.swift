import SwiftUI
import AppKit
import linphonesw

struct MacSettingsView: View {
    /// Seiten als Seitenleiste im Fenster (TabView landete in der Titelleiste hinter »)
    private enum Page: String, CaseIterable, Identifiable {
        case account = "Konto", calls = "Anrufe", audio = "Audio", desk = "Tischtelefon", log = "Protokoll"
        var id: String { rawValue }
        var symbol: String {
            switch self {
            case .account: return "person.crop.circle"
            case .calls: return "phone.bubble"
            case .audio: return "waveform"
            case .desk: return "phone"
            case .log: return "text.alignleft"
            }
        }
    }
    @State private var page: Page = .account

    var body: some View {
        HStack(spacing: 0) {
            List(Page.allCases, selection: $page) { p in
                Label(p.rawValue, systemImage: p.symbol).tag(p)
            }
            .listStyle(.sidebar)
            .frame(width: 170)
            Divider()
            Group {
                switch page {
                case .account: AccountTab()
                case .calls: GeneralTab()
                case .audio: MediaTab()
                case .desk: DeskPhoneTab()
                case .log: LogTab()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: 740, height: 580)
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
                TextField("cme-web-Adresse", text: $settings.cmeWebURL, prompt: Text("https://…"))
                SecureField("App-Schlüssel", text: $settings.appToken)
                TextField("Nebenstelle des Macs", text: $settings.extensionNumber)
                TextField("Anzeigename", text: $settings.displayName)
                HStack {
                    Text(settings.provisioningStatus.isEmpty ? "Noch nicht abgerufen" : settings.provisioningStatus)
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Von cme-web holen und anmelden") {
                        settings.cmeWebURL = settings.cmeWebURL.trimmingCharacters(in: .whitespacesAndNewlines)
                        settings.appToken = settings.appToken.trimmingCharacters(in: .whitespacesAndNewlines)
                        settings.extensionNumber = settings.extensionNumber.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard settings.hasCmeWeb, !settings.extensionNumber.isEmpty else {
                            settings.provisioningStatus = "Bitte cme-web-Adresse, App-Schlüssel und Nebenstelle eintragen"
                            return
                        }
                        Provisioning.refresh()
                        sip.applyAccount()
                    }
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
    @State private var devices: [(id: String, name: String, record: Bool, play: Bool)] = []

    var body: some View {
        Form {
            Section {
                Picker("Mikrofon", selection: $settings.audioInputId) {
                    Text("Systemstandard").tag("")
                    ForEach(devices.filter(\.record), id: \.id) { Text($0.name).tag($0.id) }
                }
                Picker("Lautsprecher / Headset", selection: $settings.audioOutputId) {
                    Text("Systemstandard").tag("")
                    ForEach(devices.filter(\.play), id: \.id) { Text($0.name).tag($0.id) }
                }
                Button("Liste aktualisieren") { reload() }
            } header: { Text("Audiogeräte") } footer: {
                Text("Die Auswahl gilt sofort und bleibt gespeichert. Klingelton und Gespräch laufen über das gewählte Ausgabegerät.")
            }
            Section("Video") {
                Toggle("Video (Kamera und Bild im Call-Toast)", isOn: $settings.videoEnabled)
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
        .onAppear(perform: reload)
        .onChange(of: settings.audioInputId) { _, _ in sip.applySavedAudioDevices() }
        .onChange(of: settings.audioOutputId) { _, _ in sip.applySavedAudioDevices() }
        .onChange(of: settings.videoEnabled) { _, _ in sip.applyMediaSettings() }
    }

    /// "Studio Display Microphone (AppleUSBAudioEngine:…)" -> "Studio Display Microphone",
    /// "Default Capture" -> "Standard-Mikrofon"
    static func cleanName(_ n: String) -> String {
        var s = n
        if let r = s.range(of: " (", options: .backwards), s.hasSuffix(")") { s = String(s[..<r.lowerBound]) }
        switch s {
        case "Default Capture": return "Standard-Mikrofon (System)"
        case "Default Playback": return "Standard-Ausgabe (System)"
        default: return s
        }
    }

    private func reload() {
        sip.core?.reloadSoundDevices()
        var seen = Set<String>()
        devices = sip.audioDevices.compactMap { d in
            guard seen.insert(d.id).inserted else { return nil }
            return (d.id, Self.cleanName(d.deviceName), d.hasCapability(capability: .CapabilityRecord),
                    d.hasCapability(capability: .CapabilityPlay))
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
                TextField("Benutzer", text: $settings.deskPhoneUser)
                SecureField("Passwort", text: $settings.deskPhonePassword)
                Stepper("Leitung \(settings.deskPhoneLine)", value: $settings.deskPhoneLine, in: 1...16)
            } header: { Text("Cisco-Telefon") } footer: {
                Text("Steuerung über Cisco XML (POST /CGI/Execute). Das Telefon prüft Benutzer und Passwort über die Authentication URL von cme-web – beides kommt automatisch aus cme-web (Tab Softphone → Tischtelefon-Steuerung).")
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
