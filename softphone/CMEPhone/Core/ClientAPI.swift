import Foundation

/// Schnittstelle zu cme-web (phoneapp.py, /client/v1/...).
/// Anmeldung mit dem App-Schlüssel aus cme-web (Tab Softphone).
enum ClientAPI {
    struct Info: Decodable {
        struct Gateway: Decodable { var host: String; var port: Int; var transport: String }
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
        var deskphone: DeskAuth?
        /// IP der CME für die direkte Anmeldung (Mac, Jabber-Modus)
        var cmeIP: String?

        enum CodingKeys: String, CodingKey {
            case ext, gateway, localpush, voicemail, recording, auth, deskphone
            case deviceName = "device_name"
            case cmeIP = "cme_ip"
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

    static func info(ext: String) async throws -> Info {
        try await load(try request("/client/v1/info", query: ["ext": ext]), as: Info.self)
    }

    static func calls(ext: String) async throws -> [RecCall] {
        try await load(try request("/client/v1/calls", query: ["ext": ext]), as: CallsResponse.self).calls
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
                let info = try await ClientAPI.info(ext: s.extensionNumber)
                let changed = s.apply(info: info)
                s.provisioningStatus = "Von cme-web übernommen (\(Date().formatted(date: .omitted, time: .shortened)))"
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
