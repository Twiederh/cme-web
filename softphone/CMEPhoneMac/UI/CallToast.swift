import SwiftUI
import AppKit
import Combine
import linphonesw

/// Call-Toast unten rechts (wie die Anrufanzeige von macOS/FaceTime):
/// Bild, Name, Nummernart bzw. Dauer und runde Knöpfe.
///   eingehend:  Voicemail · Ablehnen · Annehmen
///   Gespräch:   Aufnahme  · Auflegen
///   ausgehend:  Auflegen
/// Für Softphone-Anrufe und – bei Telefonsteuerung – für das Tischtelefon.
@MainActor
final class ToastController: ObservableObject {
    static let shared = ToastController()

    enum Phase: Equatable { case incoming, outgoing, active }
    enum Source: Equatable { case softphone(String), deskPhone }

    struct Toast: Equatable {
        var source: Source
        var phase: Phase
        var number: String
        var name: String
        var since: Date?
        /// Softphone-Gespräch mit Video: Bild als oberer Anhang des Toasts
        var video = false
        /// Telefonsteuerung: eingehender Anruf auf der Shared Line des Macs – Annehmen am Tischtelefon
        var viaDesk = false
        var key: String { switch source { case .softphone(let id): return id; case .deskPhone: return "desk" } }
    }

    @Published private(set) var toast: Toast?
    @Published private(set) var contact: ContactLookup.Match?
    @Published var dismissed = Set<String>()

    private var panel: NSPanel?
    private var cancellables = Set<AnyCancellable>()
    private var lookupFor = ""
    /// Anrufer des letzten Shared-Line-Anrufs – presence meldet nur frei/belegt, nicht wer anruft
    private var deskCaller: (number: String, name: String)?

    private var sip: SIPManager { .shared }
    private var desk: DeskPhoneController { .shared }
    private var settings: PhoneSettings { .shared }

    func setup() {
        Publishers.CombineLatest3(sip.$calls, desk.$status, settings.$macMode)
            .receive(on: RunLoop.main)
            .sink { [weak self] calls, status, mode in
                MainActor.assumeIsolated { self?.update(calls: calls, desk: status, mode: mode) }
            }
            .store(in: &cancellables)
    }

    private func update(calls: [SIPManager.CallInfo], desk status: LineStatus, mode: MacMode) {
        var t: Toast?
        // Softphone: eingehend vor laufend vor ausgehend (beendete Anrufe nicht mehr zeigen)
        let live = calls.filter { ![.End, .Error, .Released].contains($0.state) }
        let pick = live.first { $0.isIncoming && Self.isRinging($0.state) } ?? live.first { !Self.isRinging($0.state) } ?? live.first
        if status.state == .idle { deskCaller = nil }
        if mode == .deskPhone, let c = pick, c.isIncoming, Self.isRinging(c.state) {
            // Telefonsteuerung: Der Mac klingelt (stumm) über die Shared Line mit – er kennt den Anrufer,
            // angenommen wird am Tischtelefon. Nimmt das Tischtelefon an, beendet CME den Mac-Zweig.
            deskCaller = (c.remote, c.displayName)
            t = Toast(source: .softphone(c.id), phase: .incoming, number: c.remote, name: c.displayName, viaDesk: true)
        } else if let c = pick {
            let phase: Phase = Self.isRinging(c.state) ? (c.isIncoming ? .incoming : .outgoing) : .active
            let since = phase == .active ? Date().addingTimeInterval(-TimeInterval(c.duration)) : nil
            t = Toast(source: .softphone(c.id), phase: phase, number: c.remote, name: c.displayName,
                      since: toast?.key == c.id && toast?.phase == phase ? toast?.since : since,
                      video: phase == .active && c.hasVideo)
        } else if mode == .deskPhone, settings.deskPhoneNotify {
            switch status.state {
            case .ringing:
                t = Toast(source: .deskPhone, phase: .incoming, number: status.remoteNumber ?? "", name: status.remoteName ?? "")
            case .dialing:
                t = Toast(source: .deskPhone, phase: .outgoing, number: status.remoteNumber ?? "", name: status.remoteName ?? "")
            case .busy:
                let keep = toast?.source == .deskPhone && toast?.phase == .active ? toast?.since : Date()
                t = Toast(source: .deskPhone, phase: .active, number: status.remoteNumber ?? deskCaller?.number ?? toast?.number ?? "",
                          name: status.remoteName ?? deskCaller?.name ?? toast?.name ?? "", since: keep)
            default: break
            }
        }
        guard let t else {
            dismissed.removeAll()
            show(nil)
            return
        }
        if dismissed.contains(t.key + "\(t.phase)") { hide(); return }
        show(t)
    }

    /// Nur ausblenden (der Anruf läuft weiter, z.B. Tischtelefon klingelt).
    private func hide() { panel?.orderOut(nil) }

    private static func isRinging(_ s: linphonesw.Call.State) -> Bool {
        switch s {
        case .IncomingReceived, .PushIncomingReceived, .IncomingEarlyMedia,
             .OutgoingInit, .OutgoingProgress, .OutgoingRinging, .OutgoingEarlyMedia: return true
        default: return false
        }
    }

    private func show(_ t: Toast?) {
        let changedCall = t?.key != toast?.key
        toast = t
        guard let t else { RecordingController.shared.stop(); panel?.orderOut(nil); return }
        if changedCall || lookupFor != t.number {
            lookupFor = t.number
            contact = nil
            let number = t.number
            Task { @MainActor in
                let m = await ContactLookup.shared.lookup(number)
                if self.lookupFor == number { self.contact = m }
            }
        }
        if t.phase == .active {
            switch t.source {
            case .softphone(let id): RecordingController.shared.track(ext: settings.extensionNumber, localCallId: id)
            case .deskPhone: RecordingController.shared.track(ext: settings.deskPhoneExtension, localCallId: nil)
            }
        }
        presentPanel()
    }

    // MARK: Fenster

    static let width: CGFloat = 380
    static let toastHeight: CGFloat = 84
    static let videoHeight: CGFloat = 214      // 380 × 214 ≈ 16:9

    private func presentPanel() {
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 380, height: 84),
                            styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
            p.isFloatingPanel = true
            p.level = .statusBar
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            p.backgroundColor = .clear
            p.isOpaque = false
            p.hasShadow = true
            p.hidesOnDeactivate = false
            p.isMovableByWindowBackground = true
            let host = NSHostingView(rootView: CallToastView().environmentObject(self).environmentObject(RecordingController.shared)
                .environmentObject(PhoneSettings.shared))
            host.autoresizingMask = [.width, .height]
            host.frame = p.contentRect(forFrameRect: p.frame)
            p.contentView = host
            panel = p
        }
        guard let panel, let screen = NSScreen.main else { return }
        let height = Self.toastHeight + (toast?.video == true ? Self.videoHeight + 8 : 0)
        if !panel.isVisible {
            let f = screen.visibleFrame
            panel.setFrame(NSRect(x: f.maxX - Self.width - 16, y: f.minY + 16, width: Self.width, height: height), display: true)
        } else if abs(panel.frame.height - height) > 0.5 {
            // unten verankert wachsen/schrumpfen (Video kommt oben dazu)
            var fr = panel.frame
            fr.size.height = height
            panel.setFrame(fr, display: true, animate: true)
        }
        panel.orderFrontRegardless()
    }

    // MARK: Aktionen

    func answer() {
        guard let t = toast else { return }
        if t.viaDesk { desk.answer(); return }
        switch t.source {
        case .softphone(let id): CallCenter.shared.requestAnswer(callId: id)
        case .deskPhone: desk.answer()
        }
    }

    func hangup() {
        guard let t = toast else { return }
        switch t.source {
        case .softphone(let id): sip.terminate(callId: id)
        case .deskPhone: desk.hangup()
        }
    }

    func decline() {
        guard let t = toast else { return }
        if t.viaDesk {
            desk.decline()
            dismissed.insert(t.key + "\(t.phase)")
            hide()
            return
        }
        switch t.source {
        case .softphone(let id): sip.decline(callId: id, toVoicemail: false)
        case .deskPhone:
            desk.decline()
            dismissed.insert(t.key + "\(t.phase)")
            hide()
        }
    }

    func voicemail() {
        guard let t = toast else { return }
        if t.viaDesk {
            desk.execute(settings.keyVoicemail)
            dismissed.insert(t.key + "\(t.phase)")
            hide()
            return
        }
        switch t.source {
        case .softphone(let id): sip.decline(callId: id, toVoicemail: true)
        case .deskPhone:
            desk.execute(settings.keyVoicemail)
            dismissed.insert(t.key + "\(t.phase)")
            hide()
        }
    }

    /// Toast schließen, ohne den Anruf anzunehmen (Tischtelefon klingelt weiter).
    func close() {
        guard let t = toast else { return }
        dismissed.insert(t.key + "\(t.phase)")
        hide()
    }
}

// MARK: - Ansicht

struct CallToastView: View {
    @EnvironmentObject private var model: ToastController
    @EnvironmentObject private var rec: RecordingController
    @EnvironmentObject private var settings: PhoneSettings
    @State private var hover = false

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            if let t = model.toast {
              VStack(spacing: 8) {
                if t.video { VideoAttachment() }
                HStack(spacing: 12) {
                    Avatar(image: model.contact?.image, name: title(t))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title(t))
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                        Text(subtitle(t, now: ctx.date))
                            .font(.system(size: 13))
                            .foregroundStyle(.white.opacity(0.55))
                            .lineLimit(1)
                    }
                    Spacer(minLength: 8)
                    buttons(t)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .frame(width: 380, height: 84)
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(.ultraThinMaterial)
                        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color(white: 0.28).opacity(0.72)))
                        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(.white.opacity(0.12)))
                )
                .environment(\.colorScheme, .dark)
                .overlay(alignment: .topLeading) {
                    if hover {
                        Button { model.close() } label: {
                            Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
                                .frame(width: 18, height: 18)
                                .background(Circle().fill(Color(white: 0.45)))
                                .foregroundStyle(.white)
                        }
                        .buttonStyle(.plain)
                        .offset(x: -5, y: -5)
                        .help("Ausblenden")
                    }
                }
                .onHover { hover = $0 }
              }
              .frame(maxHeight: .infinity, alignment: .bottom)
            }
        }
    }

    private func title(_ t: ToastController.Toast) -> String {
        if let n = model.contact?.name, !n.isEmpty { return n }
        if !t.name.isEmpty { return t.name }
        return t.number.isEmpty ? "Unbekannt" : t.number
    }

    private func subtitle(_ t: ToastController.Toast, now: Date) -> String {
        var parts: [String] = []
        switch t.phase {
        case .incoming:
            if let l = model.contact?.label, !l.isEmpty { parts.append(l) }
            else if model.contact != nil || !t.name.isEmpty { parts.append(t.number) }
            else { parts.append("Anruf") }
        case .outgoing:
            parts.append("Wählt …")
        case .active:
            let secs = Int(now.timeIntervalSince(t.since ?? now))
            parts.append(String(format: "%d:%02d", secs / 60, secs % 60))
            if rec.isOn { parts.append("● Aufnahme") }
        }
        if t.source == .deskPhone || t.viaDesk { parts.append("Tischtelefon") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private func buttons(_ t: ToastController.Toast) -> some View {
        HStack(spacing: 12) {
            switch t.phase {
            case .incoming:
                RoundKey(symbol: "recordingtape", color: Color(white: 0.5), help: "An die Voicemail") { model.voicemail() }
                RoundKey(symbol: "phone.down.fill", color: .red, help: "Ablehnen") { model.decline() }
                RoundKey(symbol: "phone.fill", color: .green, help: "Annehmen") { model.answer() }
            case .outgoing:
                RoundKey(symbol: "phone.down.fill", color: .red, help: "Auflegen") { model.hangup() }
            case .active:
                if settings.videoEnabled, case .softphone(let id) = t.source {
                    RoundKey(symbol: t.video ? "video.slash.fill" : "video.fill", color: Color(white: 0.5),
                             help: t.video ? "Video aus" : "Video einschalten") {
                        SIPManager.shared.setVideo(callId: id, enabled: !t.video)
                    }
                }
                RoundKey(symbol: rec.isOn ? "stop.fill" : "record.circle",
                         color: rec.isOn ? .red.opacity(0.85) : Color(white: 0.5),
                         help: rec.help, enabled: rec.available && !rec.busy) { rec.toggle() }
                RoundKey(symbol: "phone.down.fill", color: .red, help: "Auflegen") { model.hangup() }
            }
        }
    }
}

/// Videobild über dem Toast: Gegenstelle groß, eigene Kamera klein unten rechts.
private struct VideoAttachment: View {
    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            NativeVideoView(role: .remote)
            NativeVideoView(role: .preview)
                .frame(width: 96, height: 54)
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.white.opacity(0.3)))
                .padding(8)
        }
        .frame(width: ToastController.width, height: ToastController.videoHeight)
        .background(Color.black)
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(.white.opacity(0.12)))
    }
}

/// NSView, in die liblinphone das Video zeichnet (SIPManager.setVideoViews).
private struct NativeVideoView: NSViewRepresentable {
    enum Role { case remote, preview }
    let role: Role

    final class Holder {
        @MainActor static var remote: NSView?
        @MainActor static var preview: NSView?
        @MainActor static func update() {
            SIPManager.shared.setVideoViews(remote: remote, preview: preview)
        }
    }

    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor.black.cgColor
        switch role {
        case .remote: Holder.remote = v
        case .preview: Holder.preview = v
        }
        DispatchQueue.main.async { Holder.update() }
        return v
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    static func dismantleNSView(_ nsView: NSView, coordinator: ()) {
        if Holder.remote === nsView { Holder.remote = nil }
        if Holder.preview === nsView { Holder.preview = nil }
        Holder.update()
    }
}

private struct Avatar: View {
    let image: NSImage?
    let name: String
    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                ZStack {
                    LinearGradient(colors: [Color(white: 0.62), Color(white: 0.42)], startPoint: .top, endPoint: .bottom)
                    if initials.isEmpty {
                        Image(systemName: "person.fill").font(.system(size: 22)).foregroundStyle(.white)
                    } else {
                        Text(initials).font(.system(size: 19, weight: .semibold)).foregroundStyle(.white)
                    }
                }
            }
        }
        .frame(width: 52, height: 52)
        .clipShape(Circle())
    }
    private var initials: String {
        let words = name.split(separator: " ").filter { $0.first?.isLetter == true }
        return words.prefix(2).compactMap { $0.first.map(String.init) }.joined().uppercased()
    }
}

struct RoundKey: View {
    let symbol: String
    let color: Color
    let help: String
    var enabled = true
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 46, height: 46)
                .background(Circle().fill(color.opacity(hover ? 1 : 0.92)))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.35)
        .help(help)
        .onHover { hover = $0 }
    }
}
