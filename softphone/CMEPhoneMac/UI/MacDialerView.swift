import SwiftUI

struct MacDialerView: View {
    @EnvironmentObject private var settings: PhoneSettings
    @EnvironmentObject private var desk: DeskPhoneController
    @State private var number = ""
    @FocusState private var focused: Bool

    private let keys = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["*", "0", "#"]]

    var body: some View {
        VStack(spacing: 18) {
            Text(settings.macMode == .softphone ? "Anruf über diesen Mac" : "Anruf über Tischtelefon \(settings.deskPhoneExtension)")
                .font(.headline).foregroundStyle(.secondary)

            TextField("Nummer oder Nebenstelle", text: $number)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 26, weight: .light, design: .rounded))
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
                .focused($focused)
                .onSubmit { call(video: false) }

            Grid(horizontalSpacing: 12, verticalSpacing: 12) {
                ForEach(keys, id: \.self) { row in
                    GridRow {
                        ForEach(row, id: \.self) { k in
                            Button { number.append(k) } label: {
                                Text(k).font(.title2).frame(width: 56, height: 44)
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                }
            }

            HStack(spacing: 14) {
                Button { call(video: false) } label: {
                    Label("Anrufen", systemImage: "phone.fill").frame(minWidth: 110)
                }
                .buttonStyle(.borderedProminent).tint(.green)
                .keyboardShortcut(.return, modifiers: [])
                .disabled(number.isEmpty)

                if settings.macMode == .softphone && settings.videoEnabled {
                    Button { call(video: true) } label: {
                        Label("Video", systemImage: "video.fill").frame(minWidth: 90)
                    }
                    .disabled(number.isEmpty)
                }
                Button { number = "" } label: { Image(systemName: "delete.left") }
                    .disabled(number.isEmpty)
            }
            if settings.macMode == .deskPhone && !desk.lastResult.isEmpty {
                Text(desk.lastResult).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(30)
        .onAppear { focused = true }
    }

    private func call(video: Bool) {
        let n = number.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        URLRouter.dial(n, video: video)
    }
}
