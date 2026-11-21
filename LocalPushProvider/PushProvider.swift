import Foundation
import Network
import NetworkExtension
import os

/// Local-Push-Extension: läuft, solange das Gerät in einem der konfigurierten Heim-WLANs ist.
/// Hält eine TCP-Verbindung zum push-relay (neben Flexisip auf docker01) und meldet
/// eingehende Anrufe per `reportIncomingCall(userInfo:)` an die App – ganz ohne APNs/Internet.
final class PushProvider: NEAppPushProvider {
    private let log = Logger(subsystem: "de.wiederhol.cmephone.localpush", category: "provider")
    private let queue = DispatchQueue(label: "localpush")
    private var connection: NWConnection?
    private var buffer = Data()
    private var reconnectDelay: TimeInterval = 2
    private var running = false
    private var pingTimer: DispatchSourceTimer?

    private var host: String { providerConfiguration?[SharedConfig.ProviderKey.relayHost] as? String ?? "" }
    private var port: Int { providerConfiguration?[SharedConfig.ProviderKey.relayPort] as? Int ?? 5070 }
    private var secret: String { providerConfiguration?[SharedConfig.ProviderKey.relaySecret] as? String ?? "" }
    private var token: String { providerConfiguration?[SharedConfig.ProviderKey.deviceToken] as? String ?? "" }

    override func start() {
        log.info("start")
        queue.async { self.running = true; self.connect() }
    }

    override func stop(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        log.info("stop \(reason.rawValue)")
        queue.async {
            self.running = false
            self.pingTimer?.cancel(); self.pingTimer = nil
            self.connection?.cancel(); self.connection = nil
            completionHandler()
        }
    }

    /// Wird von iOS periodisch aufgerufen (ca. alle 60 s) – für Keepalive nutzen.
    override func handleTimerEvent() {
        queue.async { self.send(LocalPushMessage(type: "ping")) }
    }

    // MARK: - Verbindung

    private func connect() {
        guard running, !host.isEmpty, let nwPort = NWEndpoint.Port(rawValue: UInt16(clamping: port)) else { return }
        let tcp = NWProtocolTCP.Options()
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 30
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.requiredInterfaceType = .wifi   // Local Push gilt nur im WLAN

        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: params)
        connection = conn
        conn.stateUpdateHandler = { [weak self] state in self?.onState(state, conn: conn) }
        conn.start(queue: queue)
    }

    private func onState(_ state: NWConnection.State, conn: NWConnection) {
        switch state {
        case .ready:
            log.info("verbunden mit \(self.host):\(self.port)")
            reconnectDelay = 2
            buffer.removeAll()
            send(LocalPushMessage(type: "hello", token: token, secret: secret))
            receive(on: conn)
            startPing()
        case .failed(let err), .waiting(let err):
            log.error("Verbindung: \(err.localizedDescription)")
            scheduleReconnect(conn)
        case .cancelled:
            break
        default:
            break
        }
    }

    private func scheduleReconnect(_ conn: NWConnection) {
        conn.cancel()
        guard running, connection === conn else { return }
        connection = nil
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, 60)
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.connect() }
    }

    private func startPing() {
        pingTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 25, repeating: 25)
        t.setEventHandler { [weak self] in self?.send(LocalPushMessage(type: "ping")) }
        t.resume()
        pingTimer = t
    }

    private func receive(on conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            if let data, !data.isEmpty {
                self.buffer.append(data)
                self.drainLines()
            }
            if isComplete || error != nil {
                self.scheduleReconnect(conn)
                return
            }
            self.receive(on: conn)
        }
    }

    private func drainLines() {
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            guard !line.isEmpty, let msg = try? JSONDecoder().decode(LocalPushMessage.self, from: line) else { continue }
            handle(msg)
        }
        if buffer.count > 64 * 1024 { buffer.removeAll() }   // Schutz vor Müll
    }

    private func handle(_ msg: LocalPushMessage) {
        switch msg.type {
        case "call":
            log.info("Anruf \(msg.callId ?? "?", privacy: .public) von \(msg.fromURI ?? "?", privacy: .public)")
            var info: [String: Any] = [:]
            info[SharedConfig.CallInfoKey.callId] = msg.callId ?? UUID().uuidString
            info[SharedConfig.CallInfoKey.fromURI] = msg.fromURI ?? ""
            info[SharedConfig.CallInfoKey.displayName] = msg.displayName ?? ""
            info[SharedConfig.CallInfoKey.hasVideo] = msg.video ?? false
            reportIncomingCall(userInfo: info)
            send(LocalPushMessage(type: "ack", callId: msg.callId))
        case "error":
            log.error("Relay: \(msg.reason ?? "?", privacy: .public)")
        default:
            break // ok / pong
        }
    }

    private func send(_ msg: LocalPushMessage) {
        guard let connection, var data = try? JSONEncoder().encode(msg) else { return }
        data.append(0x0A)
        connection.send(content: data, completion: .contentProcessed { [weak self] err in
            if let err { self?.log.error("Senden: \(err.localizedDescription)") }
        })
    }
}
