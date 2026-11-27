import Foundation
import Combine

/// Schnittstelle für die Fernsteuerung eines Tischtelefons.
/// Heute: Cisco XML Execute (funktioniert mit CME + SIP-Telefonen).
/// Später möglich: JTAPI/CTI über CUCM – gleiche Schnittstelle, andere Implementierung.
@MainActor
protocol DeskPhoneBackend: AnyObject {
    func dial(_ number: String) async -> CiscoXML.Result
    func answer() async -> CiscoXML.Result
    func hangup() async -> CiscoXML.Result
    func decline() async -> CiscoXML.Result
    func hold() async -> CiscoXML.Result
    func sendDigits(_ digits: String) async -> CiscoXML.Result
    func execute(_ uri: String) async -> CiscoXML.Result
}

/// Steuert ein Cisco-SIP-Telefon (88xx, 78xx, 69xx …) über HTTP POST /CGI/Execute.
/// Voraussetzung in CME: Web-Zugriff am Telefon + `url authentication` (siehe cme/deskphone-control.cfg).
@MainActor
final class CiscoXMLBackend: DeskPhoneBackend {
    private let settings: PhoneSettings
    private let session: URLSession

    init(settings: PhoneSettings = .shared) {
        self.settings = settings
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        cfg.httpCookieStorage = nil
        session = URLSession(configuration: cfg)
    }

    func dial(_ number: String) async -> CiscoXML.Result { await send([.dial(number)]) }
    func answer() async -> CiscoXML.Result { await send([.key(settings.keyAnswer)]) }
    func hangup() async -> CiscoXML.Result { await send([.key(settings.keyHangup)]) }
    func decline() async -> CiscoXML.Result { await send([.key(settings.keyDecline)]) }
    func hold() async -> CiscoXML.Result { await send([.key(settings.keyHold)]) }
    func sendDigits(_ digits: String) async -> CiscoXML.Result {
        await send([.sendDigits(digits, line: settings.deskPhoneLine)])
    }
    func execute(_ uri: String) async -> CiscoXML.Result { await send([.raw(uri)]) }

    private func send(_ commands: [CiscoXML.Command]) async -> CiscoXML.Result {
        let host = settings.deskPhoneHost.trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty, let url = URL(string: "http://\(host)/CGI/Execute") else {
            return .unexpected("Keine Telefon-Adresse eingestellt")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let cred = Data("\(settings.deskPhoneUser):\(settings.deskPhonePassword)".utf8).base64EncodedString()
        req.setValue("Basic \(cred)", forHTTPHeaderField: "Authorization")
        req.httpBody = CiscoXML.formBody(commands)
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode == 401 || http.statusCode == 403 {
                return .error(number: 4, text: CiscoXML.errorText(4))
            }
            return CiscoXML.parseResponse(data)
        } catch {
            return .unexpected(error.localizedDescription)
        }
    }
}

/// Fasst Steuerung + Statusabo zusammen und liefert den Zustand fürs UI.
@MainActor
final class DeskPhoneController: ObservableObject {
    static let shared = DeskPhoneController()

    @Published private(set) var status: LineStatus = .unknown
    @Published private(set) var subscriptionState = "–"
    @Published private(set) var lastResult = ""
    @Published private(set) var busy = false
    /// Vom Mac aus am Tischtelefon gewählte Nummer – zeigt den Toast sofort (presence meldet erst
    /// "belegt" und nie die Nummer). Endet mit Auflegen, wenn die Leitung wieder frei ist, oder nach 60 s
    /// ohne Gespräch.
    @Published private(set) var pendingDial: String?
    private var pendingTimer: Timer?

    var backend: DeskPhoneBackend
    private let settings = PhoneSettings.shared
    private var subId: Int?
    private var retry: DispatchWorkItem?
    private var lastNotifyBody = ""

    private init() {
        backend = CiscoXMLBackend()
    }

    // MARK: Befehle

    func dial(_ number: String) {
        setPendingDial(number)
        run("Wählen \(number)", onFail: { [weak self] in self?.setPendingDial(nil) }) { await $0.dial(number) }
    }

    func setPendingDial(_ number: String?) {
        pendingTimer?.invalidate()
        pendingDial = number
        guard number != nil else { return }
        pendingTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated {
                // Kein Gespräch zustande gekommen (oder kein Statusabo): Toast nicht ewig stehen lassen
                guard let self, self.status.state != .busy else { return }
                self.setPendingDial(nil)
            }
        }
    }
    func answer() { run("Annehmen") { await $0.answer() } }
    func hangup() {
        setPendingDial(nil)
        run("Auflegen") { await $0.hangup() }
    }
    func decline() { run("Ablehnen") { await $0.decline() } }
    func hold() { run("Halten") { await $0.hold() } }
    func sendDigits(_ d: String) { run("Ziffern \(d)") { await $0.sendDigits(d) } }
    func execute(_ uri: String) { run(uri) { await $0.execute(uri) } }

    private func run(_ label: String, onFail: (() -> Void)? = nil,
                     _ op: @escaping (DeskPhoneBackend) async -> CiscoXML.Result) {
        busy = true
        let backend = self.backend
        Task { @MainActor in
            let r = await op(backend)
            self.busy = false
            if !r.isOK { onFail?() }
            self.lastResult = "\(label): \(r.message)"
            SIPManager.shared.log("Tischtelefon – \(self.lastResult)")
        }
    }

    // MARK: Statusabo (SUBSCRIBE dialog / presence)

    /// Startet das Abo auf die Nebenstelle des Tischtelefons. Läuft über das SIP-Konto des Macs.
    func startMonitoring() {
        stopMonitoring()
        let sip = SIPManager.shared
        guard !settings.deskPhoneExtension.isEmpty else {
            subscriptionState = "Keine Nebenstelle für das Tischtelefon eingestellt"
            return
        }
        // CME liefert für SIP-Telefone nur presence (frei/belegt); "dialog" nimmt er an, schickt aber nie NOTIFYs.
        let pkg = "presence"
        subId = sip.subscribe(
            uri: "sip:\(settings.deskPhoneExtension)@\(settings.sipHost)", event: pkg, expires: 3600,
            headers: ["Accept": "application/pidf+xml"],
            onNotify: { [weak self] body in
                self?.subscriptionState = "Active"
                self?.received(body)
            },
            onClosed: { [weak self] why in
                guard let self else { return }
                self.subId = nil
                self.subscriptionState = "Beendet (\(why))"
                sip.log("Statusabo: beendet (\(why)) – erneuere in 30 s")
                let item = DispatchWorkItem { MainActor.assumeIsolated { self.startMonitoring() } }
                self.retry = item
                DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: item)
            })
        subscriptionState = subId != nil ? "Abo gesendet (\(pkg))" : "Abo fehlgeschlagen"
    }

    func stopMonitoring() {
        retry?.cancel(); retry = nil
        if let id = subId { SIPManager.shared.unsubscribe(id) }
        subId = nil
        lastNotifyBody = ""
        status = .unknown
    }

    private func received(_ text: String) {
        if text == lastNotifyBody, !text.isEmpty { return }
        lastNotifyBody = text
        guard !text.isEmpty else {
            SIPManager.shared.log("Statusabo: NOTIFY ohne Inhalt")
            return
        }
        let new = DialogInfoParser.parse(text)
        if new.state == .unknown {
            let snippet = text.replacingOccurrences(of: "\n", with: " ").prefix(300)
            SIPManager.shared.log("Statusabo: NOTIFY nicht auswertbar: \(snippet)")
        }
        handle(new)
    }

    private func handle(_ new: LineStatus) {
        let old = status
        guard new.state != old.state || new.remoteNumber != old.remoteNumber else { return }
        // Gespräch nach Wahl vom Mac beendet
        if old.state == .busy, new.state == .idle { setPendingDial(nil) }
        status = new          // Call-Toast (ToastController) beobachtet den Status
        SIPManager.shared.log("Tischtelefon \(settings.deskPhoneExtension): \(new.state.label) \(new.remoteNumber ?? "")")
        CallHistory.shared.deskPhone(old: old, new: new)
    }
}
