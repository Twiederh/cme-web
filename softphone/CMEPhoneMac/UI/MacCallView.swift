import SwiftUI
import AppKit
import linphonesw

/// Laufende Softphone-Anrufe auf dem Mac (oberhalb der jeweiligen Ansicht).
struct MacCallView: View {
    @EnvironmentObject private var sip: SIPManager
    @State private var showTransfer = false
    @State private var transferTarget = ""
    @State private var dtmf = ""

    private var call: SIPManager.CallInfo? { sip.calls.first { !$0.isPaused } ?? sip.calls.first }

    var body: some View {
        if let call {
            VStack(spacing: 12) {
                if call.hasVideo {
                    MacVideoContainer()
                        .frame(minHeight: 260)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                }
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(call.displayName.isEmpty ? call.remote : call.displayName).font(.title2.weight(.semibold))
                        if !call.displayName.isEmpty { Text(call.remote).foregroundStyle(.secondary) }
                        Text(statusText(call)).font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                        if call.state == .StreamsRunning {
                            Text("\(call.audioCodec)\(call.hasVideo ? " · \(call.videoCodec)" : "")")
                                .font(.caption).foregroundStyle(.tertiary)
                        }
                    }
                    Spacer()
                    controls(call)
                }
                if sip.calls.count > 1 {
                    Text("\(sip.calls.count - 1) weiterer Anruf gehalten").font(.caption).foregroundStyle(.orange)
                }
            }
            .padding()
            .background(.thinMaterial)
            .popover(isPresented: $showTransfer) {
                VStack(alignment: .leading) {
                    Text("Weiterleiten an").font(.headline)
                    TextField("Nebenstelle / Nummer", text: $transferTarget).frame(width: 200)
                        .onSubmit { transfer(call) }
                    Button("Weiterleiten") { transfer(call) }.disabled(transferTarget.isEmpty)
                }.padding()
            }
        }
    }

    @ViewBuilder
    private func controls(_ call: SIPManager.CallInfo) -> some View {
        let cc = CallCenter.shared
        if call.isIncoming && (call.state == .IncomingReceived || call.state == .IncomingEarlyMedia) {
            HStack {
                Button { cc.requestEnd(callId: call.id) } label: { Label("Ablehnen", systemImage: "phone.down.fill") }
                    .tint(.red).buttonStyle(.borderedProminent)
                Button { cc.requestAnswer(callId: call.id) } label: { Label("Annehmen", systemImage: "phone.fill") }
                    .tint(.green).buttonStyle(.borderedProminent)
                    .keyboardShortcut(.return, modifiers: [.command])
            }
        } else {
            VStack(alignment: .trailing, spacing: 8) {
                HStack {
                    Toggle(isOn: Binding(get: { sip.micMuted }, set: { cc.requestMute(callId: call.id, muted: $0) })) {
                        Image(systemName: sip.micMuted ? "mic.slash.fill" : "mic.fill")
                    }.toggleStyle(.button).help("Stumm")
                    Toggle(isOn: Binding(get: { call.isPaused }, set: { cc.requestHold(callId: call.id, held: $0) })) {
                        Image(systemName: "pause.fill")
                    }.toggleStyle(.button).help("Halten")
                    Toggle(isOn: Binding(get: { call.hasVideo }, set: { sip.setVideo(callId: call.id, enabled: $0) })) {
                        Image(systemName: "video.fill")
                    }.toggleStyle(.button).help("Video")
                    if call.hasVideo {
                        Button { sip.switchCamera() } label: { Image(systemName: "arrow.triangle.2.circlepath.camera") }
                            .help("Kamera wechseln")
                    }
                    Button { showTransfer = true } label: { Image(systemName: "arrow.turn.up.right") }.help("Weiterleiten")
                    Button { cc.requestEnd(callId: call.id) } label: { Label("Auflegen", systemImage: "phone.down.fill") }
                        .tint(.red).buttonStyle(.borderedProminent)
                        .keyboardShortcut(.escape, modifiers: [.command])
                }
                TextField("DTMF", text: $dtmf)
                    .frame(width: 140)
                    .onChange(of: dtmf) { old, new in
                        if new.count > old.count, let last = new.last { cc.requestDTMF(callId: call.id, digits: String(last)) }
                    }
            }
        }
    }

    private func transfer(_ call: SIPManager.CallInfo) {
        guard !transferTarget.isEmpty else { return }
        sip.transfer(callId: call.id, to: transferTarget)
        transferTarget = ""; showTransfer = false
    }

    private func statusText(_ call: SIPManager.CallInfo) -> String {
        switch call.state {
        case .IncomingReceived, .IncomingEarlyMedia: return "Eingehender Anruf"
        case .OutgoingInit, .OutgoingProgress: return "Verbinde …"
        case .OutgoingRinging, .OutgoingEarlyMedia: return "Klingelt …"
        case .Paused, .Pausing: return "Gehalten"
        case .PausedByRemote: return "Von Gegenstelle gehalten"
        case .Connected, .StreamsRunning, .Updating, .UpdatedByRemote:
            return String(format: "%02d:%02d", call.duration / 60, call.duration % 60)
        case .End, .Released: return "Beendet"
        case .Error: return "Fehler"
        default: return "\(call.state)"
        }
    }
}

/// Remote-Video + Vorschau (unten rechts) als NSView für liblinphone.
private struct MacVideoContainer: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.black.cgColor
        let remote = NSView(); remote.translatesAutoresizingMaskIntoConstraints = false
        let preview = NSView(); preview.translatesAutoresizingMaskIntoConstraints = false
        preview.wantsLayer = true
        preview.layer?.cornerRadius = 8
        preview.layer?.masksToBounds = true
        container.addSubview(remote)
        container.addSubview(preview)
        NSLayoutConstraint.activate([
            remote.topAnchor.constraint(equalTo: container.topAnchor),
            remote.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            remote.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            remote.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            preview.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -10),
            preview.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -10),
            preview.widthAnchor.constraint(equalToConstant: 160),
            preview.heightAnchor.constraint(equalToConstant: 90),
        ])
        MainActor.assumeIsolated { SIPManager.shared.setVideoViews(remote: remote, preview: preview) }
        return container
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    static func dismantleNSView(_ nsView: NSView, coordinator: ()) {
        MainActor.assumeIsolated { SIPManager.shared.setVideoViews(remote: nil, preview: nil) }
    }
}
