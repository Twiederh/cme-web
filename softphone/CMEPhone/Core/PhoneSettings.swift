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

/// Welches SIP-Event-Paket CME für das Statusabo liefert.
enum StatusEventPackage: String, Codable, CaseIterable, Identifiable {
    case dialog, presence
    var id: String { rawValue }
}

enum MediaEncryptionSetting: String, Codable, CaseIterable, Identifiable {
    case none, srtp
    var id: String { rawValue }
    var label: String { self == .none ? "Aus (RTP)" : "SRTP (SDES, nur mit TLS)" }
}

/// Persistente Einstellungen (App-Group-UserDefaults, damit Werte für Diagnose auch
/// in der Extension lesbar sind).
final class PhoneSettings: ObservableObject {
    static let shared = PhoneSettings()
    private let defaults = UserDefaults(suiteName: SharedConfig.appGroup) ?? .standard

    // MARK: Konto
    /// Nebenstelle in CME, z.B. "21".
    @Published var extensionNumber: String { didSet { save() } }
    @Published var displayName: String { didSet { save() } }
    /// SIP-Domain = Adresse der CME (Request-URI-Ziel). Standard: 192.168.11.4
    @Published var cmeHost: String { didSet { save() } }
    /// Optionales Passwort (nur falls im Pool `authenticate` aktiv ist).
    @Published var password: String { didSet { save() } }

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
    @Published var statusEvent: StatusEventPackage { didSet { save() } }
    /// Mitteilung bei eingehendem Anruf am Tischtelefon.
    @Published var deskPhoneNotify: Bool { didSet { save() } }

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
        extensionNumber = d.string(forKey: "extensionNumber") ?? ""
        displayName = d.string(forKey: "displayName") ?? ""
        cmeHost = d.string(forKey: "cmeHost") ?? "192.168.11.4"
        password = d.string(forKey: "password") ?? ""
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
        statusEvent = StatusEventPackage(rawValue: d.string(forKey: "statusEvent") ?? "") ?? .dialog
        deskPhoneNotify = d.object(forKey: "deskPhoneNotify") as? Bool ?? true
        loading = false
    }

    var isConfigured: Bool { !extensionNumber.isEmpty && !cmeHost.isEmpty }

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
        d.set(extensionNumber, forKey: "extensionNumber")
        d.set(displayName, forKey: "displayName")
        d.set(cmeHost, forKey: "cmeHost")
        d.set(password, forKey: "password")
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
        d.set(statusEvent.rawValue, forKey: "statusEvent")
        d.set(deskPhoneNotify, forKey: "deskPhoneNotify")
    }
}
