import SwiftUI
import UIKit
import linphonesw

struct ActiveCallView: View {
    @EnvironmentObject private var sip: SIPManager
    @State private var showKeypad = false
    @State private var showTransfer = false
    @State private var transferTarget = ""
    @State private var dtmfEntered = ""

    private var call: SIPManager.CallInfo? {
        sip.calls.first { !$0.isPaused } ?? sip.calls.first
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let call, call.hasVideo {
                VideoContainer().ignoresSafeArea()
            }
            VStack(spacing: 24) {
                header
                Spacer()
                if showKeypad { keypad }
                controls
            }
            .padding()
        }
        .preferredColorScheme(.dark)
        .alert("Weiterleiten an", isPresented: $showTransfer) {
            TextField("Nebenstelle / Nummer", text: $transferTarget).keyboardType(.phonePad)
            Button("Weiterleiten") {
                if let call, !transferTarget.isEmpty { sip.transfer(callId: call.id, to: transferTarget) }
                transferTarget = ""
            }
            Button("Abbrechen", role: .cancel) { transferTarget = "" }
        }
    }

    private var header: some View {
        VStack(spacing: 6) {
            Text(call?.displayName.isEmpty == false ? call!.displayName : (call?.remote ?? ""))
                .font(.largeTitle.weight(.semibold)).foregroundStyle(.white).lineLimit(1).minimumScaleFactor(0.5)
            if let call, !call.displayName.isEmpty {
                Text(call.remote).foregroundStyle(.white.opacity(0.7))
            }
            Text(statusText).font(.callout.monospacedDigit()).foregroundStyle(.white.opacity(0.7))
            if let call, call.state == .StreamsRunning || call.state == .Paused {
                Text("\(call.audioCodec)\(call.hasVideo ? " · \(call.videoCodec)" : "")")
                    .font(.caption).foregroundStyle(.white.opacity(0.5))
            }
            if sip.calls.count > 1 {
                Text("\(sip.calls.count - 1) weiterer Anruf gehalten").font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(.top, 40)
        .shadow(radius: 4)
    }

    private var statusText: String {
        guard let call else { return "" }
        switch call.state {
        case .IncomingReceived, .PushIncomingReceived: return "Eingehender Anruf"
        case .OutgoingInit, .OutgoingProgress: return "Verbinde …"
        case .OutgoingRinging: return "Klingelt …"
        case .Paused, .Pausing: return "Gehalten"
        case .PausedByRemote: return "Von Gegenstelle gehalten"
        case .Connected, .StreamsRunning, .Updating, .UpdatedByRemote:
            return String(format: "%02d:%02d", call.duration / 60, call.duration % 60)
        case .End, .Released: return "Beendet"
        case .Error: return "Fehler"
        default: return "\(call.state)"
        }
    }

    private var keypad: some View {
        VStack(spacing: 10) {
            Text(dtmfEntered.isEmpty ? " " : dtmfEntered).foregroundStyle(.white).font(.title2.monospacedDigit())
            ForEach([["1","2","3"],["4","5","6"],["7","8","9"],["*","0","#"]], id: \.self) { row in
                HStack(spacing: 18) {
                    ForEach(row, id: \.self) { d in
                        Button {
                            dtmfEntered.append(d)
                            if let call { CallKitProvider.shared.requestDTMF(callId: call.id, digits: d) }
                        } label: {
                            Text(d).font(.title).foregroundStyle(.white)
                                .frame(width: 64, height: 64)
                                .background(Circle().fill(.white.opacity(0.15)))
                        }
                    }
                }
            }
        }
    }

    private var controls: some View {
        VStack(spacing: 22) {
            if let call, call.isIncoming, call.state == .IncomingReceived || call.state == .PushIncomingReceived {
                HStack(spacing: 60) {
                    RoundButton(icon: "phone.down.fill", color: .red) { CallKitProvider.shared.requestEnd(callId: call.id) }
                    RoundButton(icon: "phone.fill", color: .green) { CallKitProvider.shared.requestAnswer(callId: call.id) }
                }
            } else if let call {
                HStack(spacing: 18) {
                    ToggleButton(icon: sip.micMuted ? "mic.slash.fill" : "mic.fill", active: sip.micMuted, label: "Stumm") {
                        CallKitProvider.shared.requestMute(callId: call.id, muted: !sip.micMuted)
                    }
                    ToggleButton(icon: "circle.grid.3x3.fill", active: showKeypad, label: "Tasten") { showKeypad.toggle() }
                    ToggleButton(icon: "speaker.wave.3.fill", active: sip.speakerOn, label: "Lautspr.") { sip.toggleSpeaker() }
                }
                HStack(spacing: 18) {
                    ToggleButton(icon: call.hasVideo ? "video.fill" : "video.slash", active: call.hasVideo, label: "Video") {
                        sip.setVideo(callId: call.id, enabled: !call.hasVideo)
                    }
                    ToggleButton(icon: "pause.fill", active: call.isPaused, label: "Halten") {
                        CallKitProvider.shared.requestHold(callId: call.id, held: !call.isPaused)
                    }
                    ToggleButton(icon: "arrow.turn.up.right", active: false, label: "Weiterl.") { showTransfer = true }
                }
                if call.hasVideo {
                    ToggleButton(icon: "arrow.triangle.2.circlepath.camera", active: false, label: "Kamera") { sip.switchCamera() }
                }
                RoundButton(icon: "phone.down.fill", color: .red) { CallKitProvider.shared.requestEnd(callId: call.id) }
            }
        }
        .padding(.bottom, 30)
    }
}

private struct RoundButton: View {
    let icon: String; let color: Color; let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: icon).font(.title).foregroundStyle(.white)
                .frame(width: 76, height: 76).background(Circle().fill(color))
        }
    }
}

private struct ToggleButton: View {
    let icon: String; let active: Bool; let label: String; let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(spacing: 6) {
                Image(systemName: icon).font(.title2)
                    .foregroundStyle(active ? .black : .white)
                    .frame(width: 66, height: 66)
                    .background(Circle().fill(active ? Color.white : Color.white.opacity(0.18)))
                Text(label).font(.caption2).foregroundStyle(.white.opacity(0.8))
            }
        }
    }
}

/// Remote-Video vollflächig + eigenes Vorschaubild oben rechts.
/// liblinphone rendert direkt in die übergebenen UIViews (OpenGL/Metal über mediastreamer2).
private struct VideoContainer: UIViewRepresentable {
    func makeUIView(context: Context) -> UIView {
        let container = UIView()
        container.backgroundColor = .black
        let remote = UIView()
        remote.translatesAutoresizingMaskIntoConstraints = false
        let preview = UIView()
        preview.translatesAutoresizingMaskIntoConstraints = false
        preview.layer.cornerRadius = 12
        preview.clipsToBounds = true
        container.addSubview(remote)
        container.addSubview(preview)
        NSLayoutConstraint.activate([
            remote.topAnchor.constraint(equalTo: container.topAnchor),
            remote.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            remote.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            remote.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            preview.topAnchor.constraint(equalTo: container.safeAreaLayoutGuide.topAnchor, constant: 12),
            preview.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
            preview.widthAnchor.constraint(equalToConstant: 110),
            preview.heightAnchor.constraint(equalToConstant: 160),
        ])
        MainActor.assumeIsolated { SIPManager.shared.setVideoViews(remote: remote, preview: preview) }
        return container
    }

    func updateUIView(_ uiView: UIView, context: Context) {}

    static func dismantleUIView(_ uiView: UIView, coordinator: ()) {
        MainActor.assumeIsolated { SIPManager.shared.setVideoViews(remote: nil, preview: nil) }
    }
}
