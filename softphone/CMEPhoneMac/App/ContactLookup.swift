import Foundation
import AppKit
import Contacts

/// Name, Bild und Nummernart ("mobil") des Anrufers aus der Kontakte-App.
@MainActor
final class ContactLookup {
    static let shared = ContactLookup()

    struct Match {
        var name: String
        var label: String
        var image: NSImage?
    }

    private let store = CNContactStore()
    private var cache: [String: Match?] = [:]
    private var asked = false

    func lookup(_ number: String) async -> Match? {
        let key = number.filter { $0.isNumber || $0 == "+" }
        guard key.count >= 3 else { return nil }
        if let hit = cache[key] { return hit }
        guard await authorized() else { return nil }
        let m = await Task.detached(priority: .userInitiated) { [store] in Self.search(store, key) }.value
        cache[key] = m
        return m
    }

    private func authorized() async -> Bool {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return true
        case .notDetermined:
            guard !asked else { return false }
            asked = true
            return (try? await store.requestAccess(for: .contacts)) ?? false
        default:
            return false
        }
    }

    /// Nummer wie von CME geliefert: "0711…", "+49711…", mit Amtsholung "00711…" oder "0+49…".
    nonisolated private static func candidates(_ n: String) -> [String] {
        var c = [n]
        if n.hasPrefix("0+") { c.append(String(n.dropFirst())) }
        if n.hasPrefix("00"), !n.hasPrefix("000"), n.count > 6 { c.append(String(n.dropFirst())) }   // Amtsholung 0 + 0711…
        if n.hasPrefix("+49") { c.append("0" + n.dropFirst(3)) }
        if n.hasPrefix("0049") { c.append("0" + n.dropFirst(4)) }
        return c
    }

    nonisolated private static func search(_ store: CNContactStore, _ number: String) -> Match? {
        let keys: [CNKeyDescriptor] = [
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactThumbnailImageDataKey as CNKeyDescriptor,
            CNContactImageDataAvailableKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
        ]
        for cand in candidates(number) {
            let pred = CNContact.predicateForContacts(matching: CNPhoneNumber(stringValue: cand))
            guard let contact = try? store.unifiedContacts(matching: pred, keysToFetch: keys).first else { continue }
            var name = CNContactFormatter.string(from: contact, style: .fullName) ?? ""
            if name.isEmpty { name = contact.organizationName }
            let digits = cand.filter { $0.isNumber }
            let lv = contact.phoneNumbers.first { $0.value.stringValue.filter { $0.isNumber }.hasSuffix(String(digits.suffix(6))) }
            let label = lv?.label.map { CNLabeledValue<CNPhoneNumber>.localizedString(forLabel: $0) } ?? ""
            let img = contact.thumbnailImageData.flatMap { NSImage(data: $0) }
            return Match(name: name, label: label, image: img)
        }
        return nil
    }
}
