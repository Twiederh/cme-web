import AVFoundation
import CoreMedia
import CMESIP
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// Videobilder aus baresip (cmevideo.m) auf zwei AVSampleBufferDisplayLayer: Gegenstelle und eigene Kamera.
/// Die C-Schicht ruft aus beliebigen Threads; angezeigt wird auf dem Main-Thread.
final class VideoRenderer: @unchecked Sendable {
    static let shared = VideoRenderer()

    let remoteLayer = AVSampleBufferDisplayLayer()
    let previewLayer = AVSampleBufferDisplayLayer()

    private init() {
        remoteLayer.videoGravity = .resizeAspect
        previewLayer.videoGravity = .resizeAspectFill
        // Eigenansicht gespiegelt wie ein Spiegel
        previewLayer.setAffineTransform(CGAffineTransform(scaleX: -1, y: 1))
        remoteLayer.backgroundColor = CGColor(gray: 0, alpha: 1)
        previewLayer.backgroundColor = CGColor(gray: 0, alpha: 1)
    }

    /// Bei baresip anmelden (einmal beim Start)
    func install() {
        cmesip_set_video_handler({ remote, pb, _ in
            guard let pb else { return }
            let buffer = Unmanaged<CVPixelBuffer>.fromOpaque(pb).takeUnretainedValue()
            VideoRenderer.shared.show(buffer, remote: remote)
        }, nil)
    }

    private func show(_ pb: CVPixelBuffer, remote: Bool) {
        var fd: CMVideoFormatDescription?
        guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: pb,
                                                           formatDescriptionOut: &fd) == noErr, let fd else { return }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: .zero, decodeTimeStamp: .invalid)
        var sb: CMSampleBuffer?
        guard CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: pb, formatDescription: fd,
                                                       sampleTiming: &timing, sampleBufferOut: &sb) == noErr,
              let sb else { return }
        if let att = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: true), CFArrayGetCount(att) > 0 {
            let d = unsafeBitCast(CFArrayGetValueAtIndex(att, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(d, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        let layer = remote ? remoteLayer : previewLayer
        DispatchQueue.main.async {
            let r = layer.sampleBufferRenderer
            if r.status == .failed { r.flush() }
            r.enqueue(sb)
        }
    }

    /// Letztes Bild entfernen (Gespräch ohne Video bzw. beendet)
    @MainActor func clear() {
        remoteLayer.sampleBufferRenderer.flush(removingDisplayedImage: true, completionHandler: nil)
        previewLayer.sampleBufferRenderer.flush(removingDisplayedImage: true, completionHandler: nil)
    }
}

/// Hält einen der Video-Layer und zieht ihn auf die eigene Größe
final class VideoLayerHostView: PlatformView {
    private let videoLayer: CALayer

    init(layer: CALayer) {
        videoLayer = layer
        super.init(frame: .zero)
        #if os(iOS)
        backgroundColor = .black
        self.layer.addSublayer(layer)
        #else
        wantsLayer = true
        self.layer?.backgroundColor = NSColor.black.cgColor
        self.layer?.addSublayer(layer)
        autoresizingMask = [.width, .height]
        #endif
    }
    required init?(coder: NSCoder) { nil }

    private func fit() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        if videoLayer.superlayer !== hostLayer { hostLayer?.addSublayer(videoLayer) }
        videoLayer.frame = bounds
        CATransaction.commit()
    }

    private var hostLayer: CALayer? {
        #if os(iOS)
        return layer
        #else
        return layer
        #endif
    }

    #if os(iOS)
    override func layoutSubviews() { super.layoutSubviews(); fit() }
    #else
    override func layout() { super.layout(); fit() }
    #endif

    /// Layer wieder hierher holen (er kann immer nur in einer Ansicht hängen)
    func reattach() { fit() }
}

#if os(iOS)
import SwiftUI
/// SwiftUI: Bild der Gegenstelle bzw. eigene Kamera
struct VideoLayerView: UIViewRepresentable {
    let remote: Bool
    func makeUIView(context: Context) -> VideoLayerHostView {
        VideoLayerHostView(layer: remote ? VideoRenderer.shared.remoteLayer : VideoRenderer.shared.previewLayer)
    }
    func updateUIView(_ v: VideoLayerHostView, context: Context) { v.reattach() }
}
#endif
