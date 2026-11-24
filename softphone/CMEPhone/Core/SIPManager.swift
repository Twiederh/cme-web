import Foundation
import Combine
import linphonesw
#if canImport(UIKit)
import UIKit
typealias PlatformView = UIView
#else
import AppKit
typealias PlatformView = NSView
#endif

/// Zentraler Wrapper um den liblinphone-Core: Konto, Codecs, Anrufe, Video.
/// Läuft komplett auf dem Main-Thread (liblinphone iteriert automatisch im Main-Loop).
/// Gemeinsam für iOS/iPadOS (CallKit) und macOS (eigene Anruf-UI über `CallCenter`).
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

    struct CallInfo: Identifiable, Equatable {
        let id: String            // SIP Call-ID
        var remote: String
        var displayName: String
        var state: Call.State
        var isIncoming: Bool
        var hasVideo: Bool
        var isPaused: Bool
        var audioCodec: String
        var videoCodec: String
        var duration: Int
        /// Lokale Aufnahme läuft (nur Softphone, interne Gespräche)
        var isRecording: Bool
    }

    @Published private(set) var registration: RegistrationStatus = .none
    @Published private(set) var calls: [CallInfo] = []
    @Published private(set) var micMuted = false
    @Published private(set) var speakerOn = false
    @Published private(set) var voipToken: String?
    @Published private(set) var lastLog: [String] = []

    private(set) var core: Core!
    private var coreDelegate: CoreDelegate!
    private let settings = PhoneSettings.shared
    private var durationTimer: Timer?
    private var iterateTimer: Timer?
    /// Zusätzliche Beobachter (z.B. Statusabo für das Tischtelefon auf macOS).
    private var callStateHandlers: [(Call, Call.State) -> Void] = []
    private var extraDelegates: [CoreDelegate] = []

    /// Zusätzlichen Core-Delegate anhängen (z.B. für NOTIFY des Statusabos).
    func addCoreDelegate(_ delegate: CoreDelegate) {
        extraDelegates.append(delegate)
        core?.addDelegate(delegate: delegate)
    }

    func addCallStateHandler(_ handler: @escaping (Call, Call.State) -> Void) {
        callStateHandlers.append(handler)
    }

    private init() {}

    // MARK: - Start

    func start() {
        guard core == nil else { return }
        LoggingService.Instance.logLevel = .Message
        let configDir = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: SharedConfig.appGroup)
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        let configPath = configDir.appendingPathComponent("linphonerc").path

        do {
            core = try Factory.Instance.createCore(configPath: configPath, factoryConfigPath: "", systemContext: nil)
        } catch {
            log("Core konnte nicht erstellt werden: \(error)")
            return
        }

        #if os(iOS)
        // CallKit übernimmt die AVAudioSession – liblinphone darf sie nicht selbst aktivieren.
        core.callkitEnabled = true
        // SDK verwaltet PushKit selbst und fügt RFC-8599-Parameter (pn-provider/pn-prid/pn-param) in den Contact ein.
        core.pushNotificationEnabled = true
        #else
        // macOS: kein PushKit – der Mac bleibt dauerhaft registriert.
        core.pushNotificationEnabled = false
        // macOS: liblinphone selbst takten. Das eingebaute Auto-Iterate läuft in der Menüleisten-App nicht
        // zuverlässig (ohne Iterate wird nie ein REGISTER verschickt). Modus .common, damit es auch bei
        // geöffnetem Menü weiterläuft.
        core.autoIterateEnabled = false
        let t = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.core?.iterate() }
        }
        RunLoop.main.add(t, forMode: .common)
        iterateTimer = t
        #endif
        #if os(macOS)
        // Wie Jabber: gleichbleibender Contact (feste UDP-Adresse + feste +sip.instance). CME lehnt eine
        // Anmeldung mit 403 "Registration is active for another client" ab, solange die vorige Anmeldung
        // desselben Pools von einem anderen Contact noch gilt – wechselnde Ports/IDs würden das bei jedem
        // Neustart auslösen. 5060 bleibt frei (Cisco Jabber).
        let uuidKey = "sipInstanceUUID"
        let uuid = UserDefaults.standard.string(forKey: uuidKey) ?? {
            let u = UUID().uuidString.lowercased()
            UserDefaults.standard.set(u, forKey: uuidKey)
            return u
        }()
        core.config?.setString(section: "misc", key: "uuid", value: uuid)
        if let t = core.transports {
            t.udpPort = settings.jabberMode ? 5065 : -1
            t.tcpPort = -1
            try? core.setTransports(newValue: t)
        }
        #endif
        core.keepAliveEnabled = true
        core.ipv6Enabled = true
        // RFC 2833 / rtp-nte – passt zu "dtmf-relay rtp-nte" in CME.
        core.useRfc2833ForDtmf = true
        core.useInfoForDtmf = false
        // CME schickt häufig Delayed Offer (INVITE ohne SDP) – liblinphone kommt damit klar.
        core.ringDuringIncomingEarlyMedia = false

        coreDelegate = CoreDelegateStub(
            // liblinphone ruft Delegates auf dem Main-Thread auf. Synchron weiterreichen (kein Task-Hop!),
            // denn nach einem VoIP-Push MUSS CallKit noch innerhalb des PushKit-Callbacks informiert werden.
            onCallStateChanged: { [weak self] core, call, state, message in
                MainActor.assumeIsolated { self?.onCallState(call: call, state: state, message: message) }
            },
            onAccountRegistrationStateChanged: { [weak self] core, account, state, message in
                MainActor.assumeIsolated { self?.onRegistration(account: account, state: state, message: message) }
            }
        )
        core.addDelegate(delegate: coreDelegate)
        for d in extraDelegates { core.addDelegate(delegate: d) }

        applyMediaSettings()

        do { try core.start() } catch { log("Core-Start fehlgeschlagen: \(error)"); return }
        log("CME Phone \(BuildInfo.text), liblinphone \(Core.getVersion) gestartet")
        applyAccount()
    }

    // MARK: - Konto

    private var applyGeneration = 0
    /// Jabber-Modus: CME lehnt eine neue Anmeldung (neue Call-ID) mit "403 Registration is active for another
    /// client" ab, solange eine frühere noch gilt – etwa nach einem Absturz. Ein Abmelden mit neuer Call-ID
    /// beantwortet CME mit 404. Deshalb kurze Anmeldedauer (s. jabberExpires) und bei 403 regelmäßig neu versuchen.
    private static let jabberExpires = 120
    private var forbiddenRetry: DispatchWorkItem?

    func applyAccount() {
        guard let core else { return }
        applyGeneration += 1
        let generation = applyGeneration
        // Bestehende Konten entfernen (wir haben genau eines). Ein angemeldetes Konto meldet sich dabei ab.
        // CME kennt pro Pool nur EINE Anmeldung: Läuft die Abmeldung des alten Kontos (z.B. andere Domain)
        // nach der neuen Anmeldung durch, ist der Mac beim CME abgemeldet. Deshalb erst abmelden lassen,
        // dann neu anmelden.
        lineEvent?.terminate()
        lineEvent = nil
        lineAccount = nil
        let old = core.accountList
        let wasRegistered = old.contains { $0.state == .Ok || $0.state == .Progress }
        for acc in old { core.removeAccount(account: acc) }
        registration = .none
        forbiddenRetry?.cancel(); forbiddenRetry = nil
        if wasRegistered {
            log("Konto wird neu angemeldet – alte Anmeldung wird zuerst beendet")
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self, self.applyGeneration == generation else { return }
                    self.addConfiguredAccount()
                }
            }
        } else {
            addConfiguredAccount()
        }
    }

    private func addConfiguredAccount() {
        guard let core else { return }
        core.clearAllAuthInfo()
        guard settings.isConfigured else { log("Kein Konto konfiguriert"); return }

        do {
            let identity = try Factory.Instance.createAddress(addr: "sip:\(settings.extensionNumber)@\(settings.sipHost)")
            // Jabber schickt keinen Anzeigenamen in From/To (den Namen kennt CME aus "name" am dn)
            if !settings.displayName.isEmpty && !settings.jabberMode { try identity.setDisplayname(newValue: settings.displayName) }
            // Jabber meldet sich als "User-Agent: Cisco-CSF"
            core.setUserAgent(name: settings.jabberMode ? "Cisco-CSF" : "CME Phone", version: nil)

            let params = try core.createAccountParams()
            try params.setIdentityaddress(newValue: identity)

            let serverHost: String
            let serverPort: Int
            let viaGateway = settings.useGateway && !settings.jabberMode
            if viaGateway {
                serverHost = settings.gatewayHost; serverPort = settings.gatewayPort
            } else {
                serverHost = settings.sipHost; serverPort = 5060
            }
            let server = try Factory.Instance.createAddress(addr: "sip:\(serverHost):\(serverPort)")
            // Jabber meldet sich per UDP direkt an der CME an
            try server.setTransport(newValue: settings.jabberMode ? .Udp : settings.transport.linphone)
            try params.setServeraddress(newValue: server)
            // Bei Gateway-Betrieb: alle Requests über Flexisip (Outbound-Proxy), Request-URI bleibt CME.
            params.outboundProxyEnabled = viaGateway
            params.registerEnabled = true
            params.expires = settings.jabberMode ? Self.jabberExpires : 600
            // CME nimmt im CME-Modus nur MAC-Adressen oder - für Softclients wie Jabber - einen Gerätenamen an.
            // cme-web legt die App als Pool "id device-id-name CSF<Nebenstelle>" an; den Namen schickt die App
            // wie Jabber im Contact mit.
            params.contactParameters = "+u.sip!devicename.ccm.cisco.com=\"\(settings.deviceName)\""
                + (settings.ciscoLineMode ? ";+u.sip!model.ccm.cisco.com=\"503\"" : "")

            #if os(iOS)
            // Push: VoIP-Push (PushKit) erlauben; Remote-Push (Banner) nicht nötig.
            params.pushNotificationAllowed = viaGateway
            params.remotePushNotificationAllowed = false
            #if DEBUG
            params.pushNotificationConfig?.provider = "apns.dev"
            #else
            params.pushNotificationConfig?.provider = "apns"
            #endif
            #else
            params.pushNotificationAllowed = false
            params.remotePushNotificationAllowed = false
            #endif

            if !settings.password.isEmpty {
                // CME (Soft-Client-Pool): username <nummer ohne +> password <…> – Realm/Domain offen lassen
                // liblinphone sucht die Zugangsdaten über den Benutzer der Identität (+49…); der abweichende
                // Anmeldename für CME (z.B. CSF…-2) gehört in "userid".
                let authId = settings.authUser.isEmpty ? String(settings.extensionNumber.drop(while: { $0 == "+" })) : settings.authUser
                let auth = try Factory.Instance.createAuthInfo(
                    username: settings.extensionNumber, userid: authId, passwd: settings.password,
                    ha1: nil, realm: nil, domain: nil)
                core.addAuthInfo(info: auth)
            }

            let account = try core.createAccount(params: params)
            if settings.ciscoLineMode {
                // Wie ein Cisco-Telefon (8845: "Supported: …,X-cisco-sis-7.0.0,…"). CME antwortet dann mit
                // X-cisco-cme-sis und behandelt das Gerät als Cisco-Endgerät (Shared Line, dialog-Abo).
                account.addCustomHeader(headerName: "Supported", headerValue: Self.ciscoSupported)
            }
            try core.addAccount(account: account)
            core.defaultAccount = account
            log("Konto \(identity.asStringUriOnly()) über \(server.asStringUriOnly())")
        } catch {
            log("Konto-Fehler: \(error)")
            registration = .failed("\(error)")
        }
    }

    func refreshRegistration() { core?.refreshRegisters() }

    /// Abmelden und kurz warten, bis CME es bestätigt (beim Beenden der App).
    func unregisterBlocking(timeout: TimeInterval) {
        guard let core else { return }
        lineEvent?.terminate()
        lineEvent = nil
        for acc in core.accountList {
            guard let p = acc.params?.clone() else { continue }
            p.registerEnabled = false
            acc.params = p
        }
        log("Beenden: Konten \(core.accountList.map { "\($0.state)" }.joined(separator: ","))")
        let end = Date().addingTimeInterval(timeout)
        repeat {
            core.iterate()
            Thread.sleep(forTimeInterval: 0.02)
        } while Date() < end && core.accountList.contains(where: { $0.state == .Ok || $0.state == .Progress })
        log("Beim Beenden abgemeldet: \(core.accountList.map { "\($0.state)" }.joined(separator: ","))")
        core.stop()
    }

    // MARK: - Cisco-Leitungsmodus (experimentell)

    /// Genau wie Cisco Jabber für Mac 15.3 (REGISTER an CME). "X-cisco-duplicate-reg" sorgt dafür, dass CME eine
    /// erneute Anmeldung desselben Geräts nicht als Duplikat behandelt (sonst fehlt es bei Shared Lines).
    static let ciscoSupported = "replaces,join,sdp-anat,norefersub,resource-priority,extended-refer,"
        + "X-cisco-callinfo,X-cisco-serviceuri,X-cisco-escapecodes,X-cisco-service-control,X-cisco-srtp-fallback,"
        + "X-cisco-monrec,X-cisco-config,X-cisco-sis-7.0.0,X-cisco-xsi-8.5.1,X-cisco-graceful-reg,X-cisco-duplicate-reg"
    private var lineEvent: Event?
    private var lineEventDelegate: EventDelegateStub?
    /// Hilfskonto nur für das dialog-Abo (meldet sich nicht an): CME trägt einen Abonnenten unter dem From-URI
    /// in die Shared Line ein ("+49…@192.168.11.46" beim 8845) und sucht den Dial-Peer über dessen Host.
    /// Das 8845 schickt deshalb seine eigene IP im From – mit der CME-Adresse im From (wie beim REGISTER)
    /// nimmt CME das Abo zwar an, führt das Gerät aber nicht als Teilnehmer und lässt es nicht mitklingeln.
    private var lineAccount: Account?

    private func lineProxy() -> ProxyConfig? {
        guard let core, let main = core.defaultAccount,
              let host = main.contactAddress?.domain, !host.isEmpty else { return nil }
        if lineAccount == nil {
            do {
                let p = try core.createAccountParams()
                try p.setIdentityaddress(newValue: try Factory.Instance.createAddress(addr: "sip:\(settings.extensionNumber)@\(host)"))
                let server = try Factory.Instance.createAddress(addr: "sip:\(settings.sipHost):5060")
                try server.setTransport(newValue: .Udp)
                try p.setServeraddress(newValue: server)
                p.registerEnabled = false
                p.pushNotificationAllowed = false
                p.remotePushNotificationAllowed = false
                p.contactParameters = main.params?.contactParameters
                let acc = try core.createAccount(params: p)
                try core.addAccount(account: acc)
                core.defaultAccount = main
                lineAccount = acc
                log("Cisco-Leitungsmodus: Abo-Absender sip:\(settings.extensionNumber)@\(host)")
            } catch {
                log("Cisco-Leitungsmodus: Hilfskonto fehlgeschlagen: \(error)")
                return nil
            }
        }
        return core.proxyConfigList.first { $0.identityAddress?.domain == host }
    }

    /// Wie das 8845: nach der Anmeldung die eigene Leitung per "dialog" abonnieren.
    private func subscribeOwnLine() {
        guard settings.ciscoLineMode, let core, lineEvent == nil, !settings.extensionNumber.isEmpty else { return }
        do {
            let me = try Factory.Instance.createAddress(addr: "sip:\(settings.extensionNumber)@\(settings.sipHost)")
            // Über das Konto abonnieren: dann trägt das SUBSCRIBE dessen Contact (mit Gerätename) und wird bei
            // 401 mit den Zugangsdaten des Pools wiederholt – wie beim 8845. Nur dann führt CME das Gerät in
            // "show shared-line details" und lässt es bei einer Shared Line mitklingeln.
            let ev: Event
            if let proxy = lineProxy() {
                ev = try core.createSubscribe(resource: me, proxy: proxy, event: "dialog", expires: 7200)
            } else if let proxy = core.defaultProxyConfig {
                ev = try core.createSubscribe(resource: me, proxy: proxy, event: "dialog", expires: 7200)
            } else {
                ev = try core.createSubscribe(resource: me, event: "dialog", expires: 7200)
            }
            ev.addCustomHeader(name: "Accept", value: "application/dialog-info+xml")
            ev.addCustomHeader(name: "Allow", value: "ACK,BYE,CANCEL,INVITE,NOTIFY,OPTIONS,REFER,REGISTER,UPDATE,SUBSCRIBE")
            // Wie 8845/Jabber den Gerätenamen auch im Contact des SUBSCRIBE (liblinphone setzt die Contact-Parameter
            // des Kontos nur beim REGISTER). CME ordnet das Abo darüber dem Pool zu.
            if let c = core.defaultAccount?.contactAddress, let host = c.domain, !host.isEmpty {
                let port = c.port > 0 ? ":\(c.port)" : ""
                ev.addCustomHeader(name: "Contact",
                                   value: "<sip:\(settings.extensionNumber)@\(host)\(port);transport=udp>;"
                                   + "+u.sip!devicename.ccm.cisco.com=\"\(settings.deviceName)\";+u.sip!model.ccm.cisco.com=\"503\"")
            }
            let d = EventDelegateStub(
                onSubscribeStateChanged: { [weak self] ev, state in
                    MainActor.assumeIsolated {
                        let code = ev.errorInfo?.protocolCode ?? 0
                        self?.log("Cisco-Leitungsmodus: Abo \(state)" + (code > 0 ? " (\(code))" : ""))
                    }
                })
            ev.addDelegate(delegate: d)
            lineEventDelegate = d
            try ev.sendSubscribe(body: nil)
            lineEvent = ev
            log("Cisco-Leitungsmodus: Abo dialog auf \(settings.extensionNumber), über das Konto")
        } catch {
            log("Cisco-Leitungsmodus: Abo fehlgeschlagen: \(error)")
        }
    }

    // MARK: - Medien / Codecs

    func applyMediaSettings() {
        guard let core else { return }
        core.audioPayloadTypes = Self.ordered(core.audioPayloadTypes, by: settings.audioCodecs, audio: true)
        core.videoPayloadTypes = Self.ordered(core.videoPayloadTypes, by: settings.videoCodecs, audio: false)

        core.videoCaptureEnabled = settings.videoEnabled
        core.videoDisplayEnabled = settings.videoEnabled
        if let policy = try? Factory.Instance.createVideoActivationPolicy() {
            policy.automaticallyAccept = settings.videoEnabled
            policy.automaticallyInitiate = false   // Video wird pro Anruf gestartet
            core.videoActivationPolicy = policy
        }
        // Frontkamera als Standard
        if let front = core.videoDevicesList.first(where: { $0.contains("built-in_video:1") }) {
            try? core.setVideodevice(newValue: front)
        }
        do {
            try core.setMediaencryption(newValue: settings.mediaEncryption == .srtp ? .SRTP : .None)
            core.mediaEncryptionMandatory = false
        } catch { log("Media-Encryption: \(error)") }

        let a = core.audioPayloadTypes.filter { $0.enabled() }.map { "\($0.mimeType)/\($0.clockRate)" }
        let v = core.videoPayloadTypes.filter { $0.enabled() }.map { $0.mimeType }
        log("Audio: \(a.joined(separator: ", ")) | Video: \(v.joined(separator: ", "))")
    }

    /// Sortiert die vorhandenen Payload-Types nach der gewünschten Reihenfolge,
    /// aktiviert die gewählten und deaktiviert alle anderen (Speex, GSM, iLBC, VP8, AV1 …).
    private static func ordered(_ available: [PayloadType], by wanted: [CodecEntry], audio: Bool) -> [PayloadType] {
        var result: [PayloadType] = []
        for entry in wanted {
            let matches = available.filter { pt in
                guard pt.mimeType.caseInsensitiveCompare(entry.mime) == .orderedSame else { return false }
                // Opus: nur 48 kHz; G.711/G.722: 8 kHz (G.722 meldet per RFC 3551 8000)
                if audio && entry.mime.lowercased() == "opus" { return pt.clockRate == 48000 }
                return true
            }
            for pt in matches {
                _ = pt.enable(enabled: entry.enabled)
                result.append(pt)
            }
        }
        // telephone-event (DTMF) muss aktiv bleiben
        for pt in available where !result.contains(where: { $0 === pt }) {
            let keep = audio && pt.mimeType.lowercased() == "telephone-event" && pt.clockRate == 8000
            _ = pt.enable(enabled: keep)
            result.append(pt)
        }
        return result
    }

    // MARK: - Anrufe

    /// Wird vom CallKit-Provider (CXStartCallAction) aufgerufen.
    @discardableResult
    func placeCall(to number: String, video: Bool) -> Call? {
        guard let core else { return nil }
        let target = number.contains("@") ? number : "sip:\(number)@\(settings.sipHost)"
        do {
            let addr = try Factory.Instance.createAddress(addr: target.hasPrefix("sip") ? target : "sip:\(target)")
            let params = try core.createCallParams(call: nil)
            params.videoEnabled = video && settings.videoEnabled
            params.mediaEncryption = settings.mediaEncryption == .srtp ? .SRTP : .None
            params.recordFile = Self.recordingPath(remote: number)
            #if os(iOS)
            core.configureAudioSession()
            #endif
            return core.inviteAddressWithParams(addr: addr, params: params)
        } catch {
            log("Anruf fehlgeschlagen: \(error)")
            return nil
        }
    }

    func call(withId callId: String) -> Call? {
        core?.calls.first { $0.callLog?.callId == callId }
    }

    func accept(callId: String) {
        guard let core, let call = call(withId: callId) else { return }
        do {
            let params = try core.createCallParams(call: call)
            params.videoEnabled = settings.videoEnabled && (call.remoteParams?.videoEnabled ?? false)
            params.recordFile = Self.recordingPath(remote: call.remoteAddress?.username ?? "unbekannt")
            #if os(iOS)
            core.configureAudioSession()
            #endif
            try call.acceptWithParams(params: params)
        } catch { log("Annehmen fehlgeschlagen: \(error)") }
    }

    func terminate(callId: String) {
        guard let call = call(withId: callId) else { return }
        try? call.terminate()
    }

    /// Eingehenden Anruf abweisen.
    /// toVoicemail: 486 Busy – CME leitet dann per "call-forward b2bua busy" (setzt cme-web für Nummern mit
    /// Postfach) an die Voicemail. Sonst 603 Decline.
    #if os(macOS)
    private var defaultRing: String?
    /// Telefonsteuerung: Mac klingelt über die Shared Line nur stumm mit (Toast), es klingelt das Tischtelefon.
    func setRingerEnabled(_ on: Bool) {
        guard let core else { return }
        if defaultRing == nil { defaultRing = core.ring ?? "" }
        core.ring = on ? (defaultRing?.isEmpty == false ? defaultRing : nil) : nil
    }
    #endif

    func decline(callId: String, toVoicemail: Bool) {
        guard let call = call(withId: callId) else { return }
        // An die Voicemail: per 302 auf die Voicemail-Nummer umleiten. CME leitet dann den GANZEN Anruf um –
        // auch bei einem Sammelanschluss, an dem weitere Geräte klingeln (ein "besetzt" beendete nur diesen
        // Zweig). Asterisk findet das Postfach über die Umleitungsinfo (Diversion: diese Nebenstelle).
        if toVoicemail, !settings.voicemailNumber.isEmpty {
            do {
                let target = try Factory.Instance.createAddress(addr: "sip:\(settings.voicemailNumber)@\(settings.sipHost)")
                try call.redirectTo(redirectAddress: target)
                log("An Voicemail umgeleitet (\(settings.voicemailNumber))")
                return
            } catch {
                log("Umleiten an Voicemail fehlgeschlagen: \(error) – lehne mit besetzt ab")
            }
        }
        do { try call.decline(reason: toVoicemail ? .Busy : .Declined) } catch {
            log("Abweisen: \(error)")
            try? call.terminate()
        }
    }

    // MARK: - Lokale Aufnahme (interne Gespräche; externe nimmt cme-recorder auf)

    /// Ordner für lokale Aufnahmen: macOS ~/Music/CME Phone, iOS Dokumente/Aufnahmen
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

    private static func recordingPath(remote: String) -> String {
        let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let who = remote.filter { $0.isNumber || $0 == "+" }
        return recordingsFolder.appendingPathComponent("\(df.string(from: Date())) \(who.isEmpty ? "Anruf" : who).wav").path
    }

    private var recordingCalls = Set<String>()

    func setLocalRecording(callId: String, on: Bool) {
        guard let call = call(withId: callId) else { return }
        if on { call.startRecording(); recordingCalls.insert(callId) } else { call.stopRecording(); recordingCalls.remove(callId) }
        log("Lokale Aufnahme \(on ? "an" : "aus"): \(call.params?.recordFile ?? "")")
        refreshCalls()
    }

    func setHeld(callId: String, held: Bool) {
        guard let call = call(withId: callId) else { return }
        do { if held { try call.pause() } else { try call.resume() } } catch { log("Halten: \(error)") }
    }

    func setMuted(_ muted: Bool) {
        core?.micEnabled = !muted
        micMuted = muted
    }

    func sendDTMF(_ digits: String, callId: String? = nil) {
        guard let call = callId.flatMap({ self.call(withId: $0) }) ?? core?.currentCall else { return }
        for ch in digits { try? call.sendDtmf(dtmf: CChar(ch.asciiValue ?? 0)) }
    }

    func toggleSpeaker() {
        #if os(iOS)
        guard let core else { return }
        let wantSpeaker = !speakerOn
        if let dev = core.audioDevices.first(where: { $0.type == (wantSpeaker ? .Speaker : .Microphone) }) {
            core.outputAudioDevice = dev
            speakerOn = wantSpeaker
        }
        #endif
    }

    /// Audio-Geräte (v.a. macOS: Headset, Studio Display, Mac-Lautsprecher).
    var audioDevices: [AudioDevice] { core?.extendedAudioDevices ?? core?.audioDevices ?? [] }

    /// Gespeicherte Audiogeräte (Einstellungen) setzen; leer = Systemstandard.
    func applySavedAudioDevices() {
        guard let core else { return }
        core.reloadSoundDevices()
        let devs = audioDevices
        if let i = devs.first(where: { $0.id == settings.audioInputId && $0.hasCapability(capability: .CapabilityRecord) }) {
            core.defaultInputAudioDevice = i; core.inputAudioDevice = i
        }
        if let o = devs.first(where: { $0.id == settings.audioOutputId && $0.hasCapability(capability: .CapabilityPlay) }) {
            core.defaultOutputAudioDevice = o; core.outputAudioDevice = o
        }
    }

    func selectAudio(input: AudioDevice?, output: AudioDevice?) {
        if let input { core?.inputAudioDevice = input; core?.defaultInputAudioDevice = input }
        if let output { core?.outputAudioDevice = output; core?.defaultOutputAudioDevice = output }
    }

    func setVideo(callId: String, enabled: Bool) {
        guard let core, let call = call(withId: callId) else { return }
        do {
            let params = try core.createCallParams(call: call)
            params.videoEnabled = enabled
            try call.update(params: params)
        } catch { log("Video-Update: \(error)") }
    }

    func switchCamera() {
        guard let core else { return }
        let current = core.videoDevice ?? ""
        let cams = core.videoDevicesList.filter { !$0.contains("StaticImage") }
        guard let idx = cams.firstIndex(of: current), cams.count > 1 else { return }
        try? core.setVideodevice(newValue: cams[(idx + 1) % cams.count])
    }

    /// Blind Transfer (REFER) – CME benötigt dafür "transfer-system full-consult" bzw. "transfer-pattern".
    func transfer(callId: String, to number: String) {
        guard let call = call(withId: callId) else { return }
        do {
            let addr = try Factory.Instance.createAddress(addr: "sip:\(number)@\(settings.sipHost)")
            try call.transferTo(referTo: addr)
        } catch { log("Transfer: \(error)") }
    }

    func setVideoViews(remote: PlatformView?, preview: PlatformView?) {
        guard let core else { return }
        #if os(iOS)
        // iOS-Wrapper erwartet direkt UIView
        core.nativeVideoWindow = remote
        core.nativePreviewWindow = preview
        #else
        // macOS-Wrapper erwartet einen Zeiger auf die NSView
        core.nativeVideoWindow = remote.map { Unmanaged.passUnretained($0).toOpaque() }
        core.nativePreviewWindow = preview.map { Unmanaged.passUnretained($0).toOpaque() }
        #endif
    }

    /// Von Local Push: holt den Anruf (INVITE) aktiv ab, Flexisip liefert ihn per fork-late aus.
    func processPush(callId: String) {
        start()
        core?.processPushNotification(callId: callId)
        core?.ensureRegistered()
    }

    // MARK: - Delegates

    private func onRegistration(account: Account, state: RegistrationState, message: String) {
        if let line = lineAccount, line.getCobject == account.getCobject { return }
        // Abmeldung eines entfernten Kontos nicht als Zustand des aktuellen Kontos anzeigen
        if let core, !core.accountList.contains(where: { $0.getCobject == account.getCobject }) {
            log("Alte Anmeldung: \(state) \(message)")
            return
        }
        // Alte Anmeldung hält CME noch fest: alle 20 s neu versuchen, bis sie abgelaufen ist (≤ jabberExpires)
        if state == .Failed, settings.jabberMode, message.contains("Forbidden"), forbiddenRetry == nil {
            let item = DispatchWorkItem { [weak self] in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.forbiddenRetry = nil
                    self.log("Erneuter Anmeldeversuch nach 403")
                    self.core?.refreshRegisters()
                }
            }
            forbiddenRetry = item
            DispatchQueue.main.asyncAfter(deadline: .now() + 20, execute: item)
        }
        switch state {
        case .Progress: registration = .progress
        case .Ok:
            registration = .ok
            subscribeOwnLine()
            if let tok = account.params?.pushNotificationConfig?.voipToken, !tok.isEmpty {
                if voipToken != tok {
                    voipToken = tok
                    #if os(iOS)
                    LocalPushController.shared.update(deviceToken: tok)
                    #endif
                }
            }
        case .Failed: registration = .failed(message)
        case .Cleared: registration = .cleared
        default: registration = .none
        }
        log("Registrierung: \(state) \(message)")
    }

    private func onCallState(call: Call, state: Call.State, message: String) {
        let callId = call.callLog?.callId ?? ""
        log("Call \(callId.prefix(8)): \(state) \(message)")
        let callKit = CallCenter.shared

        switch state {
        case .PushIncomingReceived, .IncomingReceived:
            // Muss sofort an CallKit gemeldet werden (sonst beendet iOS die App nach VoIP-Push).
            callKit.reportIncoming(callId: callId,
                                   handle: call.remoteAddress?.username ?? "Unbekannt",
                                   displayName: call.remoteAddress?.displayName,
                                   hasVideo: call.remoteParams?.videoEnabled ?? false)
        case .OutgoingProgress:
            callKit.reportOutgoingStarted(callId: callId, call: call)
        case .Connected:
            if call.dir == .Outgoing { callKit.reportOutgoingConnected(callId: callId) }
            startDurationTimer()
        case .End, .Error:
            callKit.reportEnded(callId: callId, failed: state == .Error)
        case .Released:
            callKit.forget(callId: callId)
        default: break
        }
        refreshCalls()
        callStateHandlers.forEach { $0(call, state) }
    }

    func refreshCalls() {
        guard let core else { calls = []; return }
        calls = core.calls.compactMap { c in
            guard let id = c.callLog?.callId else { return nil }
            let params = c.currentParams
            return CallInfo(
                id: id,
                remote: c.remoteAddress?.username ?? c.remoteAddress?.asStringUriOnly() ?? "?",
                displayName: c.remoteAddress?.displayName ?? "",
                state: c.state,
                isIncoming: c.dir == .Incoming,
                hasVideo: params?.videoEnabled ?? false,
                isPaused: c.state == .Paused || c.state == .Pausing,
                audioCodec: params?.usedAudioPayloadType.map { "\($0.mimeType) \($0.clockRate / 1000) kHz" } ?? "–",
                videoCodec: params?.usedVideoPayloadType?.mimeType ?? "–",
                duration: c.duration,
                isRecording: recordingCalls.contains(id))
        }
        recordingCalls.formIntersection(Set(calls.map(\.id)))
        if calls.isEmpty { durationTimer?.invalidate(); durationTimer = nil; speakerOn = false }
    }

    private func startDurationTimer() {
        guard durationTimer == nil else { return }
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshCalls() }
        }
    }

    func log(_ line: String) {
        let df = DateFormatter(); df.dateFormat = "HH:mm:ss"
        lastLog.append("\(df.string(from: Date())) \(line)")
        if lastLog.count > 300 { lastLog.removeFirst(lastLog.count - 300) }
        NSLog("[CMEPhone] %@", line)
    }
}

extension SIPTransport {
    var linphone: TransportType {
        switch self { case .udp: return .Udp; case .tcp: return .Tcp; case .tls: return .Tls }
    }
}
