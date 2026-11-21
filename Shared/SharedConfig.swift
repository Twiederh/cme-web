import Foundation

/// Gemeinsame Konstanten und Datentypen für App und Local-Push-Extension.
enum SharedConfig {
    static let appGroup = "group.de.wiederhol.cmephone"

    /// Schlüssel in `NEAppPushManager.providerConfiguration`.
    enum ProviderKey {
        static let relayHost = "relayHost"
        static let relayPort = "relayPort"
        static let relaySecret = "relaySecret"
        static let deviceToken = "deviceToken"
    }

    /// Schlüssel im `userInfo`, das die Extension per `reportIncomingCall` an die App übergibt.
    enum CallInfoKey {
        static let callId = "call-id"
        static let fromURI = "from-uri"
        static let displayName = "display-name"
        static let hasVideo = "video"
    }
}

/// Zeilenbasiertes JSON-Protokoll zwischen Extension und push-relay.
/// Jede Nachricht ist ein JSON-Objekt, abgeschlossen mit "\n".
///
/// Extension -> Relay:  {"type":"hello","token":"<voip-token-hex>","secret":"..."}
///                      {"type":"ping"}
/// Relay -> Extension:  {"type":"call","call-id":"...","from-uri":"...","display-name":"..."}
///                      {"type":"pong"} / {"type":"ok"} / {"type":"error","reason":"..."}
struct LocalPushMessage: Codable {
    var type: String
    var token: String?
    var secret: String?
    var callId: String?
    var fromURI: String?
    var displayName: String?
    var video: Bool?
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case type, token, secret, reason, video
        case callId = "call-id"
        case fromURI = "from-uri"
        case displayName = "display-name"
    }
}
