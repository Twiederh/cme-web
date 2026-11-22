import Foundation

/// Wählregel für Rückrufe aus der Anrufliste.
enum DialRule {
    /// "+49…" bleibt (E.164, CME setzt es um); sonst bekommen Nummern mit mehr als 4 Ziffern
    /// eine 0 (Amtsholung) vorangestellt – egal ob sie mit 0 oder 1–9 beginnen.
    /// Interne Nebenstellen (bis 4 Stellen) bleiben unverändert.
    static func callback(_ number: String, prefixZero: Bool) -> String {
        let n = number.trimmingCharacters(in: .whitespaces)
        if n.hasPrefix("+") { return "+" + n.dropFirst().filter { $0.isNumber } }
        let digits = n.filter { "0123456789*#".contains($0) }
        guard prefixZero, digits.count > 4, digits.allSatisfy({ $0.isNumber }) else { return digits.isEmpty ? n : digits }
        return "0" + digits
    }
}
