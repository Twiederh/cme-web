import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

/// Zustand einer Nebenstelle aus einem SIP-NOTIFY (RFC 4235 dialog-info oder PIDF-Presence).
struct LineStatus: Equatable {
    enum State: String, Equatable {
        case idle, ringing, dialing, busy, unknown
        var label: String {
            switch self {
            case .idle: return "Frei"
            case .ringing: return "Klingelt"
            case .dialing: return "Wählt / Ruft"
            case .busy: return "Im Gespräch"
            case .unknown: return "Unbekannt"
            }
        }
    }

    var state: State
    /// Gegenstelle (Nummer), falls CME sie mitliefert.
    var remoteNumber: String?
    var remoteName: String?
    var dialogId: String?

    static let unknown = LineStatus(state: .unknown)
}

enum DialogInfoParser {
    /// Parst `application/dialog-info+xml` (RFC 4235) und `application/pidf+xml`.
    static func parse(_ body: String) -> LineStatus {
        guard let data = body.data(using: .utf8) else { return .unknown }
        let p = Parser()
        let x = XMLParser(data: data)
        x.shouldProcessNamespaces = true
        x.delegate = p
        guard x.parse() else { return .unknown }
        return p.result()
    }

    private final class Parser: NSObject, XMLParserDelegate {
        private struct Dialog {
            var id: String?
            var direction: String?
            var state: String = ""
            var remoteIdentity: String = ""
            var remoteDisplay: String?
        }
        private var dialogs: [Dialog] = []
        private var current: Dialog?
        private var path: [String] = []
        private var text = ""
        private var sawDialogInfo = false
        // PIDF
        private var sawPIDF = false
        private var basic: String?
        private var notes: [String] = []
        private var activities: Set<String> = []

        func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                    qualifiedName qName: String?, attributes attributeDict: [String: String] = [:]) {
            path.append(elementName)
            text = ""
            switch elementName {
            case "dialog-info": sawDialogInfo = true
            case "presence": sawPIDF = true
            case "dialog":
                current = Dialog(id: attributeDict["id"], direction: attributeDict["direction"])
            case "identity" where path.contains("remote"):
                current?.remoteDisplay = attributeDict["display"]
            case "on-the-phone", "busy", "away", "meeting":
                activities.insert(elementName)
            default: break
            }
        }

        func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }

        func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) {
            let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
            switch elementName {
            case "state" where current != nil: current?.state = value.lowercased()
            case "identity" where path.contains("remote"): current?.remoteIdentity = value
            case "dialog":
                if let d = current { dialogs.append(d) }
                current = nil
            case "basic": basic = value.lowercased()
            case "note": notes.append(value.lowercased())
            default: break
            }
            path.removeLast()
            text = ""
        }

        func result() -> LineStatus {
            if sawDialogInfo {
                // Aktivsten Dialog wählen: confirmed > early > proceeding/trying > terminated
                let active = dialogs.filter { $0.state != "terminated" }
                guard let d = active.first(where: { $0.state == "confirmed" })
                        ?? active.first(where: { $0.state == "early" })
                        ?? active.first else {
                    return LineStatus(state: .idle)
                }
                let state: LineStatus.State
                switch d.state {
                case "confirmed": state = .busy
                case "early":
                    state = (d.direction == "initiator") ? .dialing : .ringing
                case "trying", "proceeding": state = .dialing
                default: state = .unknown
                }
                return LineStatus(state: state,
                                  remoteNumber: Self.number(from: d.remoteIdentity),
                                  remoteName: d.remoteDisplay,
                                  dialogId: d.id)
            }
            if sawPIDF {
                if activities.contains("on-the-phone") || activities.contains("busy")
                    || notes.contains(where: { $0.contains("phone") || $0.contains("busy") || $0.contains("besetzt") }) {
                    return LineStatus(state: .busy)
                }
                if basic == "open" { return LineStatus(state: .idle) }
                if basic == "closed" { return LineStatus(state: .busy) }
            }
            return .unknown
        }

        static func number(from identity: String) -> String? {
            guard !identity.isEmpty else { return nil }
            var s = identity
            for prefix in ["sips:", "sip:", "tel:"] where s.hasPrefix(prefix) { s.removeFirst(prefix.count) }
            if let at = s.firstIndex(of: "@") { s = String(s[..<at]) }
            if let semi = s.firstIndex(of: ";") { s = String(s[..<semi]) }
            return s.isEmpty ? nil : s
        }
    }
}
