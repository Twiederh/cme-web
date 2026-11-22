import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

/// Bausteine für Cisco IP Phone XML Services (CiscoIPPhoneExecute).
/// Bewusst ohne Netzwerk-/UI-Abhängigkeiten, damit sie testbar bleiben.
enum CiscoXML {

    /// Eine Ausführungsanweisung an das Telefon (Execute-URI).
    struct Command: Equatable {
        let uri: String
        /// 0 = sofort, 1 = wenn Telefon frei, 2 = nur wenn frei (sonst verwerfen)
        var priority: Int = 0

        static func dial(_ number: String) -> Command { .init(uri: "Dial:\(sanitize(number))") }
        static func key(_ key: String) -> Command { .init(uri: key.hasPrefix("Key:") ? key : "Key:\(key)") }
        static func sendDigits(_ digits: String, line: Int) -> Command {
            .init(uri: "SendDigits:\(sanitize(digits)):\(line)")
        }
        static func raw(_ uri: String) -> Command { .init(uri: uri) }

        /// Nur Ziffern, *, #, + und @/. für SIP-URIs zulassen – nichts, was das XML aufbrechen kann.
        static func sanitize(_ s: String) -> String {
            String(s.filter { $0.isNumber || "*#+@.:-_".contains($0) || $0.isLetter })
        }
    }

    /// XML-Dokument für bis zu 3 ExecuteItems (Cisco-Limit).
    static func executeXML(_ commands: [Command]) -> String {
        let items = commands.prefix(3).map {
            "<ExecuteItem Priority=\"\($0.priority)\" URL=\"\(escape($0.uri))\"/>"
        }.joined()
        return "<?xml version=\"1.0\" encoding=\"utf-8\"?><CiscoIPPhoneExecute>\(items)</CiscoIPPhoneExecute>"
    }

    /// Request-Body für POST /CGI/Execute (Formular-Kodierung "XML=<urlencoded>").
    static func formBody(_ commands: [Command]) -> Data {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let encoded = executeXML(commands).addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
        return Data("XML=\(encoded)".utf8)
    }

    static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }

    // MARK: - Antwort

    enum Result: Equatable {
        case ok([ResponseItem])
        case error(number: Int, text: String)
        case unexpected(String)

        var isOK: Bool { if case .ok(let items) = self { return items.allSatisfy { $0.status == 0 } } else { return false } }

        var message: String {
            switch self {
            case .ok(let items):
                let bad = items.filter { $0.status != 0 }
                return bad.isEmpty ? "OK" : bad.map { "\($0.url): Status \($0.status) \($0.data)" }.joined(separator: ", ")
            case .error(let n, let t): return "CiscoIPPhoneError \(n): \(t)"
            case .unexpected(let s): return "Unerwartete Antwort: \(s.prefix(120))"
            }
        }
    }

    struct ResponseItem: Equatable {
        let status: Int
        let data: String
        let url: String
    }

    /// Fehlernummern laut Cisco IP Phone Services Development Notes.
    static func errorText(_ n: Int) -> String {
        switch n {
        case 1: return "Fehler beim Parsen des CiscoIPPhoneExecute-Objekts"
        case 2: return "Fehler beim Bilden des Antwortobjekts"
        case 3: return "Interner Dateifehler"
        case 4: return "Authentifizierung fehlgeschlagen (url authentication / Zugangsdaten prüfen)"
        case 6: return "Telefon im falschen Zustand für diese Aktion"
        default: return "unbekannt"
        }
    }

    static func parseResponse(_ data: Data) -> Result {
        let p = ResponseParser()
        let parser = XMLParser(data: data)
        parser.delegate = p
        guard parser.parse() else {
            return .unexpected(String(decoding: data, as: UTF8.self))
        }
        if let n = p.errorNumber { return .error(number: n, text: errorText(n)) }
        if p.sawResponse { return .ok(p.items) }
        return .unexpected(String(decoding: data, as: UTF8.self))
    }

    private final class ResponseParser: NSObject, XMLParserDelegate {
        var items: [ResponseItem] = []
        var errorNumber: Int?
        var sawResponse = false

        func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                    qualifiedName qName: String?, attributes attributeDict: [String: String] = [:]) {
            switch elementName {
            case "CiscoIPPhoneResponse": sawResponse = true
            case "ResponseItem":
                items.append(.init(status: Int(attributeDict["Status"] ?? "") ?? -1,
                                   data: attributeDict["Data"] ?? "",
                                   url: attributeDict["URL"] ?? ""))
            case "CiscoIPPhoneError":
                errorNumber = Int(attributeDict["Number"] ?? "") ?? -1
            default: break
            }
        }
    }
}
