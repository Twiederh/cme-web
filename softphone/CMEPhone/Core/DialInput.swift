import Foundation

/// Eingabe im Wählfeld (iOS und Mac): Rufnummer oder Adresse für URI-Wahl (name@domain).
enum DialInput {
    /// Zeichen einer Rufnummer
    static func digits(_ s: String) -> String { s.filter { "0123456789*#+".contains($0) } }

    /// Zeichen, die in einer Adresse wie name@domain vorkommen dürfen (klein geschrieben, ohne sip:)
    static func uriChars(_ s: String) -> String {
        var v = s.lowercased()
        for p in ["sips:", "sip:"] where v.hasPrefix(p) { v.removeFirst(p.count) }
        return String(v.unicodeScalars.filter {
            ("a"..."z").contains($0) || ("0"..."9").contains($0) || "@.-_+%*#".unicodeScalars.contains($0)
        }.map(Character.init))
    }

    /// Adresse, sobald Buchstaben oder @ drin sind (ohne ein vorangestelltes sip:), sonst Rufnummer
    static func isAddress(_ s: String) -> Bool {
        var v = s.lowercased()
        for p in ["sips:", "sip:"] where v.hasPrefix(p) { v.removeFirst(p.count) }
        return v.contains { $0 == "@" || $0.isLetter }
    }

    /// Bereinigt: Adresse oder Rufnummer
    static func clean(_ s: String) -> String { isAddress(s) ? uriChars(s) : digits(s) }

    /// Wählbar? (nicht leer, kein @ am Anfang oder Ende)
    static func isDialable(_ s: String) -> Bool { !s.isEmpty && !s.hasPrefix("@") && !s.hasSuffix("@") }
}
