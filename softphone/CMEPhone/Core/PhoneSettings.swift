import Foundation
import Combine

/// Ein Codec-Eintrag in der Prioritätsliste (Reihenfolge = SDP-Angebotsreihenfolge).
struct CodecEntry: Codable, Identifiable, Hashable {
    /// MIME-Typ wie in liblinphone (`PayloadType.mimeType`), z.B. "PCMA", "G722", "opus", "H264", "H265".
    var mime: String
    var enabled: Bool
    var id: String { mime }

    var displayName: String {
        switch mime.uppercased() {
        case "PCMA": return "G.711 A-law"
        case "PCMU": return "G.711 µ-law"
        case "G722": return "G.722"
        case "OPUS": return "Opus"
        case "H264": return "H.264"
        case "H265": return "H.265 / HEVC"
        default: return mime
        }
    }
}

enum SIPTransport: String, Codable, CaseIterable, Identifiable {
    case udp, tcp, tls
    var id: String { rawValue }
}

/// Betriebsart der Mac-App.
enum MacMode: String, Codable, CaseIterable, Identifiable {
    /// Der Mac telefoniert selbst (liblinphone, eigenes Headset/Mikrofon).
    case softphone
    /// Der Mac steuert ein Cisco-SIP-Tischtelefon (XML Execute + Statusabo).
    case deskPhone
    var id: String { rawValue }
    var label: String { self == .softphone ? "Softphone (Mac telefoniert)" : "Tischtelefon steuern" }
}

enum MediaEncryptionSetting: String, Codable, CaseIterable, Identifiable {
    case none, srtp
    var id: String { rawValue }
    var label: String { self == .none ? "Aus (RTP)" : "SRTP (SDES, nur mit TLS)" }
}

/// Persistente Einstellungen.
/// iOS: UserDefaults der App – die werden in der Einstellungen-App bearbeitet (Settings.bundle),
///      die App hat keine eigene Einstellungsseite.
/// macOS: App-Group-UserDefaults (Einstellungsfenster der Menüleisten-App).
/// Gateway, CME, Local Push und Voicemail-Nummer holt die App aus cme-web (`Provisioning`),
/// wenn cme-web-Adresse und App-Schlüssel eingetragen sind.
final class PhoneSettings: ObservableObject {
    static let shared = PhoneSettings()
    #if os(iOS)
    private let defaults = UserDefaults.standard
    #else
    // macOS: eigene UserDefaults der App (Sandbox-Container). Die App-Group brauchte der Mac nie (keine
    // Extension), und je nach Signierprofil gingen Schreibzugriffe dort verloren.
    private let defaults: UserDefaults = {
        let std = UserDefaults.standard
        if std.object(forKey: "settingsMigrated") == nil {
            if let old = UserDefaults(suiteName: SharedConfig.appGroup)?.dictionaryRepresentation() {
                for (k, v) in old where std.object(forKey: k) == nil { std.set(v, forKey: k) }
            }
            std.set(true, forKey: "settingsMigrated")
        }
        return std
    }()
    #endif

    // MARK: cme-web (liefert den Rest der Einstellungen)
    /// z.B. https://cme.wiederhol.de
    @Published var cmeWebURL: String { didSet { save() } }
    /// App-Schlüssel aus cme-web (Tab Softphone)
    @Published var appToken: String { didSet { save() } }
    /// Voicemail-Nummer (aus cme-web) – Ziel für "Voicemail" bei eingehenden Anrufen
    @Published var voicemailNumber: String { didSet { save() } }
    /// Letzter Abruf aus cme-web (Anzeige)
    @Published var provisioningStatus: String { didSet { save() } }

    // MARK: Konto
    /// Nebenstelle in CME, z.B. "21".
    @Published var extensionNumber: String { didSet { save() } }
    @Published var displayName: String { didSet { save() } }
    /// SIP-Domain = Adresse der CME (Request-URI-Ziel). Standard: 192.168.11.4
    @Published var cmeHost: String { didSet { save() } }
    /// IP der CME (aus cme-web) – Ziel der direkten Anmeldung im Jabber-Modus
    @Published var cmeIP: String { didSet { save() } }
    /// SIP-Passwort des Pools (CME verlangt es von Soft-Clients; kommt aus cme-web)
    @Published var password: String { didSet { save() } }
    /// SIP-Benutzername des Pools (leer = Nebenstelle ohne "+")
    @Published var authUser: String { didSet { save() } }

    // MARK: Push-Gateway (Flexisip)
    /// Wenn aktiv, registriert sich die App über Flexisip (Outbound-Proxy), sonst direkt an CME.
    @Published var useGateway: Bool { didSet { save() } }
    @Published var gatewayHost: String { didSet { save() } }
    @Published var gatewayPort: Int { didSet { save() } }
    @Published var transport: SIPTransport { didSet { save() } }

    // MARK: Local Push
    @Published var localPushEnabled: Bool { didSet { save() } }
    /// Komma-getrennte SSIDs des Heim-WLANs.
    @Published var localPushSSIDs: String { didSet { save() } }
    @Published var relayHost: String { didSet { save() } }
    @Published var relayPort: Int { didSet { save() } }
    @Published var relaySecret: String { didSet { save() } }

    // MARK: Medien
    @Published var audioCodecs: [CodecEntry] { didSet { save() } }
    @Published var videoCodecs: [CodecEntry] { didSet { save() } }
    @Published var videoEnabled: Bool { didSet { save() } }
    @Published var mediaEncryption: MediaEncryptionSetting { didSet { save() } }

    // MARK: macOS / Tischtelefon
    @Published var macMode: MacMode { didSet { save() } }
    /// IP oder Hostname des Cisco-Telefons (z.B. 8851 am Schreibtisch).
    @Published var deskPhoneHost: String { didSet { save() } }
    /// Nebenstelle des Tischtelefons (für Statusabo / Anzeige), z.B. "10".
    @Published var deskPhoneExtension: String { didSet { save() } }
    /// Zugangsdaten für CGI/Execute (CME: "authentication credential …").
    @Published var deskPhoneUser: String { didSet { save() } }
    @Published var deskPhonePassword: String { didSet { save() } }
    /// Leitung für SendDigits (meist 1).
    @Published var deskPhoneLine: Int { didSet { save() } }
    /// Tasten-URIs (modellabhängig, daher einstellbar).
    @Published var keyAnswer: String { didSet { save() } }
    @Published var keyHangup: String { didSet { save() } }
    @Published var keyHold: String { didSet { save() } }
    @Published var keyDecline: String { didSet { save() } }
    /// Tasten-URI für "an Voicemail umleiten" am Tischtelefon (z.B. iDivert-Softkey).
    @Published var keyVoicemail: String { didSet { save() } }
    /// Call-Toast bei eingehendem Anruf am Tischtelefon.
    @Published var deskPhoneNotify: Bool { didSet { save() } }
    /// Interne Gespräche im Softphone-Modus lokal aufnehmen (externe nimmt cme-recorder auf).
    @Published var localRecording: Bool { didSet { save() } }
    /// Experimentell: wie ein Cisco-Telefon bei CME anmelden (Supported: X-cisco-sis …, Abo "dialog" auf die
    /// eigene Leitung), damit CME die App auf Shared Lines mit anklingelt.
    @Published var ciscoLineMode: Bool { didSet { save() } }
    /// Darstellung der Wähltastatur (Mac)
    @Published var dialpadOutlined: Bool { didSet { save() } }
    /// Gewählte Audiogeräte (Mac), leer = Systemstandard
    @Published var audioInputId: String { didSet { save() } }
    @Published var audioOutputId: String { didSet { save() } }

    static let defaultAudio: [CodecEntry] = [
        .init(mime: "G722", enabled: true),
        .init(mime: "PCMA", enabled: true),
        .init(mime: "PCMU", enabled: true),
        .init(mime: "opus", enabled: true),
    ]
    static let defaultVideo: [CodecEntry] = [
        .init(mime: "H264", enabled: true),
        .init(mime: "H265", enabled: true),
    ]

    private var loading = true

    private init() {
        let d = defaults
        cmeWebURL = d.string(forKey: "cmeWebURL") ?? ""
        appToken = d.string(forKey: "appToken") ?? ""
        voicemailNumber = d.string(forKey: "voicemailNumber") ?? ""
        provisioningStatus = d.string(forKey: "provisioningStatus") ?? ""
        extensionNumber = d.string(forKey: "extensionNumber") ?? ""
        displayName = d.string(forKey: "displayName") ?? ""
        cmeHost = d.string(forKey: "cmeHost") ?? "192.168.11.4"
        cmeIP = d.string(forKey: "cmeIP") ?? "192.168.11.4"
        password = d.string(forKey: "password") ?? ""
        authUser = d.string(forKey: "authUser") ?? ""
        useGateway = d.object(forKey: "useGateway") as? Bool ?? true
        gatewayHost = d.string(forKey: "gatewayHost") ?? "sip-gw.wiederhol.de"
        gatewayPort = d.object(forKey: "gatewayPort") as? Int ?? 5062
        transport = SIPTransport(rawValue: d.string(forKey: "transport") ?? "") ?? .tcp
        localPushEnabled = d.object(forKey: "localPushEnabled") as? Bool ?? true
        localPushSSIDs = d.string(forKey: "localPushSSIDs") ?? ""
        relayHost = d.string(forKey: "relayHost") ?? "sip-gw.wiederhol.de"
        relayPort = d.object(forKey: "relayPort") as? Int ?? 5072
        relaySecret = d.string(forKey: "relaySecret") ?? ""
        audioCodecs = Self.decode(d.data(forKey: "audioCodecs")) ?? Self.defaultAudio
        videoCodecs = Self.decode(d.data(forKey: "videoCodecs")) ?? Self.defaultVideo
        videoEnabled = d.object(forKey: "videoEnabled") as? Bool ?? true
        mediaEncryption = MediaEncryptionSetting(rawValue: d.string(forKey: "mediaEncryption") ?? "") ?? .none
        macMode = MacMode(rawValue: d.string(forKey: "macMode") ?? "") ?? .softphone
        deskPhoneHost = d.string(forKey: "deskPhoneHost") ?? ""
        deskPhoneExtension = d.string(forKey: "deskPhoneExtension") ?? ""
        deskPhoneUser = d.string(forKey: "deskPhoneUser") ?? "cmephone"
        deskPhonePassword = d.string(forKey: "deskPhonePassword") ?? ""
        deskPhoneLine = d.object(forKey: "deskPhoneLine") as? Int ?? 1
        keyAnswer = d.string(forKey: "keyAnswer") ?? "Key:Speaker"
        keyHangup = d.string(forKey: "keyHangup") ?? "Key:Speaker"
        keyHold = d.string(forKey: "keyHold") ?? "Key:Hold"
        keyDecline = d.string(forKey: "keyDecline") ?? "Key:Soft3"
        keyVoicemail = d.string(forKey: "keyVoicemail") ?? "Key:Soft2"
        deskPhoneNotify = d.object(forKey: "deskPhoneNotify") as? Bool ?? true
        localRecording = d.object(forKey: "localRecording") as? Bool ?? true
        // Bis zur Version mit Standard "an" wurde der Schalter als "aus" gespeichert – einmalig einschalten
        if d.object(forKey: "ciscoLineModeDefaultOn") == nil {
            d.set(true, forKey: "ciscoLineModeDefaultOn")
            d.set(true, forKey: "ciscoLineMode")
        }
        ciscoLineMode = d.object(forKey: "ciscoLineMode") as? Bool ?? true
        dialpadOutlined = d.object(forKey: "dialpadOutlined") as? Bool ?? false
        audioInputId = d.string(forKey: "audioInputId") ?? ""
        audioOutputId = d.string(forKey: "audioOutputId") ?? ""
        loading = false
        #if os(iOS)
        registerSettingsBundleDefaults()
        readSettingsBundle()
        #endif
    }

    /// Gerätename am Router (Pool "CME Phone" in cme-web: id device-id-name CSF<Nebenstelle>, wie Cisco Jabber).
    var deviceName: String { "CSF" + extensionNumber.filter { $0.isNumber } }

    var hasCmeWeb: Bool { !cmeWebURL.trimmingCharacters(in: .whitespaces).isEmpty && !appToken.isEmpty }

    #if os(iOS)
    // MARK: Einstellungen-App (Settings.bundle)
    // Schalter für die Audio-Codecs (Reihenfolge fest: G.722, G.711 A-law, G.711 µ-law, Opus)
    private static let codecSwitches = ["G722": "codec_G722", "PCMA": "codec_PCMA", "PCMU": "codec_PCMU", "opus": "codec_opus"]

    private func registerSettingsBundleDefaults() {
        var reg: [String: Any] = ["useGateway": true, "videoEnabled": true, "localPushEnabled": true,
                                  "cmeHost": "192.168.11.4", "gatewayPort": 5062, "relayPort": 5072]
        for key in Self.codecSwitches.values { reg[key] = true }
        defaults.register(defaults: reg)
    }

    /// Übernimmt die Werte, die in der Einstellungen-App geändert wurden.
    /// Gibt true zurück, wenn sich etwas Anmelderelevantes geändert hat.
    @discardableResult
    func readSettingsBundle() -> Bool {
        let d = defaults
        let before = accountFingerprint
        loading = true
        cmeWebURL = (d.string(forKey: "cmeWebURL") ?? "").trimmingCharacters(in: .whitespaces)
        appToken = (d.string(forKey: "appToken") ?? "").trimmingCharacters(in: .whitespaces)
        extensionNumber = (d.string(forKey: "extensionNumber") ?? "").trimmingCharacters(in: .whitespaces)
        displayName = d.string(forKey: "displayName") ?? ""
        cmeHost = d.string(forKey: "cmeHost") ?? "192.168.11.4"
        gatewayHost = d.string(forKey: "gatewayHost") ?? gatewayHost
        gatewayPort = d.object(forKey: "gatewayPort") as? Int ?? Int(d.string(forKey: "gatewayPort") ?? "") ?? 5062
        useGateway = d.bool(forKey: "useGateway")
        videoEnabled = d.bool(forKey: "videoEnabled")
        localPushEnabled = d.bool(forKey: "localPushEnabled")
        audioCodecs = Self.defaultAudio.map { CodecEntry(mime: $0.mime, enabled: d.bool(forKey: Self.codecSwitches[$0.mime] ?? "")) }
        loading = false
        save()
        return before != accountFingerprint
    }

    /// Werte der Einstellungen-App als Text – ändert sich nur, wenn dort etwas geändert wurde
    /// (UserDefaults.didChangeNotification kommt auch bei eigenen Schreibzugriffen).
    var settingsBundleFingerprint: String {
        let keys = ["cmeWebURL", "appToken", "extensionNumber", "displayName", "cmeHost", "gatewayHost", "gatewayPort",
                    "useGateway", "videoEnabled", "localPushEnabled"] + Self.codecSwitches.values.sorted()
        return keys.map { "\(defaults.object(forKey: $0) ?? "")" }.joined(separator: "|")
    }

    /// Statuszeile in der Einstellungen-App (PSTitleValueSpecifier "status")
    func publishStatus(_ text: String) { defaults.set(text, forKey: "status") }
    #endif

    /// Alles, was eine Neuanmeldung erfordert.
    var accountFingerprint: String {
        [extensionNumber, displayName, cmeHost, password, authUser, "\(useGateway)", gatewayHost, "\(gatewayPort)",
         transport.rawValue, audioCodecs.map { "\($0.mime)\($0.enabled)" }.joined(), "\(videoEnabled)",
         "\(ciscoLineMode)", cmeIP].joined(separator: "|")
    }

    /// Werte aus cme-web übernehmen (GET /client/v1/info).
    @discardableResult
    func apply(info: ClientAPI.Info) -> Bool {
        let before = accountFingerprint
        loading = true
        if !info.cmeHost.isEmpty { cmeHost = info.cmeHost }
        if let ip = info.cmeIP, !ip.isEmpty { cmeIP = ip }
        if !info.gateway.host.isEmpty {
            useGateway = true
            gatewayHost = info.gateway.host
            gatewayPort = info.gateway.port
            transport = SIPTransport(rawValue: info.gateway.transport) ?? .tcp
        }
        if !info.localpush.host.isEmpty {
            relayHost = info.localpush.host
            relayPort = info.localpush.port
            relaySecret = info.localpush.secret
            if !info.localpush.ssids.isEmpty { localPushSSIDs = info.localpush.ssids.joined(separator: ", ") }
        }
        voicemailNumber = info.voicemail
        if let a = info.auth, let pw = a.password, !pw.isEmpty {
            password = pw
            authUser = a.username ?? ""
        }
        if let d = info.deskphone, let pw = d.password, !pw.isEmpty {
            deskPhoneUser = d.user ?? "cmephone"
            deskPhonePassword = pw
        }
        loading = false
        save()
        return before != accountFingerprint
    }

    var isConfigured: Bool { !extensionNumber.isEmpty && !cmeHost.isEmpty }

    /// Mac im Cisco-Leitungsmodus: direkt an CME anmelden – genau wie Cisco Jabber (UDP, Domain = CME-IP).
    /// Über Flexisip kommen alle Apps von derselben IP; CME hält das für eine doppelte Anmeldung und
    /// lässt das Gerät dann bei Shared Lines aus. iOS bleibt beim Gateway (Push).
    var jabberMode: Bool {
        #if os(macOS)
        return ciscoLineMode && !cmeIP.isEmpty
        #else
        return false
        #endif
    }

    /// Host für Request-URIs (Anmeldung, Anrufe, Abos): im Jabber-Modus die CME-IP, sonst die SIP-Domain.
    var sipHost: String { jabberMode ? cmeIP : cmeHost }

    var ssidList: [String] {
        localPushSSIDs.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    private static func decode(_ data: Data?) -> [CodecEntry]? {
        guard let data else { return nil }
        return try? JSONDecoder().decode([CodecEntry].self, from: data)
    }

    private func save() {
        guard !loading else { return }
        let d = defaults
        d.set(cmeWebURL, forKey: "cmeWebURL")
        d.set(appToken, forKey: "appToken")
        d.set(voicemailNumber, forKey: "voicemailNumber")
        d.set(provisioningStatus, forKey: "provisioningStatus")
        d.set(extensionNumber, forKey: "extensionNumber")
        d.set(displayName, forKey: "displayName")
        d.set(cmeHost, forKey: "cmeHost")
        d.set(cmeIP, forKey: "cmeIP")
        d.set(password, forKey: "password")
        d.set(authUser, forKey: "authUser")
        d.set(useGateway, forKey: "useGateway")
        d.set(gatewayHost, forKey: "gatewayHost")
        d.set(gatewayPort, forKey: "gatewayPort")
        d.set(transport.rawValue, forKey: "transport")
        d.set(localPushEnabled, forKey: "localPushEnabled")
        d.set(localPushSSIDs, forKey: "localPushSSIDs")
        d.set(relayHost, forKey: "relayHost")
        d.set(relayPort, forKey: "relayPort")
        d.set(relaySecret, forKey: "relaySecret")
        d.set(try? JSONEncoder().encode(audioCodecs), forKey: "audioCodecs")
        d.set(try? JSONEncoder().encode(videoCodecs), forKey: "videoCodecs")
        d.set(videoEnabled, forKey: "videoEnabled")
        d.set(mediaEncryption.rawValue, forKey: "mediaEncryption")
        d.set(macMode.rawValue, forKey: "macMode")
        d.set(deskPhoneHost, forKey: "deskPhoneHost")
        d.set(deskPhoneExtension, forKey: "deskPhoneExtension")
        d.set(deskPhoneUser, forKey: "deskPhoneUser")
        d.set(deskPhonePassword, forKey: "deskPhonePassword")
        d.set(deskPhoneLine, forKey: "deskPhoneLine")
        d.set(keyAnswer, forKey: "keyAnswer")
        d.set(keyHangup, forKey: "keyHangup")
        d.set(keyHold, forKey: "keyHold")
        d.set(keyDecline, forKey: "keyDecline")
        d.set(keyVoicemail, forKey: "keyVoicemail")
        d.set(deskPhoneNotify, forKey: "deskPhoneNotify")
        d.set(localRecording, forKey: "localRecording")
        d.set(ciscoLineMode, forKey: "ciscoLineMode")
        d.set(dialpadOutlined, forKey: "dialpadOutlined")
        d.set(audioInputId, forKey: "audioInputId")
        d.set(audioOutputId, forKey: "audioOutputId")
        #if os(iOS)
        // Codec-Schalter der Einstellungen-App mitführen
        for c in audioCodecs { if let k = Self.codecSwitches[c.mime] { d.set(c.enabled, forKey: k) } }
        // Für die Local-Push-Extension (nur Diagnose) auch in die App-Group spiegeln
        let g = UserDefaults(suiteName: SharedConfig.appGroup)
        g?.set(relayHost, forKey: "relayHost"); g?.set(relayPort, forKey: "relayPort")
        #endif
    }
}
