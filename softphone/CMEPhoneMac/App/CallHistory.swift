import Foundation
import Combine
import linphonesw

/// Anrufliste der Mac-App (Softphone und Tischtelefon) – für Wahlwiederholung und Rückruf aus dem Menü.
@MainActor
final class CallHistory: ObservableObject {
    static let shared = CallHistory()

    struct Entry: Codable, Identifiable, Equatable {
        enum Direction: String, Codable { case incoming, outgoing, missed }
        var id = UUID()
        var date: Date
        var number: String
        var name: String
        var direction: Direction
        var viaDeskPhone: Bool
        var duration: Int

        var title: String { name.isEmpty ? number : name }
        var symbol: String {
            switch direction {
            case .incoming: return "phone.arrow.down.left"
            case .outgoing: return "phone.arrow.up.right"
            case .missed: return "phone.down"
            }
        }
    }

    @Published private(set) var entries: [Entry] = []
    private let maxEntries = 40
    private let url: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("CME Phone", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("anrufliste.json")
    }()

    // Zustand für die Auswertung
    private var connected = Set<String>()
    private var deskDialog: (id: String, number: String, name: String, incoming: Bool, answered: Bool, start: Date)?
    /// Vom Mac aus am Tischtelefon gewählt: Eintrag sofort, Dauer kommt mit dem Statusabo nach
    private var deskDialEntry: (id: UUID, date: Date)?

    func noteDeskDial(_ number: String) {
        let e = Entry(date: Date(), number: number, name: "", direction: .outgoing, viaDeskPhone: true, duration: 0)
        add(e)
        deskDialEntry = (e.id, e.date)
    }

    private init() {
        if let data = try? Data(contentsOf: url),
           let list = try? JSONDecoder().decode([Entry].self, from: data) { entries = list }
    }

    /// Letzte gewählte Nummer (Wahlwiederholung)
    var lastDialed: Entry? { entries.first { $0.direction == .outgoing } }

    func clear() { entries = []; persist() }

    func add(_ e: Entry) {
        guard !e.number.isEmpty else { return }
        entries.insert(e, at: 0)
        if entries.count > maxEntries { entries.removeLast(entries.count - maxEntries) }
        persist()
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(entries) { try? data.write(to: url, options: .atomic) }
    }

    // MARK: Softphone (Aufruf aus SIPManager-Callback)

    func softphone(call: Call, state: Call.State) {
        let id = call.callLog?.callId ?? ""
        switch state {
        case .Connected, .StreamsRunning:
            connected.insert(id)
        case .Released:
            let incoming = call.dir == .Incoming
            let wasConnected = connected.remove(id) != nil
            let number = call.remoteAddress?.username ?? ""
            let dir: Entry.Direction = incoming ? (wasConnected ? .incoming : .missed) : .outgoing
            add(Entry(date: call.callLog?.startDate.map { Date(timeIntervalSince1970: TimeInterval($0)) } ?? Date(),
                      number: number, name: call.remoteAddress?.displayName ?? "",
                      direction: dir, viaDeskPhone: false, duration: call.callLog?.duration ?? 0))
        default: break
        }
    }

    // MARK: Tischtelefon (Statusabo)

    func deskPhone(old: LineStatus, new: LineStatus) {
        let key = new.dialogId ?? new.remoteNumber ?? ""
        switch new.state {
        case .ringing, .dialing:
            if deskDialog == nil || deskDialog?.id != key {
                finishDesk()
                deskDialog = (key, new.remoteNumber ?? "", new.remoteName ?? "", new.state == .ringing, false, Date())
            }
        case .busy:
            if deskDialog == nil {
                deskDialog = (key, new.remoteNumber ?? "", new.remoteName ?? "", false, true, Date())
            } else {
                deskDialog?.answered = true
                if let n = new.remoteNumber, deskDialog?.number.isEmpty == true { deskDialog?.number = n }
            }
        case .idle:
            finishDesk()
        case .unknown:
            break
        }
    }

    private func finishDesk() {
        guard let d = deskDialog else { return }
        deskDialog = nil
        let dir: Entry.Direction = d.incoming ? (d.answered ? .incoming : .missed) : .outgoing
        let duration = d.answered ? Int(Date().timeIntervalSince(d.start)) : 0
        if dir == .outgoing, let pending = deskDialEntry, Date().timeIntervalSince(pending.date) < 3 * 3600,
           let i = entries.firstIndex(where: { $0.id == pending.id }) {
            // gehört zum Wählen aus der App: nur Dauer/Name nachtragen
            deskDialEntry = nil
            entries[i].duration = duration
            if entries[i].name.isEmpty { entries[i].name = d.name }
            persist()
            return
        }
        add(Entry(date: d.start, number: d.number, name: d.name, direction: dir, viaDeskPhone: true, duration: duration))
    }
}
