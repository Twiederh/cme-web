import SwiftUI
import AppKit
import Combine

/// Symbol in der Menüleiste (AppKit-NSStatusItem, immer sichtbar) mit Popover:
/// Status · Softphone/Telefonsteuerung · Wähltastatur · Anrufliste · Einstellungen.
@MainActor
final class StatusBarController: NSObject, NSPopoverDelegate {
    static let shared = StatusBarController()

    private var item: NSStatusItem?
    private let popover = NSPopover()
    private var cancellables = Set<AnyCancellable>()

    func setup() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.target = self
        item.button?.action = #selector(toggle(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        item.isVisible = true
        self.item = item

        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        popover.contentViewController = NSHostingController(rootView: MenuPanel()
            .environmentObject(SIPManager.shared)
            .environmentObject(PhoneSettings.shared)
            .environmentObject(DeskPhoneController.shared)
            .environmentObject(CallHistory.shared))

        // Symbol je nach Zustand
        Publishers.CombineLatest4(SIPManager.shared.$registration, SIPManager.shared.$calls,
                                  PhoneSettings.shared.$macMode, DeskPhoneController.shared.$status)
            .receive(on: RunLoop.main)
            .sink { [weak self] reg, calls, mode, desk in
                MainActor.assumeIsolated { self?.updateIcon(reg: reg, calls: calls, mode: mode, desk: desk) }
            }
            .store(in: &cancellables)
    }

    private func updateIcon(reg: SIPManager.RegistrationStatus, calls: [SIPManager.CallInfo], mode: MacMode, desk: LineStatus) {
        var name = "phone"
        var tip = "CME Phone"
        if !calls.isEmpty || (mode == .deskPhone && [.busy, .dialing].contains(desk.state)) {
            name = "phone.connection.fill"; tip = "CME Phone – im Gespräch"
        } else if mode == .deskPhone && desk.state == .ringing {
            name = "phone.badge.waveform.fill"
        } else if case .ok = reg {
            name = mode == .deskPhone ? "phone.arrow.up.right.fill" : "phone.fill"
            tip = "CME Phone – angemeldet"
        } else {
            tip = "CME Phone – \(reg.label)"
        }
        let img = NSImage(systemSymbolName: name, accessibilityDescription: tip)
            ?? NSImage(systemSymbolName: "phone", accessibilityDescription: tip)
        img?.isTemplate = true
        item?.button?.image = img
        item?.button?.toolTip = tip
        // nicht angemeldet: leicht ausgegraut
        if case .ok = reg { item?.button?.appearsDisabled = false } else { item?.button?.appearsDisabled = calls.isEmpty }
    }

    @objc private func toggle(_ sender: Any?) {
        popover.isShown ? close() : show()
    }

    func show(number: String? = nil) {
        guard let button = item?.button else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        if let number { NotificationCenter.default.post(name: .cmePrefillNumber, object: number) }
    }

    func close() { popover.performClose(nil) }
}

struct MenuPanel: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @EnvironmentObject private var history: CallHistory
    @State private var showLog = false

    var body: some View {
        VStack(spacing: 10) {
            // Kopf: Status + Einstellungen/Beenden
            HStack(spacing: 8) {
                Circle().fill(statusColor).frame(width: 8, height: 8)
                Text(statusLine).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Button { StatusBarController.shared.close(); SettingsWindow.shared.show() } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.borderless).help("Einstellungen (⌘,)")
                .keyboardShortcut(",")
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }
                    .buttonStyle(.borderless).help("CME Phone beenden – \(BuildInfo.text)")
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)

            Picker("", selection: $settings.macMode) {
                Text("Softphone").tag(MacMode.softphone)
                Text("Telefonsteuerung").tag(MacMode.deskPhone)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 14)

            DialpadView()

            Divider()

            // Anrufliste
            HStack {
                Text("Anrufliste").font(.headline)
                Spacer()
            }
            .padding(.horizontal, 14)

            if history.entries.isEmpty {
                Text("Keine Anrufe").font(.callout).foregroundStyle(.secondary).padding(.bottom, 12)
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(history.entries.prefix(30)) { e in
                            HistoryRow(entry: e) { callBack(e) }
                        }
                    }
                }
                .frame(height: min(CGFloat(history.entries.count), 5) * 40)
                HStack {
                    Spacer()
                    Button("Liste leeren") { history.clear() }.buttonStyle(.borderless).font(.caption)
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 10)
            }
        }
        .frame(width: 300)
    }

    private func callBack(_ e: CallHistory.Entry) {
        StatusBarController.shared.close()
        URLRouter.dial(e.number)
    }

    private var statusColor: Color {
        switch sip.registration {
        case .ok: return .green
        case .progress: return .orange
        case .failed: return .red
        default: return .gray
        }
    }

    private var statusLine: String {
        switch settings.macMode {
        case .softphone:
            return "\(settings.extensionNumber.isEmpty ? "Softphone" : settings.extensionNumber) · \(sip.registration.label)"
        case .deskPhone:
            let ext = settings.deskPhoneExtension.isEmpty ? "?" : settings.deskPhoneExtension
            if case .ok = sip.registration { return "Tischtelefon \(ext) · \(desk.status.state.label)" }
            return "Tischtelefon \(ext) · \(sip.registration.label)"
        }
    }
}

private struct HistoryRow: View {
    let entry: CallHistory.Entry
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: entry.symbol)
                    .foregroundStyle(entry.direction == .missed ? .red : .secondary)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 1) {
                    Text(entry.title).lineLimit(1)
                        .foregroundStyle(entry.direction == .missed ? .red : .primary)
                    Text(entry.viaDeskPhone ? "Tischtelefon" : (entry.name.isEmpty ? "" : entry.number))
                        .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                Text(Self.when(entry.date)).font(.caption).foregroundStyle(.secondary)
                Image(systemName: "phone.fill").font(.caption).foregroundStyle(.green).opacity(hover ? 1 : 0)
            }
            .padding(.horizontal, 14)
            .frame(height: 40)
            .background(hover ? Color.primary.opacity(0.06) : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Rückruf")
    }

    static func when(_ d: Date) -> String {
        if Calendar.current.isDateInToday(d) { return d.formatted(date: .omitted, time: .shortened) }
        if Calendar.current.isDateInYesterday(d) { return "gestern" }
        return d.formatted(.dateTime.day().month(.twoDigits))
    }
}
