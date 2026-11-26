import SwiftUI

/// Anrufliste der App (eigene Gespräche über CME Phone): antippen ruft zurück, Wischen löscht.
/// Auf dem iPhone gibt es zusätzlich „Zuletzt“ in der Telefon-App, auf iPad und Vision Pro nur diese Liste.
struct CallHistoryView: View {
    @ObservedObject private var history = CallHistory.shared
    let onCall: (String) -> Void
    @State private var confirmClear = false

    var body: some View {
        Group {
            if history.entries.isEmpty {
                Text("Keine Anrufe").foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    ForEach(history.entries) { e in
                        Button { onCall(e.number) } label: { row(e) }
                            .swipeActions {
                                Button(role: .destructive) { history.remove(e) } label: { Label("Löschen", systemImage: "trash") }
                            }
                    }
                }
            }
        }
        .toolbar {
            if !history.entries.isEmpty {
                Button("Leeren") { confirmClear = true }
            }
        }
        .confirmationDialog("Anrufliste leeren?", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("Leeren", role: .destructive) { history.clear() }
        }
    }

    private func row(_ e: CallHistory.Entry) -> some View {
        HStack(spacing: 12) {
            Image(systemName: e.symbol)
                .foregroundStyle(e.direction == .missed ? .red : .secondary)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(e.title).foregroundStyle(e.direction == .missed ? .red : .primary).lineLimit(1)
                Text(subtitle(e)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Text(when(e.date)).font(.caption).foregroundStyle(.secondary)
            Image(systemName: "phone.fill").font(.caption).foregroundStyle(.green)
        }
        .contentShape(Rectangle())
    }

    private func subtitle(_ e: CallHistory.Entry) -> String {
        var parts: [String] = []
        if !e.name.isEmpty { parts.append(e.number) }
        switch e.direction {
        case .missed: parts.append("verpasst")
        case .incoming: parts.append("eingehend")
        case .outgoing: parts.append("abgehend")
        }
        if e.duration > 0 { parts.append(String(format: "%d:%02d", e.duration / 60, e.duration % 60)) }
        return parts.joined(separator: " · ")
    }

    private func when(_ d: Date) -> String {
        if Calendar.current.isDateInToday(d) { return d.formatted(date: .omitted, time: .shortened) }
        if Calendar.current.isDateInYesterday(d) { return "gestern" }
        return d.formatted(.dateTime.day().month(.twoDigits))
    }
}
