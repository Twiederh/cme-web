import SwiftUI
import AppKit

/// Wähltastatur im Menüleisten-Popover: Pillen-Tasten mit Buchstaben, grüner Anrufen-Knopf.
/// Tastatureingabe und Einfügen gehen auch. Nach dem Wählen schließt sich das Popover, der Call-Toast übernimmt.
struct DialpadView: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var history: CallHistory
    @State private var number: String
    @FocusState private var focused: Bool

    init(initial: String = "") { _number = State(initialValue: initial) }

    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 4) {
                TextField("Nummer", text: $number)
                    .textFieldStyle(.plain)
                    .font(.system(size: 26, weight: .regular, design: .rounded))
                    .multilineTextAlignment(.center)
                    .focused($focused)
                    .onSubmit(call)
                    .onChange(of: number) { _, v in
                        let f = v.filter { "0123456789*#+".contains($0) }
                        if f != v { number = f }
                    }
                if !number.isEmpty {
                    Button { number.removeLast() } label: { Image(systemName: "delete.left").font(.title3) }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .frame(height: 40)
            .padding(.horizontal, 8)

            Text(modeLabel).font(.caption).foregroundStyle(.secondary)

            // Langes Drücken der 0 gibt "+" (DialKeypad, gemeinsam mit iOS)
            DialKeypad(shape: .pill, keySize: CGSize(width: 80, height: 50), outlined: settings.dialpadOutlined) {
                number.append($0)
            }

            Button(action: call) {
                Label("Anrufen", systemImage: "phone.fill")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.white)
                    .frame(width: 128, height: 46)
                    .background(Capsule().fill(Color(red: 0.30, green: 0.69, blue: 0.31)))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.defaultAction)
            .disabled(number.isEmpty && history.lastDialed == nil)
            .help(number.isEmpty ? "Wahlwiederholung" : "Anrufen")
            .padding(.top, 4)
            .overlay(alignment: .trailing) {
                // Videoanruf (nur Softphone, mit Nummer)
                if settings.macMode == .softphone && settings.videoEnabled && !number.isEmpty {
                    Button {
                        URLRouter.dial(number, video: true)
                        number = ""
                        StatusBarController.shared.close()
                    } label: {
                        Image(systemName: "video.fill").foregroundStyle(.white)
                            .frame(width: 40, height: 40)
                            .background(Circle().fill(Color(red: 0.30, green: 0.69, blue: 0.31)))
                    }
                    .buttonStyle(.plain)
                    .offset(x: 56, y: 2)
                    .help("Videoanruf")
                }
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 6)
        .padding(.bottom, 10)
        .frame(width: 300)
        .onAppear { focused = true }
        .onReceive(NotificationCenter.default.publisher(for: .cmePrefillNumber)) { n in
            if let s = n.object as? String { number = s }
        }
    }

    private var modeLabel: String {
        settings.macMode == .deskPhone
            ? "Wählt am Tischtelefon \(settings.deskPhoneExtension)"
            : "Wählt über den Mac (\(settings.extensionNumber))"
    }

    private func call() {
        // Leer + Anrufen = Wahlwiederholung (wie am Telefon: Nummer erst anzeigen)
        if number.isEmpty, let last = history.lastDialed { number = last.number; return }
        guard !number.isEmpty else { return }
        URLRouter.dial(number)
        number = ""
        StatusBarController.shared.close()
    }
}

extension Notification.Name {
    /// Nummer in die Wähltastatur übernehmen (cmephone://dial/…, Anrufliste)
    static let cmePrefillNumber = Notification.Name("cmePrefillNumber")
}
