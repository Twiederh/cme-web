import SwiftUI
import UIKit

/// Startbildschirm: Wähltastatur (langes Drücken der 0 = "+") und während eines Gesprächs die Knöpfe wie im
/// Toast der Mac-App (Annehmen/Ablehnen/Voicemail, Stumm, Tastatur, Lautsprecher, Aufnahme, Halten, Auflegen).
/// Gehaltene Gespräche anderer Geräte (Mitteilung von cme-web) lassen sich hier übernehmen.
/// Telefonieren geht weiterhin auch über die Telefon-App (CallKit, „Zuletzt“, Kontakte, Siri, tel:-Links).
struct ContentView: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @ObservedObject private var rec = RecordingController.shared
    @ObservedObject private var media = MediaLibrary.shared
    @ObservedObject private var heldCalls = HeldCallNotifications.shared
    @State private var showLog = false
    @State private var number = ""
    @State private var showDTMF = false
    @State private var dtmf = ""
    @AppStorage("lastDialed") private var lastDialed = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 18) {
                    VStack(spacing: 4) {
                        Text(settings.extensionNumber.isEmpty ? "CME Phone" : "Nebenstelle \(settings.extensionNumber)")
                            .font(.headline)
                        RegistrationBadge(status: sip.registration)
                        if !settings.provisioningStatus.isEmpty {
                            Text(settings.provisioningStatus).font(.caption2).foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                    }

                    if let call = sip.calls.first {
                        callCard(call)
                    } else {
                        ForEach(heldCalls.held) { h in heldCard(h) }
                        dialpad
                    }

                    // Voicemail und Gesprächsaufnahmen der Nebenstelle (cme-web) und dieses Geräts
                    HStack(spacing: 10) {
                        NavigationLink {
                            MediaListView(source: .voicemail, onCall: dial).navigationTitle("Voicemail")
                        } label: {
                            Label(media.newVoicemails > 0 ? "Voicemail (\(media.newVoicemails))" : "Voicemail",
                                  systemImage: media.newVoicemails > 0 ? "recordingtape.circle.fill" : "recordingtape")
                        }
                        NavigationLink {
                            MediaListView(source: .recordings, onCall: dial).navigationTitle("Aufnahmen")
                        } label: {
                            Label("Aufnahmen", systemImage: "waveform")
                        }
                    }
                    .buttonStyle(.bordered)
                    .font(.callout)

                    // Build-Nummer, Git-Hash ("+" = mit uncommitteten Änderungen) und Bauzeit, z.B. für Tests
                    Text(BuildInfo.text).font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    } label: { Image(systemName: "gearshape") }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showLog = true } label: { Image(systemName: "text.alignleft") }
                }
            }
            .sheet(isPresented: $showLog) { LogView() }
        }
        .onChange(of: sip.calls.first?.id) { _, id in
            if let id { rec.track(ext: settings.extensionNumber, localCallId: id) } else { rec.stop() }
            showDTMF = false
            dtmf = ""
            // nach dem Gespräch: neue Voicemail oder Aufnahme
            if id == nil { media.refresh(force: true) }
        }
        .onAppear {
            media.refresh()
            heldCalls.reloadDelivered()
        }
    }

    // MARK: Wähltastatur

    private var dialpad: some View {
        VStack(spacing: 14) {
            HStack(spacing: 6) {
                Text(number.isEmpty ? " " : number)
                    .font(.system(size: 34, weight: .regular, design: .rounded))
                    .lineLimit(1).minimumScaleFactor(0.5)
                    .frame(maxWidth: .infinity)
                    .contextMenu {
                        Button { paste() } label: { Label("Einfügen", systemImage: "doc.on.clipboard") }
                        if !number.isEmpty {
                            Button { UIPasteboard.general.string = number } label: { Label("Kopieren", systemImage: "doc.on.doc") }
                        }
                    }
                if !number.isEmpty {
                    Button { number.removeLast() } label: { Image(systemName: "delete.left").font(.title2) }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                        .simultaneousGesture(LongPressGesture(minimumDuration: 0.6).onEnded { _ in number = "" })
                }
            }
            .frame(height: 44)

            DialKeypad(shape: .circle, keySize: CGSize(width: 78, height: 78), spacing: 18) { number.append($0) }

            Button {
                // Leer + Anrufen = Wahlwiederholung (wie am Telefon: Nummer erst anzeigen)
                if number.isEmpty { number = lastDialed; return }
                dial(number)
            } label: {
                Image(systemName: "phone.fill").font(.title).foregroundStyle(.white)
                    .frame(width: 78, height: 78).background(Color.green, in: Circle())
            }
            .buttonStyle(.plain)
            .disabled(number.isEmpty && lastDialed.isEmpty)
            .accessibilityLabel(number.isEmpty ? "Wahlwiederholung" : "Anrufen")
        }
    }

    private func paste() {
        let s = (UIPasteboard.general.string ?? "").filter { "0123456789*#+".contains($0) }
        if !s.isEmpty { number = s }
    }

    private func dial(_ n: String) {
        let n = n.filter { "0123456789*#+".contains($0) }
        guard !n.isEmpty else { return }
        lastDialed = n
        number = ""
        CallKitProvider.shared.startOutgoing(number: n, video: false)
    }

    // MARK: Gespräch (Knöpfe wie im Toast der Mac-App)

    private enum Phase { case incoming, outgoing, active }

    private func phase(_ c: SIPManager.CallInfo) -> Phase {
        if c.isEstablished { return .active }
        return c.isIncoming ? .incoming : .outgoing
    }

    private func callCard(_ call: SIPManager.CallInfo) -> some View {
        let ph = phase(call)
        return VStack(spacing: 16) {
            VStack(spacing: 4) {
                Text(call.displayName.isEmpty ? call.remote : call.displayName)
                    .font(.title2.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.6)
                if !call.displayName.isEmpty { Text(call.remote).font(.callout).foregroundStyle(.secondary) }
                Text(statusText(call, ph)).font(.callout).foregroundStyle(call.isPaused ? .orange : .secondary)
                    .monospacedDigit()
            }

            switch ph {
            case .incoming:
                HStack(spacing: 28) {
                    RoundButton(symbol: "recordingtape", color: .gray, label: "Voicemail", enabled: true) {
                        sip.decline(callId: call.id, toVoicemail: true)
                    }
                    RoundButton(symbol: "phone.down.fill", color: .red, label: "Ablehnen", enabled: true) {
                        sip.decline(callId: call.id, toVoicemail: false)
                    }
                    RoundButton(symbol: "phone.fill", color: .green, label: "Annehmen", enabled: true) {
                        CallKitProvider.shared.requestAnswer(callId: call.id)
                    }
                }
            case .outgoing:
                RoundButton(symbol: "phone.down.fill", color: .red, label: "Auflegen", enabled: true) {
                    CallKitProvider.shared.requestEnd(callId: call.id)
                }
            case .active:
                if showDTMF {
                    Text(dtmf.isEmpty ? " " : dtmf).font(.title2.monospacedDigit()).lineLimit(1).truncationMode(.head)
                    DialKeypad(shape: .circle, keySize: CGSize(width: 70, height: 70), spacing: 14) { key in
                        // "+" gibt es als Tonwahl nicht
                        guard key != "+" else { return }
                        dtmf.append(key)
                        CallKitProvider.shared.requestDTMF(callId: call.id, digits: key)
                    }
                }
                HStack(spacing: 22) {
                    RoundButton(symbol: sip.micMuted ? "mic.slash.fill" : "mic.slash", color: sip.micMuted ? .orange : .gray,
                                label: "Stumm", enabled: true) {
                        CallKitProvider.shared.requestMute(callId: call.id, muted: !sip.micMuted)
                    }
                    RoundButton(symbol: "circle.grid.3x3.fill", color: showDTMF ? .blue : .gray,
                                label: showDTMF ? "Ausblenden" : "Tastatur", enabled: true) { showDTMF.toggle() }
                    RoundButton(symbol: sip.speakerOn ? "speaker.wave.3.fill" : "speaker.wave.2",
                                color: sip.speakerOn ? .blue : .gray, label: "Lautsprecher", enabled: true) {
                        sip.toggleSpeaker()
                    }
                }
                HStack(spacing: 22) {
                    RoundButton(symbol: rec.isOn ? "stop.fill" : "record.circle",
                                color: rec.isOn ? .red : .gray, label: rec.isOn ? "Aufnahme läuft" : "Aufnehmen",
                                enabled: rec.available && !rec.busy) { rec.toggle() }
                    RoundButton(symbol: call.isPaused ? "play.fill" : "pause.fill",
                                color: call.isPaused ? .orange : .gray,
                                label: call.isPaused ? "Fortsetzen" : "Halten", enabled: true) {
                        CallKitProvider.shared.requestHold(callId: call.id, held: !call.isPaused)
                    }
                    RoundButton(symbol: "phone.down.fill", color: .red, label: "Auflegen", enabled: true) {
                        CallKitProvider.shared.requestEnd(callId: call.id)
                    }
                }
                if settings.videoEnabled {
                    Button { sip.setVideo(callId: call.id, enabled: !call.hasVideo) } label: {
                        Label(call.hasVideo ? "Video aus" : "Video einschalten",
                              systemImage: call.hasVideo ? "video.slash.fill" : "video.fill")
                    }
                    .buttonStyle(.bordered)
                }
                if !rec.available, !rec.help.isEmpty { Text(rec.help).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 22))
    }

    private func statusText(_ c: SIPManager.CallInfo, _ ph: Phase) -> String {
        switch ph {
        case .incoming: return "Eingehender Anruf"
        case .outgoing: return "Wählt …"
        case .active:
            let d = String(format: "%d:%02d", c.duration / 60, c.duration % 60)
            return c.isPaused ? "Gehalten · \(d) – an einem anderen Gerät übernehmbar" : d
        }
    }

    /// Gehaltenes Gespräch eines anderen Geräts (wie der Übernehmen-Toast auf dem Mac)
    private func heldCard(_ h: SIPManager.HeldLineCall) -> some View {
        let number = h.remoteURI.replacingOccurrences(of: "sip:", with: "").components(separatedBy: "@").first ?? h.remoteURI
        return HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(h.remoteName.isEmpty ? number : h.remoteName).font(.headline).lineLimit(1)
                Text("Gehalten – hier übernehmen").font(.caption).foregroundStyle(.orange)
            }
            Spacer()
            Button { heldCalls.dismiss(h) } label: {
                Image(systemName: "xmark").font(.body.weight(.semibold)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(Color.gray, in: Circle())
            }
            .buttonStyle(.plain).accessibilityLabel("Ausblenden")
            Button { heldCalls.takeOver(h) } label: {
                Image(systemName: "phone.arrow.down.left.fill").font(.body.weight(.semibold)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(Color.green, in: Circle())
            }
            .buttonStyle(.plain).accessibilityLabel("Übernehmen")
        }
        .padding(14)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct RoundButton: View {
    let symbol: String, color: Color, label: String, enabled: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(spacing: 6) {
                Image(systemName: symbol).font(.title2).foregroundStyle(.white)
                    .frame(width: 64, height: 64).background(color, in: Circle())
                Text(label).font(.caption).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
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
