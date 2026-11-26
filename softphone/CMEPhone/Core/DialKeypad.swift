import SwiftUI

/// Tastenfeld 1–9, *, 0, # für iOS und Mac. Langes Drücken der 0 (0,5 s) gibt "+" statt "0" – wie am Handy:
/// das "+" erscheint schon beim Halten, beim Loslassen kommt keine 0 mehr dazu.
/// (Ein SwiftUI-Button mit .onLongPressGesture löst nie lang aus: der Button übernimmt den Klick selbst.)
struct DialKeypad: View {
    enum Shape { case pill, circle }
    var shape: Shape
    var keySize: CGSize
    var spacing: CGFloat = 12
    var outlined = false
    let onKey: (String) -> Void

    static let keys: [(digit: String, letters: String)] = [
        ("1", ""), ("2", "ABC"), ("3", "DEF"),
        ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
        ("*", ""), ("0", "+"), ("#", ""),
    ]

    var body: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.fixed(keySize.width), spacing: spacing), count: 3),
                  spacing: spacing) {
            ForEach(Self.keys, id: \.digit) { key in
                DialKey(digit: key.digit, letters: key.letters, longValue: key.digit == "0" ? "+" : nil,
                        shape: shape, size: keySize, outlined: outlined, onKey: onKey)
            }
        }
    }
}

private struct DialKey: View {
    let digit: String
    let letters: String
    let longValue: String?
    let shape: DialKeypad.Shape
    let size: CGSize
    let outlined: Bool
    let onKey: (String) -> Void

    @Environment(\.colorScheme) private var scheme
    @State private var pressed = false
    @State private var longFired = false
    @State private var longTimer: DispatchWorkItem?

    var body: some View {
        VStack(spacing: shape == .circle ? 0 : -1) {
            Text(digit)
                .font(.system(size: digitSize, weight: shape == .circle ? .regular : .semibold))
                .offset(y: digit == "*" ? digitSize * 0.2 : 0)
            if !letters.isEmpty {
                Text(letters).font(.system(size: shape == .circle ? 10 : 7, weight: .semibold)).tracking(0.5)
            }
        }
        .foregroundStyle(scheme == .dark ? Color.white : Color.black)
        .frame(width: size.width, height: size.height)
        .background(background)
        .contentShape(Rectangle())
        .brightness(pressed ? 0.12 : 0)
        .scaleEffect(pressed ? 0.96 : 1)
        .animation(.easeOut(duration: 0.08), value: pressed)
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard !pressed else { return }
                    pressed = true
                    longFired = false
                    if let longValue {
                        let w = DispatchWorkItem {
                            longFired = true
                            onKey(longValue)
                        }
                        longTimer = w
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: w)
                    }
                }
                .onEnded { _ in
                    longTimer?.cancel()
                    longTimer = nil
                    if !longFired { onKey(digit) }
                    pressed = false
                }
        )
        .accessibilityElement()
        .accessibilityLabel(digit)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { onKey(digit) }
    }

    private var digitSize: CGFloat {
        let base: CGFloat = shape == .circle ? 32 : 24
        return digit == "*" ? base * 1.25 : base
    }

    @ViewBuilder private var background: some View {
        let dark = scheme == .dark
        let fill: Color = outlined ? (dark ? .black : .white)
            : (shape == .circle ? Color.gray.opacity(dark ? 0.35 : 0.2) : (dark ? Color(white: 0.2) : Color(white: 0.76)))
        let stroke: Color = outlined ? (dark ? Color.white.opacity(0.6) : Color.black.opacity(0.75)) : .clear
        switch shape {
        case .pill: Capsule().fill(fill).overlay(Capsule().strokeBorder(stroke, lineWidth: 1.2))
        case .circle: Circle().fill(fill).overlay(Circle().strokeBorder(stroke, lineWidth: 1.2))
        }
    }
}
