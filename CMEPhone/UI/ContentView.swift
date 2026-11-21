import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings

    var body: some View {
        TabView {
            DialerView()
                .tabItem { Label("Wählen", systemImage: "circle.grid.3x3.fill") }
            SettingsView()
                .tabItem { Label("Einstellungen", systemImage: "gearshape") }
            LogView()
                .tabItem { Label("Protokoll", systemImage: "text.alignleft") }
        }
        .fullScreenCover(isPresented: Binding(get: { !sip.calls.isEmpty }, set: { _ in })) {
            ActiveCallView()
                .environmentObject(sip)
                .environmentObject(settings)
        }
    }
}

struct RegistrationBadge: View {
    let status: SIPManager.RegistrationStatus
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(status.label).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }
    private var color: Color {
        switch status {
        case .ok: return .green
        case .progress: return .orange
        case .failed: return .red
        default: return .gray
        }
    }
}

struct LogView: View {
    @EnvironmentObject private var sip: SIPManager
    var body: some View {
        NavigationStack {
            List(Array(sip.lastLog.enumerated().reversed()), id: \.offset) { _, line in
                Text(line).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
            }
            .navigationTitle("Protokoll")
            .toolbar {
                ShareLink(item: sip.lastLog.joined(separator: "\n")) { Image(systemName: "square.and.arrow.up") }
            }
        }
    }
}
