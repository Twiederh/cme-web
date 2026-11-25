import Foundation
import AVFoundation
import SwiftUI

/// Voicemail und Gesprächsaufnahmen der Nebenstelle (aus cme-web) plus lokale Aufnahmen dieses Geräts:
/// Liste, Abspielen, als gehört markieren, Löschen. Gemeinsam für iOS (Startbildschirm) und Mac (Menüleiste).
@MainActor
final class MediaLibrary: NSObject, ObservableObject, AVAudioPlayerDelegate {
    static let shared = MediaLibrary()

    struct Item: Identifiable, Equatable {
        enum Kind: Equatable {
            case voicemail(box: String, folder: String, file: String)
            case server(id: String)          // cme-recorder (SIPREC), extern
            case local(URL)                  // Aufnahme in der App (interne Gespräche)
        }
        let id: String
        let kind: Kind
        let number: String
        let name: String
        let date: Date
        let duration: Int
        var isNew = false
        var incoming = true
        var transcript = ""
        var title: String { name.isEmpty ? (number.isEmpty ? "Unbekannt" : number) : name }
        var dialable: String { number.filter { $0.isNumber || $0 == "+" } }
    }

    @Published private(set) var voicemails: [Item] = []
    @Published private(set) var recordings: [Item] = []
    @Published private(set) var loading = false
    @Published private(set) var error = ""
    @Published private(set) var playing: String?
    @Published private(set) var downloading: String?

    var newVoicemails: Int { voicemails.filter(\.isNew).count }

    private var player: AVAudioPlayer?
    private var lastRefresh = Date.distantPast

    private var ext: String { PhoneSettings.shared.extensionNumber }

    /// Neu laden (höchstens alle 5 s, außer force)
    func refresh(force: Bool = false) {
        guard force || Date().timeIntervalSince(lastRefresh) > 5, !loading else { return }
        lastRefresh = Date()
        loading = true
        Task { @MainActor in
            defer { loading = false }
            var err = ""
            if PhoneSettings.shared.hasCmeWeb, !ext.isEmpty {
                do {
                    voicemails = try await ClientAPI.voicemail(ext: ext).map { m in
                        Item(id: "vm-\(m.box)-\(m.folder)-\(m.file)",
                             kind: .voicemail(box: m.box, folder: m.folder, file: m.file),
                             number: m.caller, name: m.callerName, date: Date(timeIntervalSince1970: TimeInterval(m.time)),
                             duration: m.duration, isNew: m.isNew)
                    }
                } catch { err = "Voicemail: \(error.localizedDescription)" }
                do {
                    let server = try await ClientAPI.recordings(ext: ext).map { r in
                        Item(id: "rec-\(r.id)", kind: .server(id: r.id), number: r.external, name: r.name,
                             date: Date(timeIntervalSince1970: r.start), duration: Int(r.duration.rounded()),
                             incoming: r.direction != "out", transcript: r.transcript)
                    }
                    recordings = (server + localRecordings()).sorted { $0.date > $1.date }
                } catch {
                    recordings = localRecordings()
                    if err.isEmpty { err = "Aufnahmen: \(error.localizedDescription)" }
                }
            } else {
                voicemails = []
                recordings = localRecordings()
            }
            error = err
        }
    }

    /// Lokale Aufnahmen: "<yyyy-MM-dd HH.mm.ss> <Nummer>.wav" im Aufnahme-Ordner der App
    private func localRecordings() -> [Item] {
        let dir = SIPManager.recordingsFolder
        let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.creationDateKey]))
            ?? []
        let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd HH.mm.ss"
        return files.filter { $0.pathExtension.lowercased() == "wav" }.compactMap { url in
            let name = url.deletingPathExtension().lastPathComponent
            let date = df.date(from: String(name.prefix(19)))
                ?? (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
            let number = name.count > 20 ? String(name.dropFirst(20)) : ""
            let secs = (try? AVAudioPlayer(contentsOf: url).duration).map { Int($0.rounded()) } ?? 0
            return Item(id: "local-\(name)", kind: .local(url), number: number == "Anruf" ? "" : number, name: "",
                        date: date, duration: secs)
        }
    }

    // MARK: Abspielen

    func togglePlay(_ item: Item) {
        if playing == item.id { stop(); return }
        stop()
        Task { @MainActor in
            do {
                let url = try await audioFile(item)
                #if os(iOS)
                // Während eines Gesprächs gehört die Audio-Session CallKit – dann nicht umstellen
                if SIPManager.shared.calls.isEmpty {
                    try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
                    try? AVAudioSession.sharedInstance().setActive(true)
                }
                #endif
                let p = try AVAudioPlayer(contentsOf: url)
                p.delegate = self
                p.play()
                player = p
                playing = item.id
                if item.isNew { setHeard(item, heard: true) }
            } catch {
                self.error = "Abspielen: \(error.localizedDescription)"
            }
        }
    }

    func stop() {
        player?.stop()
        player = nil
        playing = nil
    }

    nonisolated func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in if self.player === p { self.stop() } }
    }

    /// Lokale Datei zum Abspielen: lokale Aufnahme direkt, sonst einmal von cme-web laden (Cache-Ordner)
    private func audioFile(_ item: Item) async throws -> URL {
        let cache = FileManager.default.temporaryDirectory.appendingPathComponent("cme-media", isDirectory: true)
        try? FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
        switch item.kind {
        case .local(let url):
            return url
        case .voicemail(let box, let folder, let file):
            let url = cache.appendingPathComponent("\(item.id).wav")
            if !FileManager.default.fileExists(atPath: url.path) {
                downloading = item.id
                defer { downloading = nil }
                let m = ClientAPI.VoicemailMessage(box: box, folder: folder, file: file, time: 0, duration: 0,
                                                   caller: "", callerName: "", isNew: false)
                try await ClientAPI.voicemailAudio(ext: ext, m).write(to: url)
            }
            return url
        case .server(let id):
            let url = cache.appendingPathComponent("\(item.id).mp3")
            if !FileManager.default.fileExists(atPath: url.path) {
                downloading = item.id
                defer { downloading = nil }
                try await ClientAPI.recordingAudio(ext: ext, id: id).write(to: url)
            }
            return url
        }
    }

    // MARK: Gehört / Löschen

    func setHeard(_ item: Item, heard: Bool) {
        guard case .voicemail(let box, let folder, let file) = item.kind else { return }
        if let i = voicemails.firstIndex(of: item) { voicemails[i].isNew = !heard }
        Task { @MainActor in
            let m = ClientAPI.VoicemailMessage(box: box, folder: folder, file: file, time: 0, duration: 0,
                                               caller: "", callerName: "", isNew: !heard)
            do { try await ClientAPI.setVoicemailHeard(ext: ext, m, heard: heard) }
            catch { self.error = "Voicemail: \(error.localizedDescription)" }
            // Asterisk verschiebt die Nachricht (INBOX <-> Old): neuen Dateinamen holen
            refresh(force: true)
        }
    }

    func delete(_ item: Item) {
        if playing == item.id { stop() }
        voicemails.removeAll { $0.id == item.id }
        recordings.removeAll { $0.id == item.id }
        Task { @MainActor in
            do {
                switch item.kind {
                case .voicemail(let box, let folder, let file):
                    let m = ClientAPI.VoicemailMessage(box: box, folder: folder, file: file, time: 0, duration: 0,
                                                       caller: "", callerName: "", isNew: false)
                    try await ClientAPI.deleteVoicemail(ext: ext, m)
                case .server(let id):
                    try await ClientAPI.deleteRecording(ext: ext, id: id)
                case .local(let url):
                    try FileManager.default.removeItem(at: url)
                }
            } catch {
                self.error = "Löschen: \(error.localizedDescription)"
            }
            refresh(force: true)
        }
    }
}

/// Liste für Voicemail oder Aufnahmen. onCall: Rückruf (iOS über CallKit, Mac über die Wähltastatur).
struct MediaListView: View {
    enum Source { case voicemail, recordings }
    let source: Source
    var onCall: ((String) -> Void)?
    @ObservedObject private var lib = MediaLibrary.shared
    @State private var expanded: String?

    private var items: [MediaLibrary.Item] { source == .voicemail ? lib.voicemails : lib.recordings }

    var body: some View {
        Group {
            if items.isEmpty {
                VStack(spacing: 6) {
                    Text(lib.loading ? "Lädt …" : (source == .voicemail ? "Keine Nachrichten" : "Keine Aufnahmen"))
                        .foregroundStyle(.secondary)
                    if !lib.error.isEmpty { Text(lib.error).font(.caption).foregroundStyle(.red) }
                }
                .frame(maxWidth: .infinity, minHeight: 60)
            } else {
                List {
                    ForEach(items) { item in
                        row(item)
                            #if os(iOS)
                            .swipeActions {
                                Button(role: .destructive) { lib.delete(item) } label: { Label("Löschen", systemImage: "trash") }
                            }
                            #endif
                            .contextMenu { menu(item) }
                    }
                    if !lib.error.isEmpty { Text(lib.error).font(.caption).foregroundStyle(.red) }
                }
                #if os(macOS)
                .listStyle(.plain)
                #endif
                .refreshable { lib.refresh(force: true) }
            }
        }
        .onAppear { lib.refresh() }
    }

    private func row(_ item: MediaLibrary.Item) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Button { lib.togglePlay(item) } label: {
                    Image(systemName: lib.playing == item.id ? "stop.circle.fill" : "play.circle.fill")
                        .font(.title2)
                        .foregroundStyle(lib.playing == item.id ? Color.red : Color.accentColor)
                }
                .buttonStyle(.plain)
                .disabled(lib.downloading == item.id)
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 4) {
                        if item.isNew { Circle().fill(.blue).frame(width: 7, height: 7) }
                        Text(item.title).fontWeight(item.isNew ? .semibold : .regular).lineLimit(1)
                    }
                    Text(subtitle(item)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if lib.downloading == item.id { ProgressView().controlSize(.small) }
                #if os(macOS)
                Button { lib.delete(item) } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).help("Löschen")
                #endif
            }
            if !item.transcript.isEmpty {
                Text(item.transcript)
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(expanded == item.id ? nil : 2)
                    .onTapGesture { expanded = expanded == item.id ? nil : item.id }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder private func menu(_ item: MediaLibrary.Item) -> some View {
        if let onCall, !item.dialable.isEmpty {
            Button { onCall(item.dialable) } label: { Label("Rückruf", systemImage: "phone") }
        }
        if case .voicemail = item.kind {
            Button { lib.setHeard(item, heard: item.isNew) } label: {
                Label(item.isNew ? "Als gehört markieren" : "Als neu markieren", systemImage: "envelope.badge")
            }
        }
        Button(role: .destructive) { lib.delete(item) } label: { Label("Löschen", systemImage: "trash") }
    }

    private func subtitle(_ item: MediaLibrary.Item) -> String {
        let when: String
        if Calendar.current.isDateInToday(item.date) { when = item.date.formatted(date: .omitted, time: .shortened) }
        else if Calendar.current.isDateInYesterday(item.date) {
            when = "gestern " + item.date.formatted(date: .omitted, time: .shortened)
        } else { when = item.date.formatted(.dateTime.day().month(.twoDigits).hour().minute()) }
        let dur = item.duration >= 60 ? "\(item.duration / 60):\(String(format: "%02d", item.duration % 60)) min"
            : "\(item.duration) s"
        var parts = [when, dur]
        if case .local = item.kind { parts.append("auf diesem Gerät") }
        else if case .server = item.kind { parts.append(item.incoming ? "eingehend" : "abgehend") }
        if !item.name.isEmpty, !item.number.isEmpty { parts.insert(item.number, at: 0) }
        return parts.joined(separator: " · ")
    }
}
