import Foundation
import Combine
import linphonesw

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

    var backend: DeskPhoneBackend
    private let settings = PhoneSettings.shared
    private var event: Event?
    private var delegate: CoreDelegate?
    private var lastNotifiedDialog: String?

    private init() {
        backend = CiscoXMLBackend()
    }

    // MARK: Befehle

    func dial(_ number: String) { run("Wählen \(number)") { await $0.dial(number) } }
    func answer() { run("Annehmen") { await $0.answer() } }
    func hangup() { run("Auflegen") { await $0.hangup() } }
    func decline() { run("Ablehnen") { await $0.decline() } }
    func hold() { run("Halten") { await $0.hold() } }
    func sendDigits(_ d: String) { run("Ziffern \(d)") { await $0.sendDigits(d) } }
    func execute(_ uri: String) { run(uri) { await $0.execute(uri) } }

    private func run(_ label: String, _ op: @escaping (DeskPhoneBackend) async -> CiscoXML.Result) {
        busy = true
        let backend = self.backend
        Task { @MainActor in
            let r = await op(backend)
            self.busy = false
            self.lastResult = "\(label): \(r.message)"
            SIPManager.shared.log("Tischtelefon – \(self.lastResult)")
        }
    }

    // MARK: Statusabo (SUBSCRIBE dialog / presence)

    /// Startet das Abo auf die Nebenstelle des Tischtelefons. Läuft über das SIP-Konto des Macs.
    func startMonitoring() {
        stopMonitoring()
        let sip = SIPManager.shared
        guard let core = sip.core, !settings.deskPhoneExtension.isEmpty else {
            subscriptionState = "Keine Nebenstelle für das Tischtelefon eingestellt"
            return
        }
        if delegate == nil {
            let d = CoreDelegateStub(
                onSubscriptionStateChanged: { [weak self] _, ev, state in
                    MainActor.assumeIsolated {
                        guard let self, ev === self.event else { return }
                        self.subscriptionState = "\(state)"
                        if state == .Error || state == .Expiring {
                            sip.log("Statusabo: \(state) – erneuere in 30 s")
                            DispatchQueue.main.asyncAfter(deadline: .now() + 30) { self.startMonitoring() }
                        }
                    }
                },
                onNotifyReceived: { [weak self] _, ev, _, body in
                    MainActor.assumeIsolated {
                        guard let self, ev === self.event else { return }
                        let text = body?.utf8Text ?? ""
                        let new = DialogInfoParser.parse(text)
                        self.handle(new)
                    }
                }
            )
            delegate = d
            sip.addCoreDelegate(d)
        }
        do {
            let pkg = settings.statusEvent.rawValue
            let resource = try Factory.Instance.createAddress(addr: "sip:\(settings.deskPhoneExtension)@\(settings.cmeHost)")
            let ev = try core.createSubscribe(resource: resource, event: pkg, expires: 3600)
            ev.addCustomHeader(name: "Accept",
                               value: pkg == "dialog" ? "application/dialog-info+xml" : "application/pidf+xml")
            try ev.sendSubscribe(body: nil)
            event = ev
            subscriptionState = "Abo gesendet (\(pkg))"
        } catch {
            subscriptionState = "Abo fehlgeschlagen: \(error)"
        }
    }

    func stopMonitoring() {
        event?.terminate()
        event = nil
        status = .unknown
    }

    private func handle(_ new: LineStatus) {
        let old = status
        status = new
        SIPManager.shared.log("Tischtelefon \(settings.deskPhoneExtension): \(new.state.label) \(new.remoteNumber ?? "")")
        // Mitteilung nur beim Übergang nach "klingelt", einmal pro Dialog
        if new.state == .ringing, old.state != .ringing, settings.deskPhoneNotify,
           new.dialogId == nil || new.dialogId != lastNotifiedDialog {
            lastNotifiedDialog = new.dialogId
            CallCenter.shared.notifyDeskPhoneRinging(number: new.remoteNumber, name: new.remoteName)
        }
        if new.state != .ringing {
            CallCenter.shared.clearDeskPhoneNotification()
        }
    }
}
