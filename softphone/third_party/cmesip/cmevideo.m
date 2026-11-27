/*
 * Video für CME Phone: Kamera (AVFoundation), H.264 und H.265 über VideoToolbox (Hardware), Anzeige über
 * einen Handler, der CVPixelBuffer an die App gibt (AVSampleBufferDisplayLayer).
 *
 *   vidsrc  "cmecam"   Kamera; Gerät = uniqueID von AVCaptureDevice (leer = Frontkamera bzw. Standard)
 *   vidisp  "cmedisp"  Bilder der Gegenstelle -> cmesip_video_h(remote=true, ...)
 *   vidcodec H264 (packetization-mode=1, Constrained Baseline 42e01f) und H265 (Main)
 *
 * Die Kamera meldet jedes Bild auch als Vorschau (remote=false). RTP-Verpackung/-Entpackung wie das
 * avcodec-Modul von baresip (FU-A/STAP-A bzw. FU/AP), das Kodieren macht VideoToolbox.
 */
#include <TargetConditionals.h>
#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <VideoToolbox/VideoToolbox.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#include <re.h>
#include <re_h265.h>
#include <rem.h>
#include <baresip.h>
#include "cmesip.h"

enum { DECODE_MAXSZ = 1048576 };

static cmesip_video_h *video_h;
static void *video_arg;

void cmesip_set_video_handler(cmesip_video_h *h, void *arg)
{
	video_arg = arg;
	video_h = h;
}

static void emit(bool remote, CVPixelBufferRef pb)
{
	cmesip_video_h *h = video_h;
	if (h && pb)
		h(remote, (void *)pb, video_arg);
}


/* ---------------------------------------------------------------- Geräte */

static NSArray<AVCaptureDevice *> *cameras(void)
{
	NSMutableArray *types = [NSMutableArray arrayWithObject:AVCaptureDeviceTypeBuiltInWideAngleCamera];
#if TARGET_OS_OSX
	if (@available(macOS 14.0, *)) {
		[types addObject:AVCaptureDeviceTypeExternal];
		[types addObject:AVCaptureDeviceTypeContinuityCamera];
	}
#endif
	AVCaptureDeviceDiscoverySession *ds = [AVCaptureDeviceDiscoverySession
		discoverySessionWithDeviceTypes:types mediaType:AVMediaTypeVideo
		position:AVCaptureDevicePositionUnspecified];
	return ds.devices;
}


int cmesip_video_devices(char *buf, size_t size)
{
	if (!buf || !size)
		return EINVAL;
	buf[0] = '\0';
	@autoreleasepool {
		NSMutableString *s = [NSMutableString string];
		for (AVCaptureDevice *d in cameras())
			[s appendFormat:@"%@\t%@\n", d.uniqueID, d.localizedName];
		str_ncpy(buf, s.UTF8String, size);
	}
	return 0;
}


/* ---------------------------------------------------------------- Kamera */

struct vidsrc_st {
	vidsrc_frame_h *frameh;
	void *arg;
	void *session;      /* AVCaptureSession (CFBridgingRetain) */
	void *delegate;     /* CmeCamDelegate */
	void *queue;        /* dispatch_queue_t */
	void *timer;        /* Testbild: dispatch_source_t */
	CVPixelBufferRef test_pb;
	unsigned n;
	volatile bool stop;
};

@interface CmeCamDelegate : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property (nonatomic, assign) struct vidsrc_st *st;
@end

@implementation CmeCamDelegate
- (void)captureOutput:(AVCaptureOutput *)output didOutputSampleBuffer:(CMSampleBufferRef)sb
	fromConnection:(AVCaptureConnection *)connection
{
	struct vidsrc_st *st = self.st;
	(void)output; (void)connection;
	if (!st || st->stop)
		return;
	CVImageBufferRef pb = CMSampleBufferGetImageBuffer(sb);
	if (!pb || CVPixelBufferGetPlaneCount(pb) < 2)
		return;
	emit(false, pb);

	CMTime t = CMSampleBufferGetPresentationTimeStamp(sb);
	uint64_t ts = CMTIME_IS_VALID(t) ? (uint64_t)(CMTimeGetSeconds(t) * VIDEO_TIMEBASE)
		: tmr_jiffies_usec();

	CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
	struct vidframe f;
	memset(&f, 0, sizeof(f));
	f.fmt = VID_FMT_NV12;
	f.size.w = (unsigned)CVPixelBufferGetWidth(pb);
	f.size.h = (unsigned)CVPixelBufferGetHeight(pb);
	for (int i = 0; i < 2; i++) {
		f.data[i] = CVPixelBufferGetBaseAddressOfPlane(pb, i);
		f.linesize[i] = (uint16_t)CVPixelBufferGetBytesPerRowOfPlane(pb, i);
	}
	st->frameh(&f, ts, st->arg);
	CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
}
@end


static void cam_destructor(void *arg)
{
	struct vidsrc_st *st = arg;
	st->stop = true;
	if (st->timer) {
		dispatch_source_t t = CFBridgingRelease(st->timer);
		dispatch_source_cancel(t);
	}
	if (st->session) {
		AVCaptureSession *s = CFBridgingRelease(st->session);
		[s stopRunning];
	}
	if (st->queue) {
		dispatch_queue_t q = CFBridgingRelease(st->queue);
		dispatch_sync(q, ^{});   /* letzter Rückruf ist durch */
	}
	if (st->delegate) {
		CmeCamDelegate *d = CFBridgingRelease(st->delegate);
		d.st = NULL;
	}
	if (st->test_pb)
		CVPixelBufferRelease(st->test_pb);
}


/* Testbild (Gerät "test", für den Anruftest ohne Kamera): wandernde Balken, 640x480, 15 Bilder/s */
static void test_tick(struct vidsrc_st *st)
{
	if (st->stop)
		return;
	CVPixelBufferRef pb = st->test_pb;
	CVPixelBufferLockBaseAddress(pb, 0);
	uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(pb, 0), *uv = CVPixelBufferGetBaseAddressOfPlane(pb, 1);
	size_t ys = CVPixelBufferGetBytesPerRowOfPlane(pb, 0), uvs = CVPixelBufferGetBytesPerRowOfPlane(pb, 1);
	for (unsigned r = 0; r < 480; r++)
		for (unsigned c = 0; c < 640; c++)
			y[r * ys + c] = (uint8_t)(((c + st->n * 8) / 40 % 2) ? 200 : 40);
	for (unsigned r = 0; r < 240; r++)
		memset(uv + r * uvs, 128, 640);
	struct vidframe f;
	memset(&f, 0, sizeof(f));
	f.fmt = VID_FMT_NV12;
	f.size.w = 640;
	f.size.h = 480;
	f.data[0] = y; f.linesize[0] = (uint16_t)ys;
	f.data[1] = uv; f.linesize[1] = (uint16_t)uvs;
	CVPixelBufferUnlockBaseAddress(pb, 0);
	emit(false, pb);
	CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
	st->frameh(&f, (uint64_t)st->n * VIDEO_TIMEBASE / 15, st->arg);
	CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
	st->n++;
}


static int cam_alloc(struct vidsrc_st **stp, const struct vidsrc *vs, struct vidsrc_prm *prm,
		     const struct vidsz *size, const char *fmt, const char *dev,
		     vidsrc_frame_h *frameh, vidsrc_packet_h *packeth, vidsrc_error_h *errorh, void *arg)
{
	struct vidsrc_st *st;
	(void)vs; (void)fmt; (void)packeth; (void)errorh;

	if (!stp || !frameh)
		return EINVAL;
	st = mem_zalloc(sizeof(*st), cam_destructor);
	if (!st)
		return ENOMEM;
	st->frameh = frameh;
	st->arg = arg;

	if (0 == str_casecmp(dev, "test")) {
		CVPixelBufferCreate(NULL, 640, 480, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, NULL, &st->test_pb);
		dispatch_queue_t q = dispatch_queue_create("cmevideo.test", DISPATCH_QUEUE_SERIAL);
		dispatch_source_t t = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, q);
		dispatch_source_set_timer(t, dispatch_time(DISPATCH_TIME_NOW, 0), NSEC_PER_SEC / 15, NSEC_PER_MSEC);
		dispatch_source_set_event_handler(t, ^{ test_tick(st); });
		st->queue = (void *)CFBridgingRetain(q);
		st->timer = (void *)CFBridgingRetain(t);
		dispatch_resume(t);
		info("cmevideo: Testbild\n");
		*stp = st;
		return 0;
	}
	@autoreleasepool {
		AVCaptureDevice *cam = nil;
		if (str_isset(dev) && 0 != str_casecmp(dev, "default"))
			cam = [AVCaptureDevice deviceWithUniqueID:[NSString stringWithUTF8String:dev]];
#if TARGET_OS_IPHONE
		if (!cam)
			cam = [AVCaptureDevice defaultDeviceWithDeviceType:AVCaptureDeviceTypeBuiltInWideAngleCamera
				mediaType:AVMediaTypeVideo position:AVCaptureDevicePositionFront];
#endif
		if (!cam)
			cam = [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo];
		if (!cam) {
			warning("cmevideo: keine Kamera\n");
			mem_deref(st);
			return ENODEV;
		}
		NSError *e = nil;
		AVCaptureDeviceInput *in = [AVCaptureDeviceInput deviceInputWithDevice:cam error:&e];
		if (!in) {
			warning("cmevideo: Kamera %s: %s\n", cam.localizedName.UTF8String,
				e.localizedDescription.UTF8String);
			mem_deref(st);
			return EACCES;
		}
		AVCaptureSession *s = [[AVCaptureSession alloc] init];
		NSString *preset = (size && size->w >= 1280) ? AVCaptureSessionPreset1280x720
			: AVCaptureSessionPreset640x480;
		if ([s canSetSessionPreset:preset])
			s.sessionPreset = preset;
		if ([s canAddInput:in])
			[s addInput:in];
		AVCaptureVideoDataOutput *out = [[AVCaptureVideoDataOutput alloc] init];
		out.videoSettings = @{ (id)kCVPixelBufferPixelFormatTypeKey:
				       @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) };
		out.alwaysDiscardsLateVideoFrames = YES;
		CmeCamDelegate *d = [[CmeCamDelegate alloc] init];
		d.st = st;
		dispatch_queue_t q = dispatch_queue_create("cmevideo.camera", DISPATCH_QUEUE_SERIAL);
		[out setSampleBufferDelegate:d queue:q];
		if ([s canAddOutput:out])
			[s addOutput:out];
		AVCaptureConnection *c = [out connectionWithMediaType:AVMediaTypeVideo];
#if TARGET_OS_IPHONE
		/* Hochformat wie das Telefon gehalten wird */
		if (@available(iOS 17.0, *)) {
			if ([c isVideoRotationAngleSupported:90])
				c.videoRotationAngle = 90;
		}
#endif
		(void)c;
		if (prm && prm->fps > 0 && [cam lockForConfiguration:nil]) {
			CMTime dur = CMTimeMake(1, (int32_t)prm->fps);
			for (AVFrameRateRange *r in cam.activeFormat.videoSupportedFrameRateRanges) {
				if (r.maxFrameRate >= prm->fps) {
					cam.activeVideoMinFrameDuration = dur;
					break;
				}
			}
			[cam unlockForConfiguration];
		}
		st->session = (void *)CFBridgingRetain(s);
		st->delegate = (void *)CFBridgingRetain(d);
		st->queue = (void *)CFBridgingRetain(q);
		[s startRunning];
		info("cmevideo: Kamera %s (%s)\n", cam.localizedName.UTF8String, preset.UTF8String);
	}
	*stp = st;
	return 0;
}


/* ---------------------------------------------------------------- Anzeige */

struct vidisp_st {
	CVPixelBufferPoolRef pool;
	struct vidsz size;
};

static CVPixelBufferRef last_decoded;   /* zuletzt dekodiert (gesperrt), Anzeige ohne Kopie */


static void disp_destructor(void *arg)
{
	struct vidisp_st *st = arg;
	if (st->pool)
		CVPixelBufferPoolRelease(st->pool);
}


static int disp_alloc(struct vidisp_st **vp, const struct vidisp *vd, struct vidisp_prm *prm,
		      const char *dev, vidisp_resize_h *resizeh, void *arg)
{
	(void)vd; (void)prm; (void)dev; (void)resizeh; (void)arg;
	struct vidisp_st *st = mem_zalloc(sizeof(*st), disp_destructor);
	if (!st)
		return ENOMEM;
	*vp = st;
	return 0;
}


/* NV12 oder YUV420P in einen NV12-Puffer kopieren */
static CVPixelBufferRef copy_frame(struct vidisp_st *st, const struct vidframe *f)
{
	if (!vidsz_cmp(&st->size, &f->size) || !st->pool) {
		if (st->pool)
			CVPixelBufferPoolRelease(st->pool);
		st->pool = NULL;
		NSDictionary *attrs = @{
			(id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
			(id)kCVPixelBufferWidthKey: @(f->size.w), (id)kCVPixelBufferHeightKey: @(f->size.h),
			(id)kCVPixelBufferIOSurfacePropertiesKey: @{} };
		CVPixelBufferPoolCreate(NULL, NULL, (__bridge CFDictionaryRef)attrs, &st->pool);
		st->size = f->size;
	}
	CVPixelBufferRef pb = NULL;
	if (!st->pool || CVPixelBufferPoolCreatePixelBuffer(NULL, st->pool, &pb) != kCVReturnSuccess)
		return NULL;
	CVPixelBufferLockBaseAddress(pb, 0);
	uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(pb, 0), *uv = CVPixelBufferGetBaseAddressOfPlane(pb, 1);
	size_t ys = CVPixelBufferGetBytesPerRowOfPlane(pb, 0), uvs = CVPixelBufferGetBytesPerRowOfPlane(pb, 1);
	unsigned w = f->size.w, h = f->size.h;
	for (unsigned r = 0; r < h; r++)
		memcpy(y + r * ys, f->data[0] + r * f->linesize[0], w);
	for (unsigned r = 0; r < h / 2; r++) {
		uint8_t *d = uv + r * uvs;
		if (f->fmt == VID_FMT_NV12) {
			memcpy(d, f->data[1] + r * f->linesize[1], w);
		} else {
			const uint8_t *u = f->data[1] + r * f->linesize[1], *v = f->data[2] + r * f->linesize[2];
			for (unsigned c = 0; c < w / 2; c++) {
				d[2 * c] = u[c];
				d[2 * c + 1] = v[c];
			}
		}
	}
	CVPixelBufferUnlockBaseAddress(pb, 0);
	return pb;
}


static int disp_disp(struct vidisp_st *st, const char *title, const struct vidframe *f, uint64_t ts)
{
	(void)title; (void)ts;
	if (!f || !f->data[0])
		return 0;
	if (last_decoded && f->data[0] == CVPixelBufferGetBaseAddressOfPlane(last_decoded, 0)) {
		emit(true, last_decoded);
		return 0;
	}
	if (f->fmt != VID_FMT_NV12 && f->fmt != VID_FMT_YUV420P)
		return ENOTSUP;
	CVPixelBufferRef pb = copy_frame(st, f);
	emit(true, pb);
	if (pb)
		CVPixelBufferRelease(pb);
	return 0;
}


/* ---------------------------------------------------------------- Kodierer */

struct videnc_state {
	bool h265;
	struct videnc_param prm;
	videnc_packet_h *pkth;
	const struct video *vid;
	VTCompressionSessionRef sess;
	struct vidsz size;
	struct mbuf *out;      /* Annex-B des zuletzt kodierten Bildes */
	bool key;
};


static void enc_reset(struct videnc_state *st)
{
	if (st->sess) {
		VTCompressionSessionInvalidate(st->sess);
		CFRelease(st->sess);
		st->sess = NULL;
	}
}


static void enc_destructor(void *arg)
{
	struct videnc_state *st = arg;
	enc_reset(st);
	mem_deref(st->out);
}


static void write_nal(struct mbuf *mb, const uint8_t *p, size_t n)
{
	static const uint8_t sc[4] = {0, 0, 0, 1};
	mbuf_write_mem(mb, sc, 4);
	mbuf_write_mem(mb, p, n);
}


static void enc_out(void *refcon, void *srcref, OSStatus status, VTEncodeInfoFlags flags, CMSampleBufferRef sb)
{
	struct videnc_state *st = refcon;
	(void)srcref; (void)flags;
	if (status != noErr || !sb || !CMSampleBufferDataIsReady(sb))
		return;

	bool key = true;
	CFArrayRef att = CMSampleBufferGetSampleAttachmentsArray(sb, false);
	if (att && CFArrayGetCount(att) > 0) {
		CFDictionaryRef d = CFArrayGetValueAtIndex(att, 0);
		key = !CFDictionaryContainsKey(d, kCMSampleAttachmentKey_NotSync);
	}
	st->key = key;
	if (key) {
		CMFormatDescriptionRef fd = CMSampleBufferGetFormatDescription(sb);
		size_t count = 0;
		for (size_t i = 0; ; i++) {
			const uint8_t *p = NULL;
			size_t n = 0;
			OSStatus s = st->h265
				? CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fd, i, &p, &n, &count, NULL)
				: CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fd, i, &p, &n, &count, NULL);
			if (s != noErr)
				break;
			write_nal(st->out, p, n);
			if (i + 1 >= count)
				break;
		}
	}

	CMBlockBufferRef bb = CMSampleBufferGetDataBuffer(sb);
	size_t len = CMBlockBufferGetDataLength(bb);
	uint8_t *tmp = mem_alloc(len, NULL);
	if (!tmp)
		return;
	if (CMBlockBufferCopyDataBytes(bb, 0, len, tmp) == kCMBlockBufferNoErr) {
		size_t pos = 0;
		while (pos + 4 <= len) {   /* AVCC: 4 Byte Länge + NAL */
			uint32_t n = ((uint32_t)tmp[pos] << 24) | ((uint32_t)tmp[pos + 1] << 16)
				| ((uint32_t)tmp[pos + 2] << 8) | tmp[pos + 3];
			pos += 4;
			if (n > len - pos)
				break;
			write_nal(st->out, tmp + pos, n);
			pos += n;
		}
	}
	mem_deref(tmp);
}


static int enc_open(struct videnc_state *st, unsigned w, unsigned h)
{
	enc_reset(st);
	OSStatus s = VTCompressionSessionCreate(NULL, (int32_t)w, (int32_t)h,
		st->h265 ? kCMVideoCodecType_HEVC : kCMVideoCodecType_H264, NULL, NULL, NULL,
		enc_out, st, &st->sess);
	if (s != noErr) {
		warning("cmevideo: Kodierer %s %ux%u: %d\n", st->h265 ? "H265" : "H264", w, h, (int)s);
		st->sess = NULL;
		return ENODEV;
	}
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue);
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse);
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_ProfileLevel, st->h265
		? kVTProfileLevel_HEVC_Main_AutoLevel : kVTProfileLevel_H264_ConstrainedBaseline_AutoLevel);
	int br = (int)(st->prm.bitrate ? st->prm.bitrate : 1500000);
	CFNumberRef n = CFNumberCreate(NULL, kCFNumberIntType, &br);
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_AverageBitRate, n);
	CFRelease(n);
	double fps = st->prm.fps > 0 ? st->prm.fps : 30;
	n = CFNumberCreate(NULL, kCFNumberDoubleType, &fps);
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_ExpectedFrameRate, n);
	CFRelease(n);
	double kint = 5.0;   /* alle 5 s ein Schlüsselbild */
	n = CFNumberCreate(NULL, kCFNumberDoubleType, &kint);
	VTSessionSetProperty(st->sess, kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, n);
	CFRelease(n);
	VTCompressionSessionPrepareToEncodeFrames(st->sess);
	st->size.w = w;
	st->size.h = h;
	info("cmevideo: Kodierer %s %ux%u, %d kbit/s\n", st->h265 ? "H265" : "H264", w, h, br / 1000);
	return 0;
}


static int enc_update(struct videnc_state **vesp, const struct vidcodec *vc, struct videnc_param *prm,
		      const char *fmtp, videnc_packet_h *pkth, const struct video *vid)
{
	struct videnc_state *st;
	(void)fmtp;
	if (!vesp || !vc || !prm || !pkth)
		return EINVAL;
	if (*vesp) {
		(*vesp)->prm = *prm;
		return 0;
	}
	st = mem_zalloc(sizeof(*st), enc_destructor);
	if (!st)
		return ENOMEM;
	st->h265 = 0 == str_casecmp(vc->name, "H265");
	st->prm = *prm;
	st->pkth = pkth;
	st->vid = vid;
	st->out = mbuf_alloc(65536);
	if (!st->out) {
		mem_deref(st);
		return ENOMEM;
	}
	*vesp = st;
	return 0;
}


static int enc_encode(struct videnc_state *st, bool update, const struct vidframe *f, uint64_t ts)
{
	if (!st || !f || (f->fmt != VID_FMT_NV12 && f->fmt != VID_FMT_YUV420P))
		return EINVAL;
	bool first = false;
	if (!st->sess || !vidsz_cmp(&st->size, &f->size)) {
		int err = enc_open(st, f->size.w, f->size.h);
		if (err)
			return err;
		first = true;
	}
	CVPixelBufferPoolRef pool = VTCompressionSessionGetPixelBufferPool(st->sess);
	CVPixelBufferRef pb = NULL;
	if (!pool || CVPixelBufferPoolCreatePixelBuffer(NULL, pool, &pb) != kCVReturnSuccess)
		return ENOMEM;

	CVPixelBufferLockBaseAddress(pb, 0);
	uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(pb, 0), *uv = CVPixelBufferGetBaseAddressOfPlane(pb, 1);
	size_t ys = CVPixelBufferGetBytesPerRowOfPlane(pb, 0), uvs = CVPixelBufferGetBytesPerRowOfPlane(pb, 1);
	unsigned w = f->size.w, h = f->size.h;
	for (unsigned r = 0; r < h; r++)
		memcpy(y + r * ys, f->data[0] + r * f->linesize[0], w);
	for (unsigned r = 0; r < h / 2; r++) {
		uint8_t *d = uv + r * uvs;
		if (f->fmt == VID_FMT_NV12) {
			memcpy(d, f->data[1] + r * f->linesize[1], w);
		} else {
			const uint8_t *u = f->data[1] + r * f->linesize[1], *v = f->data[2] + r * f->linesize[2];
			for (unsigned c = 0; c < w / 2; c++) {
				d[2 * c] = u[c];
				d[2 * c + 1] = v[c];
			}
		}
	}
	CVPixelBufferUnlockBaseAddress(pb, 0);

	CFDictionaryRef props = NULL;
	if (update || first) {
		const void *k[] = { kVTEncodeFrameOptionKey_ForceKeyFrame };
		const void *v[] = { kCFBooleanTrue };
		props = CFDictionaryCreate(NULL, k, v, 1, &kCFTypeDictionaryKeyCallBacks,
					   &kCFTypeDictionaryValueCallBacks);
	}
	CMTime pts = CMTimeMake((int64_t)ts, VIDEO_TIMEBASE);
	mbuf_rewind(st->out);
	OSStatus s = VTCompressionSessionEncodeFrame(st->sess, pb, pts, kCMTimeInvalid, props, NULL, NULL);
	if (props)
		CFRelease(props);
	CVPixelBufferRelease(pb);
	if (s != noErr) {
		warning("cmevideo: Kodieren: %d\n", (int)s);
		if (s == kVTInvalidSessionErr)
			enc_reset(st);
		return EPROTO;
	}
	VTCompressionSessionCompleteFrames(st->sess, pts);
	if (!st->out->end)
		return 0;

	uint64_t rtp_ts = video_calc_rtp_timestamp_fix(ts);
	return st->h265
		? h265_packetize(rtp_ts, st->out->buf, st->out->end, st->prm.pktsize,
				 (h265_packet_h *)st->pkth, (void *)st->vid)
		: h264_packetize(rtp_ts, st->out->buf, st->out->end, st->prm.pktsize,
				 (h264_packet_h *)st->pkth, (void *)st->vid);
}


/* ---------------------------------------------------------------- Dekodierer */

struct viddec_state {
	bool h265;
	struct mbuf *mb;       /* Annex-B des aktuellen Bildes */
	size_t frag_start;
	bool frag;
	uint16_t frag_seq;
	struct mbuf *ps[3];    /* H264: SPS, PPS; H265: VPS, SPS, PPS */
	bool ps_changed;
	CMVideoFormatDescriptionRef fd;
	VTDecompressionSessionRef sess;
	CVPixelBufferRef next; /* aus dem Rückruf */
	CVPixelBufferRef cur;  /* gesperrt, solange das Bild angezeigt wird */
	unsigned n_lost;
};


static void dec_release_cur(struct viddec_state *st)
{
	if (st->cur) {
		if (last_decoded == st->cur)
			last_decoded = NULL;
		CVPixelBufferUnlockBaseAddress(st->cur, kCVPixelBufferLock_ReadOnly);
		CVPixelBufferRelease(st->cur);
		st->cur = NULL;
	}
}


static void dec_reset(struct viddec_state *st)
{
	if (st->sess) {
		VTDecompressionSessionInvalidate(st->sess);
		CFRelease(st->sess);
		st->sess = NULL;
	}
	if (st->fd) {
		CFRelease(st->fd);
		st->fd = NULL;
	}
}


static void dec_destructor(void *arg)
{
	struct viddec_state *st = arg;
	dec_reset(st);
	dec_release_cur(st);
	if (st->next)
		CVPixelBufferRelease(st->next);
	mem_deref(st->mb);
	for (int i = 0; i < 3; i++)
		mem_deref(st->ps[i]);
}


static void dec_out(void *refcon, void *srcref, OSStatus status, VTDecodeInfoFlags flags,
		    CVImageBufferRef img, CMTime pts, CMTime dur)
{
	struct viddec_state *st = refcon;
	(void)srcref; (void)flags; (void)pts; (void)dur;
	if (status != noErr || !img)
		return;
	if (st->next)
		CVPixelBufferRelease(st->next);
	st->next = CVPixelBufferRetain(img);
}


static int dec_update(struct viddec_state **vdsp, const struct vidcodec *vc, const char *fmtp,
		      const struct video *vid)
{
	(void)fmtp; (void)vid;
	if (!vdsp || !vc)
		return EINVAL;
	if (*vdsp)
		return 0;
	struct viddec_state *st = mem_zalloc(sizeof(*st), dec_destructor);
	if (!st)
		return ENOMEM;
	st->h265 = 0 == str_casecmp(vc->name, "H265");
	st->mb = mbuf_alloc(65536);
	if (!st->mb) {
		mem_deref(st);
		return ENOMEM;
	}
	*vdsp = st;
	return 0;
}


static void set_ps(struct viddec_state *st, int i, const uint8_t *p, size_t n)
{
	if (st->ps[i] && st->ps[i]->end == n && 0 == memcmp(st->ps[i]->buf, p, n))
		return;
	mem_deref(st->ps[i]);
	st->ps[i] = mbuf_alloc(n);
	if (st->ps[i])
		mbuf_write_mem(st->ps[i], p, n);
	st->ps_changed = true;
}


static int dec_open(struct viddec_state *st)
{
	int nps = st->h265 ? 3 : 2;
	const uint8_t *p[3];
	size_t n[3];
	for (int i = 0; i < nps; i++) {
		if (!st->ps[i])
			return EPROTO;
		p[i] = st->ps[i]->buf;
		n[i] = st->ps[i]->end;
	}
	dec_reset(st);
	OSStatus s = st->h265
		? CMVideoFormatDescriptionCreateFromHEVCParameterSets(NULL, (size_t)nps, p, n, 4, NULL, &st->fd)
		: CMVideoFormatDescriptionCreateFromH264ParameterSets(NULL, (size_t)nps, p, n, 4, &st->fd);
	if (s != noErr) {
		warning("cmevideo: Parameter-Sets ungültig (%d)\n", (int)s);
		st->fd = NULL;
		return EPROTO;
	}
	NSDictionary *attrs = @{
		(id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
		(id)kCVPixelBufferIOSurfacePropertiesKey: @{} };
	VTDecompressionOutputCallbackRecord cb = { dec_out, st };
	s = VTDecompressionSessionCreate(NULL, st->fd, NULL, (__bridge CFDictionaryRef)attrs, &cb, &st->sess);
	if (s != noErr) {
		warning("cmevideo: Dekodierer %s: %d\n", st->h265 ? "H265" : "H264", (int)s);
		st->sess = NULL;
		return ENODEV;
	}
	CMVideoDimensions d = CMVideoFormatDescriptionGetDimensions(st->fd);
	info("cmevideo: Dekodierer %s %dx%d\n", st->h265 ? "H265" : "H264", d.width, d.height);
	st->ps_changed = false;
	return 0;
}


/* fertiges Bild (Annex-B in st->mb) dekodieren */
static int dec_frame(struct viddec_state *st, struct vidframe *frame, bool *intra)
{
	const uint8_t *start = st->mb->buf, *end = st->mb->buf + st->mb->end;
	struct mbuf *avcc = mbuf_alloc(st->mb->end + 64);
	bool key = false;
	int err = 0;

	if (!avcc)
		return ENOMEM;
	const uint8_t *r = h264_find_startcode(start, end);
	while (r < end) {
		while (r < end && !*r)
			r++;
		r++;                          /* hinter der 1 des Startcodes */
		const uint8_t *next = h264_find_startcode(r, end);
		size_t n = (size_t)(next - r);
		while (n > 0 && r[n - 1] == 0 && next < end)
			n--;                  /* Nullen vor dem nächsten Startcode */
		if (r < end && n > 0) {
			unsigned t = st->h265 ? (r[0] >> 1) & 0x3f : r[0] & 0x1f;
			if (st->h265 && t >= 32 && t <= 34)
				set_ps(st, (int)t - 32, r, n);
			else if (!st->h265 && (t == 7 || t == 8))
				set_ps(st, t == 7 ? 0 : 1, r, n);
			else {
				if (st->h265 ? (t >= 16 && t <= 21) : t == 5)
					key = true;
				mbuf_write_u32(avcc, htonl((uint32_t)n));
				mbuf_write_mem(avcc, r, n);
			}
		}
		r = next;
	}
	*intra = key;

	if (!st->sess || st->ps_changed) {
		err = dec_open(st);
		if (err)
			goto out;
	}
	if (!avcc->end)
		goto out;

	CMBlockBufferRef bb = NULL;
	OSStatus s = CMBlockBufferCreateWithMemoryBlock(NULL, NULL, avcc->end, NULL, NULL, 0, avcc->end, 0, &bb);
	if (s == kCMBlockBufferNoErr)
		s = CMBlockBufferReplaceDataBytes(avcc->buf, bb, 0, avcc->end);
	CMSampleBufferRef sb = NULL;
	size_t sz = avcc->end;
	if (s == noErr)
		s = CMSampleBufferCreateReady(NULL, bb, st->fd, 1, 0, NULL, 1, &sz, &sb);
	if (bb)
		CFRelease(bb);
	if (s != noErr || !sb) {
		err = ENOMEM;
		goto out;
	}
	VTDecodeInfoFlags info_flags = 0;
	s = VTDecompressionSessionDecodeFrame(st->sess, sb, 0, NULL, &info_flags);
	VTDecompressionSessionWaitForAsynchronousFrames(st->sess);
	CFRelease(sb);
	if (s != noErr) {
		if (s == kVTInvalidSessionErr)
			dec_reset(st);
		err = EPROTO;
		goto out;
	}
	if (!st->next)
		goto out;

	dec_release_cur(st);
	st->cur = st->next;
	st->next = NULL;
	CVPixelBufferLockBaseAddress(st->cur, kCVPixelBufferLock_ReadOnly);
	frame->fmt = VID_FMT_NV12;
	frame->size.w = (unsigned)CVPixelBufferGetWidth(st->cur);
	frame->size.h = (unsigned)CVPixelBufferGetHeight(st->cur);
	for (int i = 0; i < 2; i++) {
		frame->data[i] = CVPixelBufferGetBaseAddressOfPlane(st->cur, i);
		frame->linesize[i] = (uint16_t)CVPixelBufferGetBytesPerRowOfPlane(st->cur, i);
	}
	frame->data[2] = frame->data[3] = NULL;
	last_decoded = st->cur;
 out:
	mem_deref(avcc);
	return err;
}


static inline void frag_rewind(struct viddec_state *st)
{
	st->mb->pos = st->frag_start;
	st->mb->end = st->frag_start;
}


static int dec_decode_h264(struct viddec_state *st, struct vidframe *frame, struct viddec_packet *pkt)
{
	static const uint8_t sc[3] = {0, 0, 1};
	struct h264_nal_header hdr;
	struct mbuf *src = pkt->mb;
	int err;

	pkt->intra = false;
	err = h264_nal_header_decode(&hdr, src);
	if (err)
		return err;
	if (st->frag && hdr.type != H264_NALU_FU_A) {
		frag_rewind(st);
		st->frag = false;
		++st->n_lost;
	}
	if (1 <= hdr.type && hdr.type <= 23) {
		--src->pos;
		err = mbuf_write_mem(st->mb, sc, 3);
		err |= mbuf_write_mem(st->mb, mbuf_buf(src), mbuf_get_left(src));
	}
	else if (hdr.type == H264_NALU_FU_A) {
		struct h264_fu fu;
		err = h264_fu_hdr_decode(&fu, src);
		if (err)
			return err;
		hdr.type = fu.type;
		if (fu.s) {
			if (st->frag) {
				frag_rewind(st);
				++st->n_lost;
			}
			st->frag_start = st->mb->pos;
			st->frag = true;
			mbuf_write_mem(st->mb, sc, 3);
			err = h264_nal_header_encode(st->mb, &hdr);
		}
		else {
			if (!st->frag || rtp_seq_diff(st->frag_seq, pkt->hdr->seq) != 1) {
				if (st->frag)
					frag_rewind(st);
				st->frag = false;
				++st->n_lost;
				return 0;
			}
		}
		err |= mbuf_write_mem(st->mb, mbuf_buf(src), mbuf_get_left(src));
		if (fu.e)
			st->frag = false;
		st->frag_seq = pkt->hdr->seq;
	}
	else if (hdr.type == H264_NALU_STAP_A) {
		err = h264_stap_decode_annexb(st->mb, src);
	}
	else {
		return EBADMSG;
	}
	if (err)
		goto out;
	if (!pkt->hdr->m) {
		if (st->mb->end > DECODE_MAXSZ) {
			err = ENOMEM;
			goto out;
		}
		return 0;
	}
	if (st->frag) {
		err = EPROTO;
		goto out;
	}
	err = dec_frame(st, frame, &pkt->intra);
 out:
	mbuf_rewind(st->mb);
	st->frag = false;
	return err;
}


static int dec_decode_h265(struct viddec_state *st, struct vidframe *frame, struct viddec_packet *pkt)
{
	static const uint8_t sc[3] = {0, 0, 1};
	struct h265_nal hdr;
	struct mbuf *mb = pkt->mb;
	int err;

	pkt->intra = false;
	if (mbuf_get_left(mb) < H265_HDR_SIZE)
		return EBADMSG;
	err = h265_nal_decode(&hdr, mbuf_buf(mb));
	if (err)
		return err;
	mbuf_advance(mb, H265_HDR_SIZE);
	if (st->frag && hdr.nal_unit_type != H265_NAL_FU) {
		frag_rewind(st);
		st->frag = false;
	}
	if (hdr.nal_unit_type <= 40) {
		mb->pos -= H265_HDR_SIZE;
		err = mbuf_write_mem(st->mb, sc, 3);
		err |= mbuf_write_mem(st->mb, mbuf_buf(mb), mbuf_get_left(mb));
	}
	else if (hdr.nal_unit_type == H265_NAL_FU) {
		if (mbuf_get_left(mb) < 1)
			return EBADMSG;
		uint8_t v = mbuf_read_u8(mb);
		bool s = v >> 7 & 1, e = v >> 6 & 1;
		if (s) {
			if (st->frag)
				frag_rewind(st);
			st->frag_start = st->mb->pos;
			st->frag = true;
			hdr.nal_unit_type = v & 0x3f;
			err = mbuf_write_mem(st->mb, sc, 3);
			err |= h265_nal_encode_mbuf(st->mb, &hdr);
		}
		else if (!st->frag || rtp_seq_diff(st->frag_seq, pkt->hdr->seq) != 1) {
			if (st->frag)
				frag_rewind(st);
			st->frag = false;
			++st->n_lost;
			return 0;
		}
		err |= mbuf_write_mem(st->mb, mbuf_buf(mb), mbuf_get_left(mb));
		if (e)
			st->frag = false;
		st->frag_seq = pkt->hdr->seq;
	}
	else if (hdr.nal_unit_type == H265_NAL_AP) {
		while (!err && mbuf_get_left(mb) >= 2) {
			const uint16_t len = ntohs(mbuf_read_u16(mb));
			if (mbuf_get_left(mb) < len)
				return EBADMSG;
			err = mbuf_write_mem(st->mb, sc, 3);
			err |= mbuf_write_mem(st->mb, mbuf_buf(mb), len);
			mb->pos += len;
		}
	}
	else {
		return EPROTO;
	}
	if (err)
		goto out;
	if (!pkt->hdr->m) {
		if (st->mb->end > DECODE_MAXSZ) {
			err = ENOMEM;
			goto out;
		}
		return 0;
	}
	if (st->frag) {
		err = EPROTO;
		goto out;
	}
	err = dec_frame(st, frame, &pkt->intra);
 out:
	mbuf_rewind(st->mb);
	st->frag = false;
	return err;
}


static int dec_decode(struct viddec_state *st, struct vidframe *frame, struct viddec_packet *pkt)
{
	if (!st || !frame || !pkt || !pkt->mb)
		return EINVAL;
	return st->h265 ? dec_decode_h265(st, frame, pkt) : dec_decode_h264(st, frame, pkt);
}


/* ---------------------------------------------------------------- SDP */

static uint32_t packetization_mode(const char *fmtp)
{
	struct pl pl, mode;
	if (!fmtp)
		return 0;
	pl_set_str(&pl, fmtp);
	return fmt_param_get(&pl, "packetization-mode", &mode) ? pl_u32(&mode) : 0;
}


static int h264_fmtp_enc(struct mbuf *mb, const struct sdp_format *fmt, bool offer, void *arg)
{
	(void)offer; (void)arg;
	if (!mb || !fmt)
		return 0;
	/* Constrained Baseline, Level 3.1 (720p30) – das verstehen auch die Cisco-Telefone */
	return mbuf_printf(mb, "a=fmtp:%s packetization-mode=1;profile-level-id=42e01f\r\n", fmt->id);
}


static bool h264_fmtp_cmp(const char *lfmtp, const char *rfmtp, void *arg)
{
	(void)lfmtp; (void)arg;
	return packetization_mode(rfmtp) == 1;
}


/* ---------------------------------------------------------------- Anmeldung */

static struct vidcodec vc_h264 = {
	.name = "H264", .variant = "packetization-mode=1",
	.encupdh = enc_update, .ench = enc_encode, .decupdh = dec_update, .dech = dec_decode,
	.fmtp_ench = h264_fmtp_enc, .fmtp_cmph = h264_fmtp_cmp,
};

static struct vidcodec vc_h265 = {
	.name = "H265",
	.encupdh = enc_update, .ench = enc_encode, .decupdh = dec_update, .dech = dec_decode,
};

static struct vidsrc *camsrc;
static struct vidisp *disp;


int cmevideo_init(void)
{
	int err;
	/* H265 zuerst: sparsamer; Gegenstellen ohne H265 nehmen H264 */
	vidcodec_register(baresip_vidcodecl(), &vc_h265);
	vidcodec_register(baresip_vidcodecl(), &vc_h264);
	err = vidsrc_register(&camsrc, baresip_vidsrcl(), "cmecam", cam_alloc, NULL);
	err |= vidisp_register(&disp, baresip_vidispl(), "cmedisp", disp_alloc, NULL, disp_disp, NULL);
	return err;
}


void cmevideo_close(void)
{
	camsrc = mem_deref(camsrc);
	disp = mem_deref(disp);
	vidcodec_unregister(&vc_h264);
	vidcodec_unregister(&vc_h265);
}
