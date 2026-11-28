import Foundation
import os
import Combine
import AVFoundation
import Network
import CMESIP
#if canImport(UIKit)
import UIKit
import PushKit
typealias PlatformView = UIView
#else
import AppKit
typealias PlatformView = NSView
#endif

/// SIP-Schicht der Apps (iOS und Mac) über baresip (CMESIP.xcframework, C-Schicht `cmesip`).
/// baresip läuft in einem eigenen Thread; seine Ereignisse kommen per DispatchQueue.main hier an.
/// iOS: PushKit (VoIP-Push über Kamailio/push-relay), CallKit, SRTP über das Gateway.
/// Mac: wie Jabber direkt an der CME (eigener Pool), Rufton selbst.
@MainActor
final class SIPManager: ObservableObject {
    static let shared = SIPManager()

    enum RegistrationStatus: Equatable {
        case none, progress, ok, failed(String), cleared
        var label: String {
            switch self {
            case .none: return "Nicht angemeldet"
            case .progress: return "Anmeldung läuft …"
            case .ok: return "Angemeldet"
            case .failed(let m): return "Fehler: \(m)"
            case .cleared: return "Abgemeldet"
            }
        }
    }

    /// Zustände wie bei liblinphone (gleiche Namen, damit die Oberfläche unverändert bleibt)
    enum CallState: String {
        case Idle, IncomingReceived, PushIncomingReceived, IncomingEarlyMedia, EarlyUpdating, EarlyUpdatedByRemote
        case OutgoingInit, OutgoingProgress, OutgoingRinging, OutgoingEarlyMedia
        case Connected, StreamsRunning, Pausing, Paused, PausedByRemote, Resuming, Updating, UpdatedByRemote
        case End, Error, Released
    }

    struct CallInfo: Identifiable, Equatable {
        let id: String            // SIP Call-ID
        var remote: String
        var displayName: String
        var state: CallState
        var isIncoming: Bool
        var hasVideo: Bool
        var isPaused: Bool
        var audioCodec: String
        var videoCodec: String
        var duration: Int
        var isRecording: Bool
        var mediaDetails: [String] = []
        var isEstablished: Bool {
            switch state {
            case .Connected, .StreamsRunning, .Pausing, .Paused, .PausedByRemote, .Resuming, .Updating, .UpdatedByRemote:
                return true
            default: return false
            }
        }
    }

    struct AudioDevice: Identifiable, Hashable {
        let id: String            // Gerätename wie bei CoreAudio (baresip sucht danach)
        let canRecord: Bool
        let canPlay: Bool
    }

    @Published private(set) var registration: RegistrationStatus = .none
    @Published private(set) var calls: [CallInfo] = []
    @Published private(set) var micMuted = false
    @Published private(set) var speakerOn = false
    @Published private(set) var voipToken: String?
    @Published private(set) var lastLog: [String] = []

    private let settings = PhoneSettings.shared
    private var callStateHandlers: [(CallInfo, CallState) -> Void] = []
    private var durationTimer: Timer?
    private var started = false
    private var software = ""
    private var pathMonitor: NWPathMonitor?
    private var lastPath = ""

    func addCallStateHandler(_ handler: @escaping (CallInfo, CallState) -> Void) {
        callStateHandlers.append(handler)
    }

    private init() {}

    // MARK: - Start

    /// Jabber: "User-Agent: Cisco-CSF", sonst "CME Phone/<Build>" (cme-web zeigt den Build an)
    private var wantedSoftware: String {
        settings.jabberMode ? "Cisco-CSF"
            : "CME Phone/" + ((Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String) ?? "?")
    }

    #if os(iOS)
    private static let audioModule = "audiounit"
    #else
    private static let audioModule = "coreaudio"
    #endif

    private var baresipConfig: String {
        // 5060 wie ein Cisco-Telefon (s. CLAUDE.md: UDM-SIP-Helfer); auf allen Schnittstellen, UDP und TCP
        let port = settings.jabberMode ? 5060 : 0
        let m = Self.audioModule
        let out = settings.audioOutputId.isEmpty ? "default" : settings.audioOutputId
        let inp = settings.audioInputId.isEmpty ? "default" : settings.audioInputId
        return """
        sip_listen 0.0.0.0:\(port)
        audio_player \(m),\(out)
        audio_source \(m),\(inp)
        audio_alert \(m),\(out)
        rtp_ports 30000-30999
        rtp_timeout 60
        video_source cmecam,\(Self.cameraId(settings.videoDeviceId))
        video_display cmedisp,app
        video_size 1280x720
        video_fps 30
        video_bitrate 1500000
        opus_bitrate 32000
        opus_stereo no
        opus_sprop_stereo no
        \(Self.tlsConfig)
        """
    }

    /// TLS zum Gateway: Zertifikat gegen die mitgelieferten Wurzelzertifikate (cacert.pem, Mozilla-Liste aus macOS)
    /// und den Namen des Gateways prüfen
    private static var tlsConfig: String {
        if let ca = Bundle.main.path(forResource: "cacert", ofType: "pem") {
            return "sip_cafile \(ca)\nsip_verify_server yes"
        }
        return "sip_verify_server no"
    }

    func start() {
        guard !started else { return }
        software = wantedSoftware
        #if os(iOS)
        configureAudioSession()
        #endif
        let err = cmesip_start(software, baresipConfig, "g711,libg722,opus,srtp,\(Self.audioModule)", cmesipCallback, nil)
        guard err == 0 else { log("baresip-Start fehlgeschlagen: \(Self.errText(err))"); return }
        started = true
        _ = cmesip_set_trace(true)
        VideoRenderer.shared.install()
        #if os(iOS)
        // Feste Geräte-ID (+sip.instance): Kamailio ersetzt damit die vorige Anmeldung dieses Geräts, statt nach
        // jedem Aufwachen (neue TCP-Verbindung, neuer Port) einen weiteren Contact zu speichern
        _ = cmesip_set_instance(Self.instanceUUID)
        // Nach einem Start durch den VoIP-Push kommt das Token erst später – bis dahin das zuletzt bekannte
        if voipToken == nil { voipToken = UserDefaults.standard.string(forKey: Self.voipTokenKey) }
        startPushKit()
        #endif
        log("CME Phone \(BuildInfo.text), baresip gestartet")
        startPathMonitor()
        let d = UserDefaults.standard
        if !settings.jabberMode {
            d.set(false, forKey: Self.runningKey)
        } else if d.bool(forKey: Self.runningKey) {
            log("Letzter Lauf endete ohne Beenden-Ablauf (Absturz, Kill oder Neustart des Macs) – CME hält die alte Anmeldung evtl. noch bis zu \(Self.jabberExpires) s")
        } else if let last = d.string(forKey: Self.cleanQuitKey) {
            log("Letztes Beenden: \(last)")
        }
        if settings.jabberMode { d.set(true, forKey: Self.runningKey) }
        applyAccount()
    }

    /// Netzwechsel (WLAN, VPN): Transporte neu aufbauen und neu anmelden
    private func startPathMonitor() {
        let m = NWPathMonitor()
        m.pathUpdateHandler = { path in
            let key = path.availableInterfaces.map(\.name).joined(separator: ",") + "|\(path.status)"
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    let sip = SIPManager.shared
                    guard sip.started, key != sip.lastPath else { return }
                    let first = sip.lastPath.isEmpty
                    sip.lastPath = key
                    if !first, path.status == .satisfied {
                        sip.log("Netz geändert (\(key)) – Transporte neu")
                        _ = cmesip_network_changed()
                    }
                }
            }
        }
        m.start(queue: .global(qos: .utility))
        pathMonitor = m
    }

    // MARK: - Konto

    /// Kürzeste Anmeldedauer, die CME annimmt: Bleibt eine Anmeldung nach einem harten Ende hängen, ist die
    /// Leitung höchstens so lange blockiert ("403 Registration is active for another client").
    private static let jabberExpires = 60
    private static let cleanQuitKey = "lastQuitUnregistered"
    private static let runningKey = "sipRunning"
    private var forbiddenRetry: DispatchWorkItem?
    private var applyGeneration = 0
    private var accountActive = false

    /// Wie ein Cisco-Telefon (8845: "Supported: …,X-cisco-sis-7.0.0,…"). CME antwortet mit X-cisco-cme-sis und
    /// behandelt das Gerät als Cisco-Endgerät (Shared Line, dialog-Abo). "X-cisco-duplicate-reg": eine erneute
    /// Anmeldung desselben Geräts gilt nicht als Duplikat.
    static let ciscoSupported = "replaces,join,sdp-anat,norefersub,resource-priority,extended-refer,"
        + "X-cisco-callinfo,X-cisco-serviceuri,X-cisco-escapecodes,X-cisco-service-control,X-cisco-srtp-fallback,"
        + "X-cisco-monrec,X-cisco-config,X-cisco-sis-7.0.0,X-cisco-xsi-8.5.1,X-cisco-graceful-reg,X-cisco-duplicate-reg"

    func applyAccount() {
        guard started else { start(); return }
        applyGeneration += 1
        let generation = applyGeneration
        stopLineSubscription()
        forbiddenRetry?.cancel(); forbiddenRetry = nil
        let wasRegistered = accountActive && (registration == .ok || registration == .progress)
        registration = .none
        // CME kennt pro Pool nur EINE Anmeldung: erst die alte abmelden lassen, dann neu anmelden
        if accountActive {
            cmesip_remove_account()
            accountActive = false
        }
        // User-Agent (Jabber ↔ Gateway) und Port gelten nur ab Start von baresip
        let restart = software != wantedSoftware
        let go = { [weak self] in
            guard let self, self.applyGeneration == generation else { return }
            if restart {
                self.log("baresip wird neu gestartet (\(self.wantedSoftware))")
                self.stopEngine()
                self.start()
                return
            }
            self.addConfiguredAccount()
        }
        if wasRegistered {
            log("Konto wird neu angemeldet – alte Anmeldung wird zuerst beendet")
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { MainActor.assumeIsolated { go() } }
        } else {
            go()
        }
    }

    private func stopEngine() {
        cmesip_unsubscribe(0)
        subHandlers.removeAll()
        cmesip_stop()
        started = false
        entries.removeAll()
        refreshCalls()
    }

    /// Codecs in der Reihenfolge der Einstellungen (baresip-Namen mit Takt)
    private var codecList: String {
        settings.audioCodecs.filter(\.enabled).compactMap { c -> String? in
            switch c.mime.uppercased() {
            case "G722": return "G722/16000/1"
            case "PCMA": return "PCMA/8000/1"
            case "PCMU": return "PCMU/8000/1"
            case "OPUS": return "opus/48000/1"   // baresip führt Opus (Mono) so; im SDP steht opus/48000/2
            default: return nil
            }
        }.joined(separator: ",")
    }

    /// SRTP (SDES): iOS über das Gateway immer – Kamailio/rtpengine verschlüsseln die Strecke zu den iOS-Geräten.
    /// Nicht verpflichtend (baresip "srtp": unverschlüsseltes RTP wird weiter angenommen). Sonst die Einstellung.
    private var useSRTP: Bool {
        #if os(iOS)
        if settings.useGateway && !settings.jabberMode { return true }
        #endif
        return settings.mediaEncryption == .srtp
    }

    private func addConfiguredAccount() {
        guard settings.isConfigured else { log("Kein Konto konfiguriert"); return }
        let viaGateway = settings.useGateway && !settings.jabberMode
        let transport: SIPTransport = settings.jabberMode ? (settings.transport == .tcp ? .tcp : .udp) : settings.transport
        // TLS nur über das Gateway (Kamailio, Zertifikat für dessen Namen); direkt an CME UDP/TCP
        let tp = transport == .udp ? "udp" : (transport == .tls && viaGateway ? "tls" : "tcp")

        var aor = "<sip:\(settings.extensionNumber)@\(settings.sipHost)" + (viaGateway ? "" : ";transport=\(tp)") + ">"
        if !settings.displayName.isEmpty && !settings.jabberMode {
            aor = "\"\(settings.displayName.replacingOccurrences(of: "\"", with: ""))\" " + aor
        }
        if !settings.password.isEmpty {
            let authId = settings.authUser.isEmpty ? String(settings.extensionNumber.drop(while: { $0 == "+" })) : settings.authUser
            aor += ";auth_user=\(authId);auth_pass=\(settings.password)"
        }
        if viaGateway {
            aor += ";outbound=\"sip:\(settings.gatewayHost):\(settings.gatewayPort);transport=\(tp)\""
        }
        #if os(iOS)
        // Über das Gateway mit Push: die Anmeldung muss bis zum nächsten Öffnen gelten (Kamailio max_expires 7 Tage)
        aor += ";regint=\(settings.jabberMode ? Self.jabberExpires : 7 * 24 * 3600)"
        #else
        aor += ";regint=\(settings.jabberMode ? Self.jabberExpires : 600)"
        #endif
        aor += ";answermode=manual;ptime=20"
        let codecs = codecList
        if !codecs.isEmpty { aor += ";audio_codecs=\(codecs)" }
        let vcodecs = settings.videoCodecs.filter(\.enabled).map(\.mime).joined(separator: ",")
        if !vcodecs.isEmpty { aor += ";video_codecs=\(vcodecs)" }
        if useSRTP { aor += ";mediaenc=srtp" }

        // CME nimmt im CME-Modus nur MAC-Adressen oder – für Softclients wie Jabber – einen Gerätenamen an
        var hdrParams = settings.jabberMode
            ? "+u.sip!devicename.ccm.cisco.com=\"\(settings.deviceName)\";+u.sip!model.ccm.cisco.com=\"503\"" : nil
        let regHdr = settings.jabberMode ? "Supported: \(Self.ciscoSupported)" : nil
        var uriParams: String?
        #if os(iOS)
        if viaGateway {
            // CME ordnet INVITEs dem Pool über den Gerätenamen im Contact zu (MOB<Nummer>, meldet cme-web an)
            hdrParams = "+u.sip!devicename.ccm.cisco.com=\"\(settings.mobilePoolName)\";+u.sip!model.ccm.cisco.com=\"503\""
            // RFC 8599 wie liblinphone: Kamailio schickt darüber den VoIP-Push (push-relay)
            if let tok = voipToken {
                #if DEBUG
                let provider = "apns.dev"
                #else
                let provider = "apns"
                #endif
                uriParams = "pn-provider=\(provider);pn-prid=\(tok):voip;pn-param=\(Self.teamID).de.wiederhol.cmephone.voip"
            } else {
                log("Noch kein VoIP-Token – Anmeldung ohne Push")
            }
        }
        #endif

        registration = .progress
        let err = cmesip_set_account(aor, uriParams, hdrParams, regHdr)
        if err != 0 {
            log("Konto-Fehler: \(Self.errText(err))")
            registration = .failed(Self.errText(err))
            return
        }
        accountActive = true
        log("Konto sip:\(settings.extensionNumber)@\(settings.sipHost) über "
            + (viaGateway ? "\(settings.gatewayHost):\(settings.gatewayPort)" : "\(settings.sipHost) (\(tp.uppercased()))")
            + ", Codecs \(codecs)")
    }

    func refreshRegistration() { _ = cmesip_register() }

    func reRegister() {
        log("Neu anmelden (von Hand)")
        if !started { start() } else { applyAccount() }
    }

    /// Abmelden und kurz warten (beim Beenden der App)
    func unregisterBlocking(timeout: TimeInterval) {
        guard started else { return }
        stopLineSubscription()
        cmesip_unsubscribe(0)
        unregisterAnswered = false
        awaitingUnregister = true
        _ = cmesip_unregister()
        let end = Date().addingTimeInterval(timeout)
        while Date() < end && !unregisterAnswered {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        let ok = unregisterAnswered
        log("Beim Beenden abgemeldet: \(ok ? "bestätigt" : "keine Antwort")")
        let stamp = Date().formatted(date: .numeric, time: .standard)
        UserDefaults.standard.set("\(stamp) \(ok ? "abgemeldet" : "Abmelden NICHT bestätigt")", forKey: Self.cleanQuitKey)
        UserDefaults.standard.set(false, forKey: Self.runningKey)
        UserDefaults.standard.synchronize()
        cmesip_stop()
        started = false
    }
    private var unregisterAnswered = false
    private var awaitingUnregister = false

    // MARK: - Gehaltene Gespräche auf der Shared Line (Übernehmen)

    struct HeldLineCall: Equatable, Identifiable {
        var id: String { callId }
        let callId: String
        let localTag: String     // Tag des haltenden Geräts
        let remoteTag: String    // Tag der CME
        let remoteURI: String    // Gegenstelle, z.B. sip:0179…@192.168.11.4
        let remoteName: String
        let holder: String       // Gerät, das hält
    }
    @Published private(set) var heldOnLine: [HeldLineCall] = []

    private static func attr(_ name: String, in text: String) -> String {
        let p = "\\b\(name)=\"([^\"]*)\""
        guard let re = try? NSRegularExpression(pattern: p),
              let m = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let r = Range(m.range(at: 1), in: text) else { return "" }
        return String(text[r])
    }

    private static func firstGroup(_ pattern: String, in text: String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]),
              let m = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let r = Range(m.range(at: 1), in: text) else { return "" }
        return String(text[r])
    }

    /// dialog-NOTIFY der CME: gehalten = <param pname="+sip.rendering" pval="no"/> (Cisco schreibt pval)
    func handleLineNotify(_ xml: String) {
        guard xml.contains("<dialog") else { return }
        let entity = Self.attr("entity", in: xml)
        guard let re = try? NSRegularExpression(pattern: "<dialog\\s.*?</dialog>", options: [.dotMatchesLineSeparators])
        else { return }
        let ownIds = Set(entries.keys)
        var list = heldOnLine
        for m in re.matches(in: xml, range: NSRange(xml.startIndex..., in: xml)) {
            guard let r = Range(m.range, in: xml) else { continue }
            let d = String(xml[r])
            let head = String(d.prefix(while: { $0 != ">" }))
            let callId = Self.attr("call-id", in: head)
            guard !callId.isEmpty else { continue }
            let state = Self.firstGroup("<state[^>]*>([^<]*)</state>", in: d)
            let held = d.range(of: "\\+sip\\.rendering\"\\s+pval(ue)?=\"no\"", options: .regularExpression) != nil
            list.removeAll { $0.callId == callId }
            guard held, state != "terminated", !ownIds.contains(callId) else { continue }
            let remote = Self.firstGroup("<remote>.*?<identity([^>]*)>", in: d)
            let uri = Self.firstGroup("<remote>.*?<identity[^>]*>([^<]*)</identity>", in: d)
            guard !uri.isEmpty else { continue }
            list.append(HeldLineCall(callId: callId, localTag: Self.attr("local-tag", in: head),
                                     remoteTag: Self.attr("remote-tag", in: head), remoteURI: uri,
                                     remoteName: Self.attr("display", in: remote).trimmingCharacters(in: .whitespaces),
                                     holder: entity))
        }
        if list != heldOnLine {
            heldOnLine = list
            log("Shared Line: gehalten auf anderem Gerät: \(list.map { "\($0.remoteURI) (\($0.holder))" }.joined(separator: ", "))")
        }
    }

    /// Übernehmen wie ein Cisco-Telefon: INVITE an die Gegenstelle mit
    /// "Replaces: <call-id>;to-tag=<Tag des haltenden Geräts>;from-tag=<Tag der CME>"
    @discardableResult
    func takeOver(_ h: HeldLineCall) -> String? {
        heldOnLine.removeAll { $0.callId == h.callId }
        log("Übernehme gehaltenes Gespräch \(h.remoteURI)")
        // Über das Gateway: From mit der CME-IP, sonst ordnet CME das Gespräch keinem Pool der Shared Line zu
        let from = !settings.jabberMode && !settings.cmeIP.isEmpty ? "sip:\(settings.extensionNumber)@\(settings.cmeIP)" : nil
        return dial(h.remoteURI, from: from, replaces: "\(h.callId);to-tag=\(h.localTag);from-tag=\(h.remoteTag)",
                    remote: h.remoteURI, name: h.remoteName)
    }

    // MARK: - Abos (dialog der eigenen Leitung, presence des Tischtelefons)

    private var subHandlers: [Int: (notify: (String) -> Void, closed: (String) -> Void)] = [:]
    private var lineSub: Int?
    private var lineRetry: DispatchWorkItem?

    /// Abo über das Konto. Ergebnis: Abo-Nummer oder nil
    @discardableResult
    func subscribe(uri: String, event: String, from: String? = nil, expires: Int, headers: [String: String],
                   onNotify: @escaping (String) -> Void, onClosed: @escaping (String) -> Void) -> Int? {
        let hdrs = headers.map { "\($0.key): \($0.value)\r\n" }.joined()
        let id = Int(cmesip_subscribe(uri, event, from, UInt32(expires), hdrs))
        guard id > 0 else { log("Abo \(event) auf \(uri) fehlgeschlagen: \(Self.errText(Int32(-id)))"); return nil }
        subHandlers[id] = (onNotify, onClosed)
        return id
    }

    func unsubscribe(_ id: Int) {
        subHandlers[id] = nil
        cmesip_unsubscribe(Int32(id))
    }

    /// Wie das 8845: nach der Anmeldung die eigene Leitung per "dialog" abonnieren. CME nimmt das Gerät nur in die
    /// Shared Line auf, wenn das Abo die eigene IP im From trägt.
    private func subscribeOwnLine() {
        guard settings.jabberMode, lineSub == nil, !settings.extensionNumber.isEmpty else { return }
        let host = Self.localAddress(toward: settings.sipHost) ?? settings.sipHost
        let me = "sip:\(settings.extensionNumber)@\(settings.sipHost)"
        lineSub = subscribe(
            uri: me, event: "dialog", from: "sip:\(settings.extensionNumber)@\(host)", expires: 600,
            headers: ["Accept": "application/dialog-info+xml",
                      "Allow": "ACK,BYE,CANCEL,INVITE,NOTIFY,OPTIONS,REFER,REGISTER,UPDATE,SUBSCRIBE"],
            onNotify: { [weak self] body in self?.handleLineNotify(body) },
            onClosed: { [weak self] why in
                guard let self else { return }
                self.log("Cisco-Leitungsmodus: Abo beendet (\(why)) – neu in 30 s")
                self.lineSub = nil
                let item = DispatchWorkItem { MainActor.assumeIsolated { self.subscribeOwnLine() } }
                self.lineRetry = item
                DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: item)
            })
        if lineSub != nil { log("Cisco-Leitungsmodus: Abo dialog auf \(settings.extensionNumber), From sip:\(settings.extensionNumber)@\(host)") }
    }

    private func stopLineSubscription() {
        lineRetry?.cancel(); lineRetry = nil
        if let id = lineSub { unsubscribe(id) }
        lineSub = nil
    }

    /// Eigene IP auf dem Weg zu host (UDP-Socket verbinden, nichts senden)
    static func localAddress(toward host: String) -> String? {
        var hints = addrinfo(ai_flags: 0, ai_family: AF_INET, ai_socktype: SOCK_DGRAM, ai_protocol: 0,
                             ai_addrlen: 0, ai_canonname: nil, ai_addr: nil, ai_next: nil)
        var res: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, "5060", &hints, &res) == 0, let ai = res else { return nil }
        defer { freeaddrinfo(res) }
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        guard connect(fd, ai.pointee.ai_addr, ai.pointee.ai_addrlen) == 0 else { return nil }
        var sin = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &sin) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) == 0 }
        }
        guard ok else { return nil }
        var buf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        inet_ntop(AF_INET, &sin.sin_addr, &buf, socklen_t(INET_ADDRSTRLEN))
        return String(cString: buf)
    }

    // MARK: - Medien

    /// Codecs und SRTP stehen im Konto – neu anmelden übernimmt sie (applyAccount ruft das danach auf).
    func applyMediaSettings() {
        log("Audio: \(codecList)\(useSRTP ? " | SRTP" : "")")
    }

    // MARK: - Anrufe

    private final class Entry {
        let id: String
        var remoteURI: String
        var displayName: String
        let incoming: Bool
        var state: CallState
        let created = Date()
        var established: Date?
        var heldLocal = false
        var heldRemote = false
        var info = ""
        init(id: String, remoteURI: String, displayName: String, incoming: Bool, state: CallState) {
            self.id = id; self.remoteURI = remoteURI; self.displayName = displayName
            self.incoming = incoming; self.state = state
        }
    }
    private var entries: [String: Entry] = [:]
    private var order: [String] = []

    private func dial(_ uri: String, from: String?, replaces: String?, remote: String, name: String,
                      video: Bool = false) -> String? {
        var buf = [CChar](repeating: 0, count: 256)
        let err = cmesip_dial(uri, from, replaces, video, &buf, buf.count)
        guard err == 0 else { log("Anruf fehlgeschlagen: \(Self.errText(err))"); return nil }
        let id = String(cString: buf)
        if entries[id] == nil {
            entries[id] = Entry(id: id, remoteURI: remote, displayName: name, incoming: false, state: .OutgoingInit)
            order.append(id)
        }
        if micMuted { _ = cmesip_mute(id, true) }
        refreshCalls()
        return id
    }

    /// Adresse unter der eigenen öffentlichen Domain (Jitsi-Raum, Alias): Hängt das Konto direkt an der CME (Mac),
    /// würde sie per DNS-SRV beim CUCM landen. Deshalb per maddr an Kamailio (TCP, Zugang von draußen); die Adresse
    /// selbst bleibt unverändert. Über das Gateway (iOS) geht ohnehin alles an Kamailio.
    private func viaKamailio(_ uri: String) -> String {
        guard !(settings.useGateway && !settings.jabberMode), !settings.gatewayHost.isEmpty,
              uri.lowercased().hasPrefix("sip:"), let at = uri.firstIndex(of: "@") else { return uri }
        var dom = settings.publicDomain.lowercased()
        if dom.isEmpty, let dot = settings.gatewayHost.firstIndex(of: ".") {   // sip-gw.example.de -> example.de
            dom = String(settings.gatewayHost[settings.gatewayHost.index(after: dot)...]).lowercased()
        }
        let hostPart = uri[uri.index(after: at)...]
        let host = hostPart.prefix { $0 != ";" && $0 != ":" && $0 != ">" }.lowercased()
        guard !dom.isEmpty, host == dom || settings.otherDomains.contains(host), !hostPart.contains(";") else { return uri }
        let r = "\(uri[..<at])@\(host):\(settings.extSipPort);maddr=\(settings.gatewayHost);transport=tcp"
        log("Wahl \(uri) über Kamailio (\(settings.gatewayHost):\(settings.extSipPort) TCP)")
        return r
    }

    @discardableResult
    func placeCall(to number: String, video: Bool) -> String? {
        let target = number.contains("@") ? number : "sip:\(number)@\(settings.sipHost)"
        let uri = target.hasPrefix("sip") ? target : "sip:\(target)"
        // Videoanruf nur auf Wunsch: ein m=video im Angebot an reine Audio-Gegenstellen (Trunk) vermeiden
        return dial(viaKamailio(uri), from: nil, replaces: nil, remote: uri, name: "", video: video && settings.videoEnabled)
    }

    func accept(callId: String) {
        let err = cmesip_answer(callId)
        if err != 0 { log("Annehmen fehlgeschlagen: \(Self.errText(err))") }
    }

    func terminate(callId: String) {
        _ = cmesip_hangup(callId, 0)
    }

    /// Eingehenden Anruf abweisen. An die Voicemail: 302 auf die Voicemail-Nummer – CME leitet dann den GANZEN
    /// Anruf um (auch bei weiteren klingelnden Geräten). Ohne Nummer 486 (call-forward busy), sonst 603.
    func decline(callId: String, toVoicemail: Bool) {
        if toVoicemail, !settings.voicemailNumber.isEmpty {
            let err = cmesip_redirect(callId, "sip:\(settings.voicemailNumber)@\(settings.sipHost)")
            if err == 0 { log("An Voicemail umgeleitet (\(settings.voicemailNumber))"); return }
            log("Umleiten an Voicemail fehlgeschlagen: \(Self.errText(err)) – lehne mit besetzt ab")
        }
        _ = cmesip_hangup(callId, toVoicemail ? 486 : 603)
    }

    /// Halten / Fortsetzen wie ein Cisco-Telefon: re-INVITE (sendonly/sendrecv) mit
    /// "Call-Info: <urn:x-cisco-remotecc:hold|resume>" – nur so sehen die anderen Geräte der Shared Line es.
    func setHeld(callId: String, held: Bool) {
        let hdr = "Call-Info: <urn:x-cisco-remotecc:\(held ? "hold" : "resume")>\r\n"
        let err = cmesip_hold(callId, held, hdr)
        guard err == 0 else { log("Halten: \(Self.errText(err))"); return }
        entries[callId]?.heldLocal = held
        log(held ? "Gespräch gehalten" : "Gespräch fortgesetzt")
        refreshCalls()
    }

    func setMuted(_ muted: Bool) {
        micMuted = muted
        for id in entries.keys { _ = cmesip_mute(id, muted) }
    }

    func sendDTMF(_ digits: String, callId: String? = nil) {
        guard let id = callId ?? calls.first(where: { $0.isEstablished })?.id else { return }
        for ch in digits { _ = cmesip_send_dtmf(id, CChar(ch.asciiValue ?? 0)) }
    }

    func toggleSpeaker() {
        #if os(iOS)
        let want = !speakerOn
        do {
            try AVAudioSession.sharedInstance().overrideOutputAudioPort(want ? .speaker : .none)
            speakerOn = want
        } catch { log("Lautsprecher: \(error.localizedDescription)") }
        #endif
    }

    /// Blind Transfer (REFER)
    func transfer(callId: String, to number: String) {
        let err = cmesip_transfer(callId, "sip:\(number)@\(settings.sipHost)")
        if err != 0 { log("Transfer: \(Self.errText(err))") }
    }

    // MARK: - Klingeln (baresip spielt keinen Rufton – eigener Ton über AVAudioPlayer)

    private var ringerEnabled = true
    private var ringPlayer: AVAudioPlayer?

    /// Telefonsteuerung: der Mac klingelt über die Shared Line nur stumm mit (Toast), es klingelt das Tischtelefon.
    func setRingerEnabled(_ on: Bool) {
        ringerEnabled = on
        updateRinger()
    }

    private func updateRinger() {
        #if os(macOS)   // iOS: CallKit klingelt
        let ringing = ringerEnabled && entries.values.contains { $0.incoming && $0.state == .IncomingReceived }
        if ringing {
            if ringPlayer == nil {
                ringPlayer = try? AVAudioPlayer(data: Self.ringTone)
                ringPlayer?.numberOfLoops = -1
                ringPlayer?.volume = 0.6
            }
            if ringPlayer?.isPlaying == false { ringPlayer?.play() }
        } else if ringPlayer?.isPlaying == true {
            ringPlayer?.stop()
            ringPlayer?.currentTime = 0
        }
        #endif
    }

    /// Triller wie ein Tischtelefon: 1 s abwechselnd 1000/1250 Hz (je 40 ms), 3 s Pause
    private static let ringTone: Data = {
        let rate = 16000
        var samples = [Int16]()
        let on = rate, off = 3 * rate
        for i in 0..<on {
            let t = Double(i) / Double(rate)
            let f = (i / (rate / 25)) % 2 == 0 ? 1000.0 : 1250.0
            let fade = min(1.0, Double(min(i, on - i)) / Double(rate / 100))
            samples.append(Int16(sin(2 * .pi * f * t) * 9000 * fade))
        }
        samples += [Int16](repeating: 0, count: off)
        var d = Data()
        func u32(_ v: Int) { var x = UInt32(v).littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: Int) { var x = UInt16(v).littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!); u32(36 + samples.count * 2)
        d.append("WAVEfmt ".data(using: .ascii)!); u32(16); u16(1); u16(1); u32(rate); u32(rate * 2); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(samples.count * 2)
        samples.withUnsafeBytes { d.append(contentsOf: $0) }
        return d
    }()

    // MARK: - Lokale Aufnahme (interne Gespräche; externe nimmt cme-recorder auf)

    static var recordingsFolder: URL {
        #if os(macOS)
        let base = FileManager.default.urls(for: .musicDirectory, in: .userDomainMask).first
            ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("CME Phone", isDirectory: true)
        #else
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Aufnahmen", isDirectory: true)
        #endif
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private var recordingCalls = Set<String>()

    private static func recordingPath(remote: String) -> String {
        let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let who = remote.filter { $0.isNumber || $0 == "+" }
        return recordingsFolder.appendingPathComponent("\(df.string(from: Date())) \(who.isEmpty ? "Anruf" : who).wav").path
    }

    /// Lokale Aufnahme (cmesip: Mikrofon und Gegenstelle gemischt, WAV); endet spätestens mit dem Gespräch
    func setLocalRecording(callId: String, on: Bool) {
        let path = on ? Self.recordingPath(remote: call(withId: callId)?.remote ?? "") : nil
        let err = cmesip_record(callId, path)
        guard err == 0 else { log("Lokale Aufnahme: \(Self.errText(err))"); return }
        if on { recordingCalls.insert(callId) } else { recordingCalls.remove(callId) }
        log("Lokale Aufnahme \(on ? "an: \(path ?? "")" : "aus")")
        refreshCalls()
    }

    // MARK: - Audiogeräte

    var audioDevices: [AudioDevice] {
        func list(_ input: Bool) -> [String] {
            var buf = [CChar](repeating: 0, count: 8192)
            guard cmesip_audio_devices(input, &buf, buf.count) == 0 else { return [] }
            return String(cString: buf).split(separator: "\n").map(String.init)
        }
        let ins = list(true), outs = list(false)
        var result: [AudioDevice] = []
        for n in ins { result.append(AudioDevice(id: n, canRecord: true, canPlay: outs.contains(n))) }
        for n in outs where !ins.contains(n) { result.append(AudioDevice(id: n, canRecord: false, canPlay: true)) }
        return result
    }

    /// Gespeicherte Audiogeräte setzen (leer = Systemstandard); gilt auch im laufenden Gespräch
    func applySavedAudioDevices() {
        guard started else { return }
        let devs = audioDevices
        let input = devs.contains { $0.id == settings.audioInputId && $0.canRecord } ? settings.audioInputId : ""
        let output = devs.contains { $0.id == settings.audioOutputId && $0.canPlay } ? settings.audioOutputId : ""
        _ = cmesip_set_audio_devices(input, output)
    }

    // MARK: - Video (cmevideo.m: Kamera, H.264/H.265 über VideoToolbox)

    /// Kameras als "uniqueID: Name" (Mac-Einstellungen zeigen den Teil nach ": ")
    var videoDevices: [String] {
        var buf = [CChar](repeating: 0, count: 4096)
        _ = cmesip_video_devices(&buf, buf.count)
        return String(cString: buf).split(separator: "\n").compactMap { line in
            let p = line.split(separator: "\t", maxSplits: 1).map(String.init)
            return p.count == 2 ? "\(p[0]): \(p[1])" : nil
        }
    }
    var currentVideoDevice: String { videoDevices.first ?? "" }

    /// uniqueID aus "uniqueID: Name" (ältere Einstellung "AVFoundation: <Name>" -> Standardkamera)
    static func cameraId(_ setting: String) -> String {
        guard let r = setting.range(of: ": ") else { return setting }
        let id = String(setting[..<r.lowerBound])
        return id == "AVFoundation" ? "" : id
    }

    func applySavedVideoDevice() {
        guard started else { return }
        _ = cmesip_set_video_device(Self.cameraId(settings.videoDeviceId))
    }

    func setVideo(callId: String, enabled: Bool) {
        let err = cmesip_set_video(callId, enabled)
        if err == ENOTSUP {
            log("Video nachträglich einschalten geht nicht – Gespräch wurde ohne Video aufgebaut")
        } else if err != 0 {
            log("Video \(enabled ? "an" : "aus"): \(Self.errText(err))")
        }
        refreshCalls()
    }

    private var videoHosts: [VideoLayerHostView] = []

    /// Mac: Ansichten für Gegenstelle und Vorschau (NativeVideoView in CallToast/VideoWindow)
    func setVideoViews(remote: PlatformView?, preview: PlatformView?) {
        videoHosts.forEach { $0.removeFromSuperview() }
        videoHosts = []
        for (view, layer) in [(remote, VideoRenderer.shared.remoteLayer), (preview, VideoRenderer.shared.previewLayer)] {
            guard let view else { continue }
            let h = VideoLayerHostView(layer: layer)
            h.frame = view.bounds
            #if os(iOS)
            h.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            #endif
            view.addSubview(h)
            videoHosts.append(h)
        }
    }

    func call(withId callId: String) -> CallInfo? { calls.first { $0.id == callId } }

    /// Alle Gespräche beenden (CallKit-Reset)
    func terminateAll() { for id in entries.keys { _ = cmesip_hangup(id, 0) } }

    /// App kommt nach vorn: Anmeldung auffrischen (im Hintergrund schläft baresip, die TCP-Verbindung ist evtl. weg)
    func ensureRegistered() {
        guard started else { start(); return }
        guard accountActive else { return }
        _ = cmesip_network_changed()
    }

    /// Von Local Push: Anmeldung erneuern, damit Kamailio das INVITE zustellt
    func processPush(callId: String) {
        start()
        ensureRegistered()
    }

    // MARK: - iOS: Audio-Sitzung, PushKit

    #if os(iOS)
    /// Kategorie für Telefonate; aktiv schaltet CallKit die Sitzung (didActivate → audioSession(active:))
    private func configureAudioSession() {
        let s = AVAudioSession.sharedInstance()
        do {
            try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP, .allowBluetoothA2DP])
        } catch { log("Audio-Sitzung: \(error.localizedDescription)") }
    }

    /// CallKit hat die Audio-Sitzung aktiviert bzw. beendet: Audio-Units starten/stoppen
    func audioSession(active: Bool) {
        log("Audio-Sitzung \(active ? "aktiv" : "inaktiv") (CallKit)")
        _ = cmesip_audio_session(active)
    }

    private static let teamID = "83BLV56527"
    private static let voipTokenKey = "voipToken"
    private static var instanceUUID: String {
        let key = "sipInstanceUUID"
        if let u = UserDefaults.standard.string(forKey: key) { return u }
        let u = UUID().uuidString.lowercased()
        UserDefaults.standard.set(u, forKey: key)
        return u
    }

    private var pushRegistry: PKPushRegistry?
    private let pushDelegate = VoIPPushDelegate()
    /// Per VoIP-Push gemeldete Anrufe, deren INVITE noch fehlt (Call-ID -> Zeit)
    private var pushPending: [String: Date] = [:]
    /// Kürzlich beendete Anrufe: ein verspäteter Push dafür darf nicht mehr klingeln
    private var recentlyEnded: [String: Date] = [:]

    private func startPushKit() {
        guard pushRegistry == nil else { return }
        let r = PKPushRegistry(queue: .main)
        r.delegate = pushDelegate
        r.desiredPushTypes = [.voIP]
        pushRegistry = r
    }

    fileprivate func pushToken(_ token: String?) {
        UserDefaults.standard.set(token, forKey: Self.voipTokenKey)
        guard token != voipToken else { return }
        voipToken = token
        log("VoIP-Token \(token.map { String($0.prefix(8)) + "…" } ?? "entfernt")")
        if let token { LocalPushController.shared.update(deviceToken: token) }
        // Push-Parameter stehen im Contact: Konto neu setzen (ersetzt die Anmeldung, gleiche +sip.instance)
        if accountActive { addConfiguredAccount() }
    }

    /// VoIP-Push von Kamailio/push-relay: SOFORT an CallKit melden (sonst beendet iOS die App), dann neu anmelden –
    /// Kamailio wartet bis 30 s auf die Anmeldung und stellt das INVITE (gleiche Call-ID) dann zu.
    fileprivate func incomingPush(_ payload: [AnyHashable: Any]) {
        let aps = payload["aps"] as? [String: Any] ?? [:]
        let callId = aps["call-id"] as? String ?? payload["call-id"] as? String ?? UUID().uuidString
        let from = payload["from-uri"] as? String ?? ""
        let name = payload["display-name"] as? String ?? ""
        let handle = from.isEmpty ? "Unbekannt" : Self.user(of: from)
        CallCenter.shared.reportIncoming(callId: callId, handle: handle, displayName: name, hasVideo: false)
        log("VoIP-Push für \(callId.prefix(8)) von \(handle)")
        if let ended = recentlyEnded[callId], Date().timeIntervalSince(ended) < 120 {
            log("Push für bereits beendeten Anruf \(callId.prefix(8)) – beende sofort")
            CallCenter.shared.reportEnded(callId: callId, failed: false)
            CallCenter.shared.forget(callId: callId)
            return
        }
        if entries[callId] != nil { return }   // INVITE war schneller
        // Zweiter Push zum selben Anruf (weitere Anmeldung desselben Geräts bei Kamailio): nicht noch einmal
        // neu anmelden – jede Anmeldung lässt Kamailio das INVITE erneut zustellen
        if pushPending[callId] != nil { return }
        pushPending[callId] = Date()
        start()
        ensureRegistered()
        // Kommt das INVITE nicht (am anderen Gerät angenommen, Anrufer hat aufgelegt): CallKit-Anruf beenden
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.pushPending.removeValue(forKey: callId) != nil, self.entries[callId] == nil else { return }
                self.log("Kein INVITE zum Push \(callId.prefix(8)) – Anruf beendet")
                CallCenter.shared.reportEnded(callId: callId, failed: false)
                CallCenter.shared.forget(callId: callId)
            }
        }
    }
    #endif

    // MARK: - Ereignisse aus baresip

    fileprivate func handle(_ e: CmesipEvent) {
        switch e.kind {
        case CMESIP_LOG:
            let t = e.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { log("baresip: \(t)") }
        case CMESIP_TRACE:
            let first = e.text.prefix(while: { $0 != "\r" && $0 != "\n" })
            if awaitingUnregister, e.peer.hasPrefix("rx"), e.text.hasPrefix("SIP/2.0 200"), e.text.contains(" REGISTER\r") {
                unregisterAnswered = true
            }
            NSLog("[CMEPhone] SIP %@\n%@", e.peer, e.text)
            if !first.isEmpty, !first.hasPrefix("OPTIONS"), !(first.hasPrefix("SIP/2.0") && e.text.contains("CSeq: ") && e.text.contains(" OPTIONS")) {
                log("SIP \(e.peer): \(first)")
            }
        case CMESIP_REGISTER_OK:
            forbiddenRetry?.cancel(); forbiddenRetry = nil
            if registration != .ok { log("Registrierung: Ok \(e.text)") }
            registration = .ok
            subscribeOwnLine()
        case CMESIP_REGISTER_FAIL:
            log("Registrierung: Failed \(e.text)")
            registration = .failed(e.text)
            // Alte Anmeldung hält CME noch fest: alle 10 s neu versuchen, bis sie abgelaufen ist
            if settings.jabberMode, e.text.contains("403"), forbiddenRetry == nil {
                let item = DispatchWorkItem { [weak self] in
                    MainActor.assumeIsolated {
                        guard let self else { return }
                        self.forbiddenRetry = nil
                        self.log("Erneuter Anmeldeversuch nach 403")
                        _ = cmesip_register()
                    }
                }
                forbiddenRetry = item
                DispatchQueue.main.asyncAfter(deadline: .now() + 10, execute: item)
            }
        case CMESIP_UNREGISTERED:
            registration = .cleared
        case CMESIP_NOTIFY:
            if let id = Int(e.callId) { subHandlers[id]?.notify(e.text) }
        case CMESIP_SUB_CLOSED:
            if let id = Int(e.callId), let h = subHandlers.removeValue(forKey: id) { h.closed(e.text) }
        default:
            handleCallEvent(e)
        }
    }

    private func handleCallEvent(_ e: CmesipEvent) {
        let id = e.callId
        guard !id.isEmpty else { return }
        let callKit = CallCenter.shared
        var entry = entries[id]
        let newState: CallState?
        switch e.kind {
        case CMESIP_CALL_INCOMING:
            #if os(iOS)
            pushPending[id] = nil
            #endif
            if entry == nil {
                let c = Entry(id: id, remoteURI: e.peer, displayName: e.text, incoming: true, state: .IncomingReceived)
                entries[id] = c; order.append(id); entry = c
            }
            newState = .IncomingReceived
            callKit.reportIncoming(callId: id, handle: Self.party(of: e.peer), displayName: e.text, hasVideo: false)
        case CMESIP_CALL_OUTGOING:
            newState = .OutgoingProgress
            callKit.reportOutgoingStarted(callId: id)
        case CMESIP_CALL_RINGING: newState = .OutgoingRinging
        case CMESIP_CALL_PROGRESS: newState = entry?.incoming == true ? .IncomingEarlyMedia : .OutgoingEarlyMedia
        case CMESIP_CALL_ESTABLISHED:
            entry?.established = Date()
            newState = .StreamsRunning
            if entry?.incoming == false { callKit.reportOutgoingConnected(callId: id) }
            if micMuted { _ = cmesip_mute(id, true) }
            startDurationTimer()
        case CMESIP_CALL_HOLD:
            entry?.heldRemote = true; newState = .PausedByRemote
        case CMESIP_CALL_RESUME:
            entry?.heldRemote = false; newState = .StreamsRunning
        case CMESIP_CALL_MENC:
            newState = nil
        case CMESIP_CALL_DTMF:
            newState = nil
        case CMESIP_CALL_CLOSED:
            newState = .End
        default:
            newState = nil
        }
        guard let entry else { return }
        if !e.peer.isEmpty, entry.remoteURI.isEmpty { entry.remoteURI = e.peer }
        if let newState {
            entry.state = newState
            log("Call \(id.prefix(8)): \(newState) \(e.text)")
        }
        var info = [CChar](repeating: 0, count: 128)
        if newState != .End, cmesip_call_info(id, &info, info.count) == 0 { entry.info = String(cString: info) }
        refreshCalls()
        if let newState { notifyHandlers(entry, newState) }
        if newState == .End {
            callKit.reportEnded(callId: id, failed: false)
            #if os(iOS)
            recentlyEnded[id] = Date()
            recentlyEnded = recentlyEnded.filter { Date().timeIntervalSince($0.value) < 120 }
            #endif
            // wie liblinphone: kurz "End", dann "Released" und weg
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self, let c = self.entries[id] else { return }
                    c.state = .Released
                    self.refreshCalls()
                    self.notifyHandlers(c, .Released)
                    self.entries[id] = nil
                    self.order.removeAll { $0 == id }
                    self.refreshCalls()
                    callKit.forget(callId: id)
                }
            }
        }
    }

    private func notifyHandlers(_ c: Entry, _ state: CallState) {
        guard let info = calls.first(where: { $0.id == c.id }) else { return }
        callStateHandlers.forEach { $0(info, state) }
    }

    /// Gegenstelle für Anzeige und Anrufliste: Rufnummer bzw. bei Namen die ganze Adresse (konferenz@example.de),
    /// damit die Wahl aus der Anrufliste wieder dorthin geht
    static func party(of uri: String) -> String {
        let u = user(of: uri)
        guard u.contains(where: { $0.isLetter }) else { return u }
        var s = uri
        if let r = s.range(of: "<") { s = String(s[r.upperBound...]) }
        guard let at = s.firstIndex(of: "@") else { return u }
        let host = s[s.index(after: at)...].prefix { !";>:".contains($0) }
        return host.isEmpty ? u : "\(u)@\(host)".lowercased()
    }

    private static func user(of uri: String) -> String {
        var s = uri
        if let r = s.range(of: "<") { s = String(s[r.upperBound...]) }
        if let r = s.range(of: ":") { s = String(s[r.upperBound...]) }
        return s.components(separatedBy: CharacterSet(charactersIn: "@;>")).first ?? uri
    }

    /// "codec=G722/16000 menc=srtp" -> ("G722 16 kHz", "srtp")
    private static func parseInfo(_ s: String) -> (codec: String, menc: String) {
        var codec = "–", menc = "none"
        for part in s.split(separator: " ") {
            let kv = part.split(separator: "=", maxSplits: 1).map(String.init)
            guard kv.count == 2 else { continue }
            if kv[0] == "codec" {
                let c = kv[1].split(separator: "/").map(String.init)
                if c.count >= 2, c[0] != "-", let hz = Int(c[1]) { codec = "\(c[0]) \(hz / 1000) kHz" }
            } else if kv[0] == "menc" { menc = kv[1] }
        }
        return (codec, menc)
    }

    /// "vcodec=H264" aus cmesip_call_info
    private static func videoCodec(_ info: String) -> String? {
        for part in info.split(separator: " ") where part.hasPrefix("vcodec=") {
            let v = String(part.dropFirst(7))
            return v == "-" ? nil : v
        }
        return nil
    }

    func refreshCalls() {
        calls = order.compactMap { id in
            guard let c = entries[id] else { return nil }
            let (codec, menc) = Self.parseInfo(c.info)
            let duration = c.established.map { Int(Date().timeIntervalSince($0)) } ?? 0
            var details: [String] = []
            if c.established != nil {
                details.append("Audio: \(codec)")
                if let v = Self.videoCodec(c.info) { details.append("Video: \(v)") }
                details.append("Verschlüsselung: " + (menc == "srtp" ? "SRTP" : "keine"))
            }
            details.append("Gegenstelle: \(c.remoteURI)")
            var state = c.state
            if c.heldLocal, state == .StreamsRunning || state == .PausedByRemote { state = .Paused }
            return CallInfo(id: id, remote: Self.party(of: c.remoteURI), displayName: c.displayName, state: state,
                            isIncoming: c.incoming, hasVideo: c.established != nil && cmesip_call_video(id), isPaused: c.heldLocal,
                            audioCodec: codec, videoCodec: "–", duration: duration, isRecording: recordingCalls.contains(id),
                            mediaDetails: details)
        }
        recordingCalls.formIntersection(Set(calls.map(\.id)))
        updateRinger()
        if calls.isEmpty { durationTimer?.invalidate(); durationTimer = nil; speakerOn = false }
    }

    private func startDurationTimer() {
        guard durationTimer == nil else { return }
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            Task { @MainActor in SIPManager.shared.refreshCalls() }
        }
    }

    static func errText(_ err: Int32) -> String {
        String(cString: strerror(err)) + " (\(err))"
    }

    func log(_ line: String) {
        let df = DateFormatter(); df.dateFormat = "HH:mm:ss"
        lastLog.append("\(df.string(from: Date())) \(line)")
        if lastLog.count > 500 { lastLog.removeFirst(lastLog.count - 500) }
        #if os(macOS)
        // im Systemprotokoll lesbar (NSLog erscheint dort als <private>): log stream --predicate 'subsystem == "de.wiederhol.cmephone"'
        Self.osLog.notice("\(line, privacy: .public)")
        #else
        NSLog("[CMEPhone] %@", line)   // devicectl --console zeigt stderr
        #endif
    }
    #if os(macOS)
    private static let osLog = Logger(subsystem: "de.wiederhol.cmephone", category: "sip")
    #endif
}

#if os(iOS)
/// PushKit-Delegate (Queue .main): Token und eingehende VoIP-Pushes an den SIPManager
final class VoIPPushDelegate: NSObject, PKPushRegistryDelegate {
    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let hex = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        MainActor.assumeIsolated { SIPManager.shared.pushToken(hex) }
    }

    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        MainActor.assumeIsolated { SIPManager.shared.pushToken(nil) }
    }

    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                      for type: PKPushType, completion: @escaping () -> Void) {
        MainActor.assumeIsolated { SIPManager.shared.incomingPush(payload.dictionaryPayload) }
        completion()
    }
}
#endif

/// Ereignis aus dem baresip-Thread (Strings schon kopiert)
fileprivate struct CmesipEvent: Sendable {
    let kind: cmesip_event
    let callId: String
    let peer: String
    let text: String
}

/// C-Rückruf (baresip-Thread): nur kopieren und an den Main-Thread geben
private let cmesipCallback: @convention(c) (Int32, UnsafePointer<CChar>?, UnsafePointer<CChar>?,
                                            UnsafePointer<CChar>?, UnsafeMutableRawPointer?) -> Void = { ev, cid, peer, text, _ in
    let e = CmesipEvent(kind: cmesip_event(rawValue: UInt32(ev)),
                        callId: cid.map { String(cString: $0) } ?? "",
                        peer: peer.map { String(cString: $0) } ?? "",
                        text: text.map { String(cString: $0) } ?? "")
    DispatchQueue.main.async { MainActor.assumeIsolated { SIPManager.shared.handle(e) } }
}
