import SwiftUI

struct DialerView: View {
    @EnvironmentObject private var sip: SIPManager
    @EnvironmentObject private var settings: PhoneSettings
    @State private var number = ""

    private let keys: [[(String, String)]] = [
        [("1", ""), ("2", "ABC"), ("3", "DEF")],
        [("4", "GHI"), ("5", "JKL"), ("6", "MNO")],
        [("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ")],
        [("*", ""), ("0", "+"), ("#", "")],
    ]

    var body: some View {
        VStack(spacing: 18) {
            HStack {
                RegistrationBadge(status: sip.registration)
                Spacer()
                if !settings.extensionNumber.isEmpty {
                    Text("Nst. \(settings.extensionNumber)").font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal)

            Spacer(minLength: 0)

            Text(number.isEmpty ? " " : number)
                .font(.system(size: 38, weight: .light, design: .rounded))
                .lineLimit(1).minimumScaleFactor(0.4)
                .frame(maxWidth: .infinity)
                .padding(.horizontal)
                .contextMenu {
                    Button("Einfügen") { if let s = UIPasteboard.general.string { number = sanitize(s) } }
                }

            VStack(spacing: 14) {
                ForEach(keys.indices, id: \.self) { row in
                    HStack(spacing: 22) {
                        ForEach(keys[row], id: \.0) { key in
                            DialKey(digit: key.0, letters: key.1) { number.append(key.0) }
                                .simultaneousGesture(LongPressGesture().onEnded { _ in
                                    if key.0 == "0" { number.append("+") }
                                })
                        }
                    }
                }
            }

            HStack(spacing: 22) {
                Button { call(video: true) } label: {
                    Image(systemName: "video.fill").font(.title2)
                        .frame(width: 72, height: 72)
                        .background(Circle().fill(Color.green.opacity(0.18)))
                }
                .disabled(number.isEmpty || !settings.videoEnabled)

                Button { call(video: false) } label: {
                    Image(systemName: "phone.fill").font(.title)
                        .foregroundStyle(.white)
                        .frame(width: 78, height: 78)
                        .background(Circle().fill(Color.green))
                }
                .disabled(number.isEmpty)

                Button { if !number.isEmpty { number.removeLast() } } label: {
                    Image(systemName: "delete.left").font(.title2)
                        .frame(width: 72, height: 72)
                }
                .opacity(number.isEmpty ? 0 : 1)
                .onLongPressGesture { number = "" }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical)
        .frame(maxWidth: 480)
    }

    private func call(video: Bool) {
        guard !number.isEmpty else { return }
        CallKitProvider.shared.startOutgoing(number: number, video: video)
    }

    private func sanitize(_ s: String) -> String {
        s.filter { "0123456789*#+".contains($0) }
    }
}

private struct DialKey: View {
    let digit: String
    let letters: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 0) {
                Text(digit).font(.system(size: 32, weight: .regular, design: .rounded))
                Text(letters.isEmpty ? " " : letters).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            }
            .frame(width: 78, height: 78)
            .background(Circle().fill(Color(.secondarySystemBackground)))
        }
        .buttonStyle(.plain)
    }
}
