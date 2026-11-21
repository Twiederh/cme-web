import SwiftUI

struct MacMainView: View {
    enum Section: String, CaseIterable, Identifiable {
        case dial = "Wählen", desk = "Tischtelefon", log = "Protokoll"
        var id: String { rawValue }
        var icon: String {
            switch self {
            case .dial: return "circle.grid.3x3.fill"
            case .desk: return "desktopcomputer.and.arrow.down"
            case .log: return "text.alignleft"
            }
        }
    }

    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @State private var selection: Section? = .dial

    var body: some View {
        NavigationSplitView {
            List(Section.allCases, selection: $selection) { s in
                Label(s.rawValue, systemImage: s.icon).tag(s)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190)
            .safeAreaInset(edge: .bottom) {
                VStack(alignment: .leading, spacing: 6) {
                    MacRegistrationBadge(status: sip.registration)
                    if settings.macMode == .deskPhone {
                        Label("\(settings.deskPhoneExtension): \(desk.status.state.label)", systemImage: "phone")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } detail: {
            VStack(spacing: 0) {
                if !sip.calls.isEmpty {
                    MacCallView()
                    Divider()
                }
                switch selection ?? .dial {
                case .dial: MacDialerView()
                case .desk: DeskPhoneView()
                case .log: MacLogView()
                }
            }
        }
        .toolbar {
            ToolbarItem(placement: .principal) {
                Picker("Modus", selection: $settings.macMode) {
                    Text("Softphone").tag(MacMode.softphone)
                    Text("Tischtelefon").tag(MacMode.deskPhone)
                }
                .pickerStyle(.segmented)
                .help("Softphone: der Mac telefoniert. Tischtelefon: der Mac steuert dein Cisco-Telefon.")
            }
        }
        .onChange(of: settings.macMode) { _, mode in
            if mode == .deskPhone { desk.startMonitoring(); selection = .desk } else { desk.stopMonitoring() }
        }
    }
}

struct MacRegistrationBadge: View {
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

struct MacLogView: View {
    @EnvironmentObject private var sip: SIPManager
    var body: some View {
        List(Array(sip.lastLog.enumerated().reversed()), id: \.offset) { _, line in
            Text(line).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
        }
        .toolbar {
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(sip.lastLog.joined(separator: "\n"), forType: .string)
            } label: { Label("Kopieren", systemImage: "doc.on.doc") }
        }
    }
}
