import SwiftUI
import AppKit

/// Videobild eines Softphone-Gesprächs in einem eigenen Fenster: frei verschiebbar (am Bild ziehen), Größe an den
/// Rändern änderbar (Seitenverhältnis 16:9), schwebt über anderen Fenstern. Position und Größe merkt sich macOS
/// (Autosave-Name). Schließen schaltet das Video des Gesprächs ab.
@MainActor
final class VideoWindowController: NSObject, NSWindowDelegate {
    static let shared = VideoWindowController()

    private var panel: NSPanel?
    private var callId: String?

    /// Zeigt das Fenster für dieses Gespräch (bzw. lässt es offen)
    func show(callId: String) {
        self.callId = callId
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
                            styleMask: [.titled, .closable, .resizable, .fullSizeContentView, .nonactivatingPanel],
                            backing: .buffered, defer: false)
            p.title = "Video"
            p.titlebarAppearsTransparent = true
            p.titleVisibility = .hidden
            p.isMovableByWindowBackground = true
            p.isFloatingPanel = true
            p.level = .floating
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.hidesOnDeactivate = false
            p.backgroundColor = .black
            p.contentAspectRatio = NSSize(width: 16, height: 9)
            p.contentMinSize = NSSize(width: 256, height: 144)
            p.delegate = self
            p.center()
            // stellt Position und Größe des letzten Gesprächs wieder her (und speichert Änderungen)
            p.setFrameAutosaveName("CMEPhoneVideo")
            panel = p
        }
        // Inhalt bei jedem Einblenden neu: beim Ausblenden wird er verworfen, damit die Kamera-Vorschau stoppt
        if panel?.contentView == nil || panel?.isVisible == false {
            panel?.contentView = NSHostingView(rootView: VideoWindowView())
            panel?.orderFrontRegardless()
        }
    }

    /// Gespräch ohne Video bzw. beendet
    func hide() {
        callId = nil
        panel?.orderOut(nil)
        panel?.contentView = nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Schließen = Video aus (das Gespräch läuft weiter)
        if let callId { SIPManager.shared.setVideo(callId: callId, enabled: false) }
        hide()
        return false
    }
}

/// Gegenstelle füllt das Fenster, eigene Kamera klein unten rechts (proportional zur Fenstergröße).
private struct VideoWindowView: View {
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .bottomTrailing) {
                NativeVideoView(role: .remote)
                let w = max(96, geo.size.width / 4)
                // Eigenansicht: Bilder der Kamera aus baresip (cmevideo.m)
                NativeVideoView(role: .preview)
                    .frame(width: w, height: w * 9 / 16)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.white.opacity(0.3)))
                    .padding(10)
            }
        }
        .background(Color.black)
        .ignoresSafeArea()
    }
}
