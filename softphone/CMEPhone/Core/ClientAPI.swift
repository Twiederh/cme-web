import Foundation

/// Schnittstelle zu cme-web (phoneapp.py, /client/v1/...).
/// Anmeldung mit dem App-Schlüssel aus cme-web (Tab Softphone).
enum ClientAPI {
    struct Info: Decodable {
        struct Gateway: Decodable {
            var host: String; var port: Int; var transport: String
            /// SIP_PUBLIC_DOMAIN und der TCP-Port von Kamailio für Anrufe von draußen (fehlt bei älterem cme-web)
            var domain: String?; var ext_port: Int?
            /// SIP_PUBLIC_DOMAIN und weitere Domains (Aliasse), die Kamailio annimmt
            var domains: [String]?
        }
        struct LocalPush: Decodable { var host: String; var port: Int; var secret: String; var ssids: [String] }
        struct Recording: Decodable { var available: Bool; var mode: String }
        /// SIP-Zugangsdaten des Pools (CME verlangt sie von Soft-Clients); leer, wenn cme-web keine kennt
        struct Auth: Decodable { var username: String?; var password: String? }
        /// Zugang für die Tischtelefon-Steuerung (CGI/Execute) – cme-web beantwortet die Authentication URL
        struct DeskAuth: Decodable { var user: String?; var password: String? }
        var ext: String
        var deviceName: String
        var cmeHost: String
        var gateway: Gateway
        var localpush: LocalPush
        var voicemail: String
        var recording: Recording
        var auth: Auth?
        /// iPhone/iPad (Anmeldung über das Push-Gateway): Zugang des Pools MOB<Nummer>, gilt für alle Mobilgeräte
        var mobileAuth: Auth?
        var deskphone: DeskAuth?
        /// IP der CME für die direkte Anmeldung (Mac, Jabber-Modus)
        var cmeIP: String?

        enum CodingKeys: String, CodingKey {
            case ext, gateway, localpush, voicemail, recording, auth, deskphone
            case deviceName = "device_name"
            case cmeIP = "cme_ip"
            case mobileAuth = "mobile_auth"
            case cmeHost = "cme_host"
        }
    }

    /// Laufendes externes Gespräch beim cme-recorder.
    struct RecCall: Decodable, Identifiable, Equatable {
        var id: String
        var other: String
        var recording: Bool
        var start: Double
    }

    enum APIError: LocalizedError {
        case notConfigured, http(Int, String), invalid
        var errorDescription: String? {
            switch self {
            case .notConfigured: return "cme-web-Adresse oder App-Schlüssel fehlt"
            case .http(401, _): return "App-Schlüssel falsch (cme-web → Softphone)"
            case .http(let c, let m): return m.isEmpty ? "cme-web: HTTP \(c)" : m
            case .invalid: return "Unerwartete Antwort von cme-web"
            }
        }
    }

    private struct CallsResponse: Decodable { var calls: [RecCall] }
    private struct ErrorResponse: Decodable { var error: String? }

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 6
        cfg.httpCookieStorage = nil
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    private static func request(_ path: String, query: [String: String] = [:], method: String = "GET",
                                body: [String: Any]? = nil) throws -> URLRequest {
        let s = PhoneSettings.shared
        var base = s.cmeWebURL.trimmingCharacters(in: .whitespaces)
        guard !base.isEmpty, !s.appToken.isEmpty else { throw APIError.notConfigured }
        if !base.lowercased().hasPrefix("http") { base = "https://" + base }
        while base.hasSuffix("/") { base.removeLast() }
        guard var comp = URLComponents(string: base + path) else { throw APIError.notConfigured }
        if !query.isEmpty { comp.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        guard let url = comp.url else { throw APIError.notConfigured }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("Bearer \(s.appToken)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return req
    }

    private static func load<T: Decodable>(_ req: URLRequest, as: T.Type) async throws -> T {
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            let msg = (try? JSONDecoder().decode(ErrorResponse.self, from: data))?.error ?? ""
            throw APIError.http(code, msg)
        }
        do { return try JSONDecoder().decode(T.self, from: data) } catch { throw APIError.invalid }
    }

    static func info(ext: String, device: String = "") async throws -> Info {
        var q = ["ext": ext]
        if !device.isEmpty { q["device"] = device }
        #if os(iOS)
        q["tls"] = "1"   // iPhone/iPad: über TLS zum Gateway (Kamailio), auch von unterwegs ohne VPN
        #endif
        return try await load(try request("/client/v1/info", query: q), as: Info.self)
    }

    /// Ereignis der Nebenstelle (Mac: Long-Poll), z.B. „jemand wartet in der Lobby“ eines Jitsi-Raums
    struct AppEvent: Decodable, Sendable {
        var id: Int
        var type: String
        var room: String?
        var name: String?
        var url: String?
    }
    struct EventsResponse: Decodable, Sendable { var events: [AppEvent]; var last: Int }

    private static let pollSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 40   // cme-web hält die Anfrage bis zu 25 s offen
        cfg.httpCookieStorage = nil
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    /// Neue Ereignisse seit `since` (0: nur den aktuellen Stand holen); wartet serverseitig bis zu `wait` s.
    static func events(ext: String, since: Int, wait: Int = 25) async throws -> EventsResponse {
        let req = try request("/client/v1/events", query: ["ext": ext, "since": String(since), "wait": String(wait)])
        let (data, resp) = try await pollSession.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else { throw APIError.http(code, "") }
        do { return try JSONDecoder().decode(EventsResponse.self, from: data) } catch { throw APIError.invalid }
    }

    static func calls(ext: String) async throws -> [RecCall] {
        try await load(try request("/client/v1/calls", query: ["ext": ext]), as: CallsResponse.self).calls
    }

    /// Normaler APNs-Token der App (Mitteilungen wie „gehalten – übernehmen“). env: "prod" (TestFlight/App Store)
    /// oder "sandbox" (aus Xcode installiert).
    static func registerPush(device: String, token: String, env: String) async throws {
        _ = try await load(try request("/client/v1/push", method: "POST",
                                       body: ["device": device, "token": token, "env": env]),
                           as: ErrorResponse.self)
    }

    // MARK: Voicemail und Gesprächsaufnahmen (cme-web: Postfach bzw. Aufnahmen der Nebenstelle)

    struct VoicemailMessage: Decodable {
        var box: String, folder: String, file: String, time: Int, duration: Int
        var caller: String, callerName: String, isNew: Bool
        enum CodingKeys: String, CodingKey {
            case box, folder, file, time, duration, caller, callerName = "caller_name", isNew = "new"
        }
    }
    private struct VoicemailResponse: Decodable { var messages: [VoicemailMessage] }

    struct ServerRecording: Decodable {
        var id: String, start: Double, duration: Double, direction: String, external: String
        var transcript: String, name: String
        private struct Party: Decodable { var number: String?; var name: String? }
        enum CodingKeys: String, CodingKey { case id, start, duration, direction, external, transcript, parties }
        init(from d: Decoder) throws {
            let c = try d.container(keyedBy: CodingKeys.self)
            id = try c.decode(String.self, forKey: .id)
            start = (try? c.decode(Double.self, forKey: .start)) ?? 0
            duration = (try? c.decode(Double.self, forKey: .duration)) ?? 0
            direction = (try? c.decode(String.self, forKey: .direction)) ?? ""
            external = (try? c.decode(String.self, forKey: .external)) ?? ""
            transcript = (try? c.decode(String.self, forKey: .transcript)) ?? ""
            let parties = (try? c.decode([Party].self, forKey: .parties)) ?? []
            let ext = external
            name = parties.first { $0.number == ext }?.name ?? ""
        }
    }
    private struct RecordingsResponse: Decodable { var recordings: [ServerRecording] }

    static func voicemail(ext: String) async throws -> [VoicemailMessage] {
        try await load(try request("/client/v1/voicemail", query: ["ext": ext]), as: VoicemailResponse.self).messages
    }

    static func voicemailAudio(ext: String, _ m: VoicemailMessage) async throws -> Data {
        try await raw(try request("/client/v1/voicemail/\(m.box)/\(m.folder)/\(m.file)/audio", query: ["ext": ext]))
    }

    static func setVoicemailHeard(ext: String, _ m: VoicemailMessage, heard: Bool) async throws {
        _ = try await load(try request("/client/v1/voicemail/\(m.box)/\(m.folder)/\(m.file)", method: "POST",
                                       body: ["ext": ext, "heard": heard]), as: ErrorResponse.self)
    }

    static func deleteVoicemail(ext: String, _ m: VoicemailMessage) async throws {
        _ = try await load(try request("/client/v1/voicemail/\(m.box)/\(m.folder)/\(m.file)", query: ["ext": ext],
                                       method: "DELETE"), as: ErrorResponse.self)
    }

    static func recordings(ext: String) async throws -> [ServerRecording] {
        try await load(try request("/client/v1/recordings", query: ["ext": ext]), as: RecordingsResponse.self).recordings
    }

    static func recordingAudio(ext: String, id: String) async throws -> Data {
        try await raw(try request("/client/v1/recordings/\(id)/audio", query: ["ext": ext]))
    }

    static func deleteRecording(ext: String, id: String) async throws {
        _ = try await load(try request("/client/v1/recordings/\(id)", query: ["ext": ext], method: "DELETE"),
                           as: ErrorResponse.self)
    }

    /// Audiodatei laden (längere Zeit als die übrigen Anfragen erlaubt)
    private static func raw(_ req: URLRequest) async throws -> Data {
        var r = req
        r.timeoutInterval = 60
        let (data, resp) = try await session.data(for: r)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            let msg = (try? JSONDecoder().decode(ErrorResponse.self, from: data))?.error ?? ""
            throw APIError.http(code, msg)
        }
        return data
    }

    static func setRecording(id: String, ext: String, on: Bool) async throws {
        _ = try await load(try request("/client/v1/calls/\(id)", method: "POST", body: ["ext": ext, "on": on]),
                           as: ErrorResponse.self)
    }
}

/// Holt die Einstellungen aus cme-web und meldet die App bei Änderungen neu an.
@MainActor
enum Provisioning {
    private static var running = false

    static func refresh() {
        let s = PhoneSettings.shared
        guard s.hasCmeWeb, !s.extensionNumber.isEmpty, !running else { return }
        running = true
        Task { @MainActor in
            defer { running = false }
            do {
                let info = try await ClientAPI.info(ext: s.extensionNumber, device: s.deviceLabel)
                let changed = s.apply(info: info)
                #if os(iOS)
                HeldCallNotifications.shared.sendToken()
                #endif
                s.provisioningStatus = "Von cme-web übernommen (\(Date().formatted(date: .omitted, time: .shortened)))"
                if !s.jabberMode, (info.mobileAuth?.password ?? "").isEmpty {
                    // Über das Push-Gateway braucht jedes Gerät einen eigenen Namen in cme-web
                    s.provisioningStatus = s.deviceLabel.isEmpty
                        ? "Gerätename fehlt – in cme-web (Tab Softphone → Mobilgeräte) anlegen und hier eintragen"
                        : "\(s.deviceLabel) ist in cme-web nicht als Mobilgerät angelegt (Tab Softphone → Mobilgeräte)"
                }
                SIPManager.shared.log("cme-web: Einstellungen übernommen, Gateway \(info.gateway.host):\(info.gateway.port)")
                if changed { SIPManager.shared.applyAccount() }
                #if os(iOS)
                LocalPushController.shared.apply()
                #endif
            } catch {
                s.provisioningStatus = "cme-web: \(error.localizedDescription)"
                SIPManager.shared.log("cme-web: \(error.localizedDescription)")
            }
        }
    }
}
