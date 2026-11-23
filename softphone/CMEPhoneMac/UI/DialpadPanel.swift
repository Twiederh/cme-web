import SwiftUI
import AppKit

/// Wähltastatur im Menüleisten-Popover: Pillen-Tasten mit Buchstaben, grüner Anrufen-Knopf.
/// Tastatureingabe und Einfügen gehen auch. Nach dem Wählen schließt sich das Popover, der Call-Toast übernimmt.
struct DialpadView: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var history: CallHistory
    @Environment(\.colorScheme) private var scheme
    @State private var number: String
    @FocusState private var focused: Bool

    init(initial: String = "") { _number = State(initialValue: initial) }

    private static let keys: [(String, String)] = [
        ("1", ""), ("2", "ABC"), ("3", "DEF"),
        ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
        ("*", ""), ("0", "+"), ("#", ""),
    ]

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

            LazyVGrid(columns: Array(repeating: GridItem(.fixed(80), spacing: 12), count: 3), spacing: 12) {
                ForEach(Self.keys, id: \.0) { key in
                    PillKey(digit: key.0, letters: key.1, outlined: settings.dialpadOutlined, dark: scheme == .dark) {
                        number.append(key.0)
                    }
                    .onLongPressGesture(minimumDuration: 0.5) { if key.0 == "0" { number.append("+") } }
                }
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

private struct PillKey: View {
    let digit: String
    let letters: String
    let outlined: Bool
    let dark: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: -1) {
                Text(digit).font(.system(size: digit == "*" ? 30 : 24, weight: .semibold))
                    .offset(y: digit == "*" ? 5 : 0)
                if !letters.isEmpty {
                    Text(letters).font(.system(size: 7, weight: .semibold)).tracking(0.5)
                }
            }
            .foregroundStyle(fg)
            .frame(width: 80, height: 50)
            .background(Capsule().fill(fill))
            .overlay(Capsule().strokeBorder(outlined ? (dark ? Color.white.opacity(0.6) : Color.black.opacity(0.75)) : .clear,
                                            lineWidth: 1.2))
            .contentShape(Capsule())
        }
        .buttonStyle(PressStyle())
    }

    private var fill: Color {
        if outlined { return dark ? .black : .white }
        return dark ? Color(white: 0.2) : Color(white: 0.76)
    }
    private var fg: Color { dark ? .white : .black }
}

private struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .brightness(configuration.isPressed ? 0.12 : 0)
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
    }
}

extension Notification.Name {
    /// Nummer in die Wähltastatur übernehmen (cmephone://dial/…, Anrufliste)
    static let cmePrefillNumber = Notification.Name("cmePrefillNumber")
}
