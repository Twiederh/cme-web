/*
 * cmesip.c – baresip für CME Phone (siehe cmesip.h).
 *
 * Ein Thread führt libre/baresip (re_main). Befehle aus anderen Threads laufen über eine mqueue im
 * baresip-Thread (run()); der Aufrufer wartet auf das Ergebnis. Mit re_thread_enter() hielte der Aufrufer
 * die Ereignisschleife fest – das Starten des Tons beim Annehmen wartet aber auf genau diese Schleife.
 */
#include <TargetConditionals.h>
#include <pthread.h>
#include <stdlib.h>
#include <semaphore.h>
#include <string.h>
#include <stdarg.h>
#include <stdio.h>
#include <errno.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>
#include "cmesip.h"

/* cmevideo.m */
int  cmevideo_init(void);
void cmevideo_close(void);

static struct {
	pthread_t thread;
	pthread_mutex_t lock;     /* nur für Start/Stopp */
	pthread_cond_t cond;
	bool ready;
	bool running;
	int start_err;
	char *software, *config, *modules;
	cmesip_event_h *eh;
	void *arg;
	struct ua *ua;
	struct mqueue *mq;
} g = { .lock = PTHREAD_MUTEX_INITIALIZER, .cond = PTHREAD_COND_INITIALIZER };


static struct list subl;

static void emit(int ev, const char *call_id, const char *peer, const char *text)
{
	if (g.eh)
		g.eh(ev, call_id, peer, text, g.arg);
}


static void log_handler(uint32_t level, const char *msg)
{
	(void)level;
	emit(CMESIP_LOG, NULL, NULL, msg);
}


static struct log lg = { .h = log_handler };


static void event_handler(enum bevent_ev ev, struct bevent *event, void *arg)
{
	struct call *call = bevent_get_call(event);
	const char *txt = bevent_get_text(event);
	const char *cid = call ? call_id(call) : NULL;
	const char *peer = call ? call_peeruri(call) : NULL;
	(void)arg;

	switch (ev) {
	case BEVENT_SIPSESS_CONN: {
		/* Eingehender Anruf: annehmen (klingeln), die App entscheidet dann über Annehmen/Ablehnen */
		const struct sip_msg *msg = bevent_get_msg(event);
		struct ua *ua = uag_find_msg(msg);
		char cid[256];
		int err;

		/* Zweiter Zweig desselben Anrufs (z.B. Kamailio nach zwei Anmeldungen kurz nacheinander): als
		 * "Merged Request" ablehnen (RFC 3261 8.2.2.2). Sonst gäbe es zwei Gespräche mit derselben Call-ID,
		 * und das CANCEL des einen beendete in der App das angenommene. */
		pl_strcpy(&msg->callid, cid, sizeof(cid));
		if (uag_call_find(cid)) {
			info("cmesip: zweites INVITE zu %s – 482\n", cid);
			(void)sip_treply(NULL, uag_sip(), msg, 482, "Loop Detected");
			bevent_stop(event);
			break;
		}
		err = ua ? ua_accept(ua, msg) : ENOENT;
		if (err) {
			warning("cmesip: eingehender Anruf nicht angenommen: %m\n", err);
			if (!ua)
				(void)sip_treply(NULL, uag_sip(), msg, 404, "Not Found");
		}
		bevent_stop(event);
		break;
	}
	case BEVENT_REGISTER_OK:      emit(CMESIP_REGISTER_OK, NULL, NULL, txt); break;
	case BEVENT_REGISTER_FAIL:    emit(CMESIP_REGISTER_FAIL, NULL, NULL, txt); break;
	case BEVENT_UNREGISTERING:    emit(CMESIP_UNREGISTERED, NULL, NULL, txt); break;
	case BEVENT_CALL_INCOMING:
		emit(CMESIP_CALL_INCOMING, cid, peer, call ? call_peername(call) : NULL);
		break;
	case BEVENT_CALL_OUTGOING:    emit(CMESIP_CALL_OUTGOING, cid, peer, txt); break;
	case BEVENT_CALL_RINGING:     emit(CMESIP_CALL_RINGING, cid, peer, txt); break;
	case BEVENT_CALL_PROGRESS:    emit(CMESIP_CALL_PROGRESS, cid, peer, txt); break;
	case BEVENT_CALL_ESTABLISHED: emit(CMESIP_CALL_ESTABLISHED, cid, peer, txt); break;
	case BEVENT_CALL_CLOSED:      emit(CMESIP_CALL_CLOSED, cid, peer, txt); break;
	case BEVENT_CALL_HOLD:        emit(CMESIP_CALL_HOLD, cid, peer, txt); break;
	case BEVENT_CALL_RESUME:      emit(CMESIP_CALL_RESUME, cid, peer, txt); break;
	case BEVENT_CALL_MENC:        emit(CMESIP_CALL_MENC, cid, peer, txt); break;
	case BEVENT_CALL_DTMF_START:  emit(CMESIP_CALL_DTMF, cid, peer, txt); break;
	default: {
		char buf[160];
		re_snprintf(buf, sizeof(buf), "cmesip: Ereignis %s %s\n", bevent_str(ev), txt ? txt : "");
		emit(CMESIP_LOG, cid, peer, buf);
		break;
	}
	}
}


/* Befehle an den baresip-Thread (weckt die Ereignisschleife auf) */
enum { MQ_STOP = 1, MQ_CALL };

struct cmesip_cmd {
	int (^blk)(void);
	int err;
	bool done;
	pthread_mutex_t m;
	pthread_cond_t c;
};

static void mqueue_handler(int id, void *data, void *arg)
{
	(void)arg;
	if (id == MQ_STOP) {
		re_cancel();
	}
	else if (id == MQ_CALL) {
		struct cmesip_cmd *c = data;
		int err = c->blk();
		pthread_mutex_lock(&c->m);
		c->err = err;
		c->done = true;
		pthread_cond_signal(&c->c);
		pthread_mutex_unlock(&c->m);
	}
}


/* --- Lokale Aufnahme: Audiofilter mischt Mikrofon (tx) und Gegenstelle (rx) in eine WAV-Datei (mono, 16 bit) --- */

#define REC_RING 48000   /* Puffer für die Gegenstelle: 1 s bei 48 kHz */

struct rec_ctx {
	struct le le;
	const struct audio *au;
	mtx_t *mtx;
	FILE *f;
	uint32_t srate;
	size_t bytes;
	int16_t ring[REC_RING];
	size_t rpos, rlen;
};

struct rec_enc { struct aufilt_enc_st af; struct rec_ctx *ctx; };
struct rec_dec { struct aufilt_dec_st af; struct rec_ctx *ctx; };

static struct list recl;
static mtx_t *recl_mtx;

static void wav_header(FILE *f, uint32_t srate, uint32_t bytes)
{
	uint8_t h[44];
	uint32_t v;
	memcpy(h, "RIFF", 4); v = 36 + bytes; memcpy(h + 4, &v, 4);
	memcpy(h + 8, "WAVEfmt ", 8); v = 16; memcpy(h + 16, &v, 4);
	h[20] = 1; h[21] = 0; h[22] = 1; h[23] = 0;           /* PCM, mono */
	memcpy(h + 24, &srate, 4); v = srate * 2; memcpy(h + 28, &v, 4);
	h[32] = 2; h[33] = 0; h[34] = 16; h[35] = 0;
	memcpy(h + 36, "data", 4); memcpy(h + 40, &bytes, 4);
	fseek(f, 0, SEEK_SET);
	fwrite(h, 1, sizeof(h), f);
	fseek(f, 0, SEEK_END);
}

/* mit ctx->mtx */
static void rec_close(struct rec_ctx *ctx)
{
	if (!ctx->f)
		return;
	wav_header(ctx->f, ctx->srate, (uint32_t)ctx->bytes);
	fclose(ctx->f);
	ctx->f = NULL;
	info("cmesip: Aufnahme beendet (%zu Byte)\n", ctx->bytes);
}

static void rec_ctx_destructor(void *arg)
{
	struct rec_ctx *ctx = arg;
	mtx_lock(recl_mtx);
	list_unlink(&ctx->le);
	mtx_unlock(recl_mtx);
	mtx_lock(ctx->mtx);
	rec_close(ctx);
	mtx_unlock(ctx->mtx);
	mem_deref(ctx->mtx);
}

static int rec_ctx_get(struct rec_ctx **ctxp, void **ctx, const struct audio *au)
{
	struct rec_ctx *c = *ctx;
	struct le *le;
	/* Encoder und Decoder werden evtl. getrennt eingerichtet (eigenes ctx): vorhandenen Zustand suchen */
	if (!c) {
		mtx_lock(recl_mtx);
		for (le = list_head(&recl); le; le = le->next) {
			struct rec_ctx *x = le->data;
			if (x->au == au) {
				c = x;
				break;
			}
		}
		mtx_unlock(recl_mtx);
		if (c)
			*ctx = c;
	}
	if (!c) {
		c = mem_zalloc(sizeof(*c), rec_ctx_destructor);
		if (!c)
			return ENOMEM;
		if (mutex_alloc(&c->mtx)) {
			mem_deref(c);
			return ENOMEM;
		}
		c->au = au;
		mtx_lock(recl_mtx);
		list_append(&recl, &c->le, c);
		mtx_unlock(recl_mtx);
		*ctx = c;
		*ctxp = c;          /* erste Referenz */
	}
	else {
		*ctxp = mem_ref(c);
	}
	return 0;
}

static void rec_enc_destructor(void *arg) { mem_deref(((struct rec_enc *)arg)->ctx); }
static void rec_dec_destructor(void *arg) { mem_deref(((struct rec_dec *)arg)->ctx); }

static int rec_encupd(struct aufilt_enc_st **stp, void **ctx, const struct aufilt *af,
		      struct aufilt_prm *prm, const struct audio *au)
{
	struct rec_enc *st;
	int err;
	(void)af;
	if (!stp || !ctx || !prm)
		return EINVAL;
	if (*stp)
		return 0;
	st = mem_zalloc(sizeof(*st), rec_enc_destructor);
	if (!st)
		return ENOMEM;
	err = rec_ctx_get(&st->ctx, ctx, au);
	if (err) {
		mem_deref(st);
		return err;
	}
	mtx_lock(st->ctx->mtx);
	st->ctx->srate = prm->srate;
	mtx_unlock(st->ctx->mtx);
	*stp = (struct aufilt_enc_st *)st;
	return 0;
}

static int rec_decupd(struct aufilt_dec_st **stp, void **ctx, const struct aufilt *af,
		      struct aufilt_prm *prm, const struct audio *au)
{
	struct rec_dec *st;
	int err;
	(void)af; (void)prm;
	if (!stp || !ctx)
		return EINVAL;
	if (*stp)
		return 0;
	st = mem_zalloc(sizeof(*st), rec_dec_destructor);
	if (!st)
		return ENOMEM;
	err = rec_ctx_get(&st->ctx, ctx, au);
	if (err) {
		mem_deref(st);
		return err;
	}
	*stp = (struct aufilt_dec_st *)st;
	return 0;
}

/* Gegenstelle: in den Ring */
static int rec_decode(struct aufilt_dec_st *st, struct auframe *af)
{
	struct rec_ctx *c = ((struct rec_dec *)st)->ctx;
	const int16_t *s = af->sampv;
	size_t i;
	if (af->fmt != AUFMT_S16LE || af->ch != 1)
		return 0;
	mtx_lock(c->mtx);
	if (c->f && af->srate == c->srate) {
		for (i = 0; i < af->sampc; i++) {
			c->ring[(c->rpos + c->rlen) % REC_RING] = s[i];
			if (c->rlen < REC_RING)
				c->rlen++;
			else
				c->rpos = (c->rpos + 1) % REC_RING;
		}
	}
	mtx_unlock(c->mtx);
	return 0;
}

/* Mikrofon: mit der Gegenstelle mischen und schreiben (Takt der Aufnahme) */
static int rec_encode(struct aufilt_enc_st *st, struct auframe *af)
{
	struct rec_ctx *c = ((struct rec_enc *)st)->ctx;
	const int16_t *s = af->sampv;
	int16_t buf[960];
	size_t i, done = 0;
	if (af->fmt != AUFMT_S16LE || af->ch != 1)
		return 0;
	mtx_lock(c->mtx);
	while (c->f && done < af->sampc) {
		size_t n = af->sampc - done;
		if (n > RE_ARRAY_SIZE(buf))
			n = RE_ARRAY_SIZE(buf);
		for (i = 0; i < n; i++) {
			int32_t v = s[done + i];
			if (c->rlen) {
				v += c->ring[c->rpos];
				c->rpos = (c->rpos + 1) % REC_RING;
				c->rlen--;
			}
			buf[i] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
		}
		c->bytes += fwrite(buf, 2, n, c->f) * 2;
		done += n;
	}
	mtx_unlock(c->mtx);
	return 0;
}

static struct aufilt rec_filt = {
	.name = "cmerec",
	.enabled = true,
	.encupdh = rec_encupd, .ench = rec_encode,
	.decupdh = rec_decupd, .dech = rec_decode,
};


static int load_modules(const char *list)
{
	char name[64];
	const char *p = list;
	int err = 0;

	while (p && *p) {
		const char *e = strchr(p, ',');
		size_t n = e ? (size_t)(e - p) : strlen(p);
		if (n && n < sizeof(name)) {
			memcpy(name, p, n);
			name[n] = '\0';
			int e2 = module_load(".", name);
			if (e2) {
				warning("cmesip: Modul %s: %m\n", name, e2);
				err = e2;
			}
		}
		p = e ? e + 1 : NULL;
	}
	return err;
}


static void *thread_main(void *unused)
{
	int err;
	(void)unused;

	err = libre_init();
	if (err)
		goto out;

	log_register_handler(&lg);
	log_enable_stdout(false);

	err = conf_configure_buf((const uint8_t *)g.config, str_len(g.config));
	if (err)
		goto out;
	err = baresip_init(conf_config());
	if (err)
		goto out;
	err = ua_init(g.software, true, true, true);   /* TLS zum SIP-Gateway (OpenSSL) */
	if (err)
		goto out;
	err = load_modules(g.modules);
	if (err)
		goto out;
	err = bevent_register(event_handler, NULL);
	if (err)
		goto out;
	if (!err)
		err = mutex_alloc(&recl_mtx);
	if (!err)
		aufilt_register(baresip_aufiltl(), &rec_filt);
	if (!err)
		err = cmevideo_init();
	err = mqueue_alloc(&g.mq, mqueue_handler, NULL);

 out:
	pthread_mutex_lock(&g.lock);
	g.start_err = err;
	g.ready = true;
	g.running = !err;
	pthread_cond_broadcast(&g.cond);
	pthread_mutex_unlock(&g.lock);

	if (!err)
		re_main(NULL);

	/* Aufräumen im selben Thread */
	list_flush(&subl);
	aufilt_unregister(&rec_filt);
	cmevideo_close();
	recl_mtx = mem_deref(recl_mtx);
	bevent_unregister(event_handler);
	g.mq = mem_deref(g.mq);
	g.ua = mem_deref(g.ua);
	ua_stop_all(true);
	ua_close();
	baresip_close();
	mod_close();
	log_unregister_handler(&lg);
	libre_close();

	pthread_mutex_lock(&g.lock);
	g.running = false;
	pthread_mutex_unlock(&g.lock);
	return NULL;
}


int cmesip_start(const char *software, const char *config, const char *modules,
		 cmesip_event_h *eh, void *arg)
{
	int err;

	pthread_mutex_lock(&g.lock);
	if (g.running) {
		pthread_mutex_unlock(&g.lock);
		return EALREADY;
	}
	free(g.software); free(g.config); free(g.modules);
	g.software = strdup(software ? software : "CME Phone");
	g.config = strdup(config ? config : "");
	g.modules = strdup(modules ? modules : "");
	g.eh = eh;
	g.arg = arg;
	g.ready = false;
	g.start_err = 0;
	pthread_mutex_unlock(&g.lock);

	err = pthread_create(&g.thread, NULL, thread_main, NULL);
	if (err)
		return err;

	pthread_mutex_lock(&g.lock);
	while (!g.ready)
		pthread_cond_wait(&g.cond, &g.lock);
	err = g.start_err;
	pthread_mutex_unlock(&g.lock);

	if (err)
		pthread_join(g.thread, NULL);
	return err;
}


void cmesip_stop(void)
{
	if (!cmesip_running())
		return;
	mqueue_push(g.mq, MQ_STOP, NULL);
	pthread_join(g.thread, NULL);
}


bool cmesip_running(void)
{
	bool r;
	pthread_mutex_lock(&g.lock);
	r = g.running;
	pthread_mutex_unlock(&g.lock);
	return r;
}


/* blk im baresip-Thread ausführen und auf das Ergebnis warten */
static int run(int (^blk)(void))
{
	struct cmesip_cmd c = { .blk = blk };
	int err;

	if (!cmesip_running())
		return ESRCH;
	if (pthread_equal(pthread_self(), g.thread))
		return blk();

	pthread_mutex_init(&c.m, NULL);
	pthread_cond_init(&c.c, NULL);
	err = mqueue_push(g.mq, MQ_CALL, &c);
	if (!err) {
		pthread_mutex_lock(&c.m);
		while (!c.done)
			pthread_cond_wait(&c.c, &c.m);
		err = c.err;
		pthread_mutex_unlock(&c.m);
	}
	pthread_cond_destroy(&c.c);
	pthread_mutex_destroy(&c.m);
	return err;
}


static struct call *find_call(const char *call_id)
{
	return call_id ? uag_call_find(call_id) : NULL;
}


int cmesip_set_account(const char *aor, const char *uri_params, const char *hdr_params,
		       const char *reg_hdr)
{
	return run(^int(void) {
		int err;
		g.ua = mem_deref(g.ua);
		err = sip_contact_set_params(uri_params, hdr_params);
		if (!err)
			err = ua_alloc(&g.ua, aor);
		if (!err && reg_hdr && *reg_hdr) {
			/* "Name: Wert" */
			const char *c = strchr(reg_hdr, ':');
			char name[64];
			if (c && (size_t)(c - reg_hdr) < sizeof(name)) {
				memcpy(name, reg_hdr, c - reg_hdr);
				name[c - reg_hdr] = '\0';
				c++;
				while (*c == ' ')
					c++;
				err = ua_set_reg_hdr(g.ua, name, c);
			}
		}
		if (!err && account_regint(ua_account(g.ua)) > 0)
			err = ua_register(g.ua);
		return err;
	});
}


int cmesip_register(void)
{
	return run(^int(void) {
		return g.ua ? ua_register(g.ua) : ENOENT;
	});
}


int cmesip_unregister(void)
{
	return run(^int(void) {
		if (g.ua)
			ua_unregister(g.ua);
		return 0;
	});
}


void cmesip_remove_account(void)
{
	(void)run(^int(void) {
		if (g.ua) {
			ua_unregister(g.ua);
			g.ua = mem_deref(g.ua);
		}
		return 0;
	});
}


int cmesip_network_changed(void)
{
	return run(^int(void) {
		net_dns_refresh(baresip_network());
		return uag_reset_transp(true, true);
	});
}


/* Replaces-Wert für den URI-Header escapen */
static int print_escaped(struct re_printf *pf, const char *s)
{
	int err = 0;
	for (; s && *s && !err; s++) {
		if (*s == ';' || *s == '=' || *s == '@' || *s == ' ' || *s == '?' || *s == '&')
			err = re_hprintf(pf, "%%%02X", (unsigned char)*s);
		else
			err = re_hprintf(pf, "%c", *s);
	}
	return err;
}


int cmesip_dial(const char *uri, const char *from_uri, const char *replaces, bool video,
		char *cid_out, size_t cid_size)
{
	if (!uri)
		return EINVAL;

	return run(^int(void) {
		struct call *call = NULL;
		char *target = NULL;
		int err;

		if (!g.ua)
			return ENOENT;
		if (replaces && *replaces)
			err = re_sdprintf(&target, "<%s?Replaces=%H>", uri, print_escaped, replaces);
		else
			err = re_sdprintf(&target, "%s", uri);
		if (!err)
			err = ua_connect(g.ua, &call, from_uri, target, video ? VIDMODE_ON : VIDMODE_OFF);
		if (!err && call && cid_out && cid_size)
			str_ncpy(cid_out, call_id(call), cid_size);
		mem_deref(target);
		return err;
	});
}


int cmesip_answer(const char *call_id)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		/* mit Video nur, wenn die Gegenstelle es anbietet (sonst bliebe ein m=video im Angebot) */
		return call ? ua_answer(call_get_ua(call), call, call_has_video(call) ? VIDMODE_ON : VIDMODE_OFF)
			: ENOENT;
	});
}


int cmesip_hangup(const char *call_id, unsigned scode)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		if (!call)
			return ENOENT;
		ua_hangup(call_get_ua(call), call, (uint16_t)scode, NULL);
		return 0;
	});
}


int cmesip_hold(const char *call_id, bool hold, const char *hdrs)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		int err;
		if (!call)
			return ENOENT;
		err = call_set_reinvite_hdrs(call, hdrs);
		if (!err)
			err = call_hold(call, hold);
		(void)call_set_reinvite_hdrs(call, NULL);
		return err;
	});
}


int cmesip_mute(const char *call_id, bool mute)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		if (!call)
			return ENOENT;
		audio_mute(call_audio(call), mute);
		return 0;
	});
}


int cmesip_send_dtmf(const char *call_id, char key)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		int err = call ? call_send_digit(call, key) : ENOENT;
		if (!err)
			err = call_send_digit(call, KEYCODE_REL);
		return err;
	});
}


int cmesip_call_info(const char *call_id, char *buf, size_t size)
{
	if (!buf || !size)
		return EINVAL;
	return run(^int(void) {
		struct call *call = find_call(call_id);
		if (!call)
			return ENOENT;
		const struct aucodec *ac = audio_codec(call_audio(call), true);
		struct stream *s = audio_strm(call_audio(call));
		const struct vidcodec *vc = call_video(call) ? video_codec(call_video(call), true) : NULL;
		re_snprintf(buf, size, "codec=%s/%u menc=%s vcodec=%s",
			    ac ? ac->name : "-", ac ? ac->srate : 0,
			    s && stream_is_secure(s) ? "srtp" : "none", vc ? vc->name : "-");
		return 0;
	});
}


int cmesip_call_count(void)
{
	__block int n = 0;
	(void)run(^int(void) {
		struct le *le;
		for (le = list_head(uag_list()); le; le = le->next)
			n += (int)list_count(ua_calls(le->data));
		return 0;
	});
	return n;
}


static void trace_handler(bool tx, enum sip_transp tp, const struct sa *src, const struct sa *dst,
			  const uint8_t *pkt, size_t len, void *arg)
{
	char who[80];
	char *msg = NULL;
	(void)arg;

	re_snprintf(who, sizeof(who), "%s %s %J", tx ? "tx" : "rx", sip_transp_name(tp), tx ? dst : src);
	if (re_sdprintf(&msg, "%b", pkt, len))
		return;
	emit(CMESIP_TRACE, NULL, who, msg);
	mem_deref(msg);
}


int cmesip_set_trace(bool on)
{
	return run(^int(void) {
		sip_set_trace_handler(uag_sip(), on ? trace_handler : NULL);
		return 0;
	});
}


/* --- Abos (SUBSCRIBE/NOTIFY) --- */

struct csub {
	struct le le;
	struct sipsub *sub;
	int id;
	char event[32];
	char idstr[16];
};

static int sub_next = 1;

static void csub_destructor(void *data)
{
	struct csub *cs = data;
	list_unlink(&cs->le);
	mem_deref(cs->sub);
}

static int sub_auth_handler(char **username, char **password, const char *realm, void *arg)
{
	(void)arg;
	return g.ua ? account_auth(ua_account(g.ua), username, password, realm) : ENOENT;
}

static void sub_notify_handler(struct sip *sip, const struct sip_msg *msg, void *arg)
{
	struct csub *cs = arg;
	char *body = NULL;

	(void)sip_treply(NULL, sip, msg, 200, "OK");
	if (mbuf_get_left(msg->mb))
		(void)re_sdprintf(&body, "%b", mbuf_buf(msg->mb), mbuf_get_left(msg->mb));
	emit(CMESIP_NOTIFY, cs->idstr, cs->event, body ? body : "");
	mem_deref(body);
}

static void sub_close_handler(int err, const struct sip_msg *msg,
			      const struct sipevent_substate *substate, void *arg)
{
	struct csub *cs = arg;
	char txt[96];
	(void)substate;

	if (msg)
		re_snprintf(txt, sizeof(txt), "%u %r", msg->scode, &msg->reason);
	else
		re_snprintf(txt, sizeof(txt), "%m", err);
	emit(CMESIP_SUB_CLOSED, cs->idstr, cs->event, txt);
	mem_deref(cs);
}

int cmesip_subscribe(const char *uri, const char *event, const char *from_uri, unsigned expires,
		     const char *hdrs)
{
	if (!uri || !event)
		return -EINVAL;

	__block int id = 0;
	int err = run(^int(void) {
		struct csub *cs;
		const char *routev[1];
		int e;

		if (!g.ua)
			return ENOENT;
		cs = mem_zalloc(sizeof(*cs), csub_destructor);
		if (!cs)
			return ENOMEM;
		cs->id = sub_next++;
		re_snprintf(cs->idstr, sizeof(cs->idstr), "%d", cs->id);
		str_ncpy(cs->event, event, sizeof(cs->event));
		routev[0] = ua_outbound(g.ua);
		e = sipevent_subscribe(&cs->sub, uag_sipevent_sock(), uri, NULL,
				       from_uri ? from_uri : account_aor(ua_account(g.ua)),
				       event, NULL, expires, ua_cuser(g.ua),
				       routev, routev[0] ? 1 : 0,
				       sub_auth_handler, NULL, true, NULL,
				       sub_notify_handler, sub_close_handler, cs,
				       "%s", hdrs ? hdrs : "");
		if (e) {
			mem_deref(cs);
			return e;
		}
		list_append(&subl, &cs->le, cs);
		id = cs->id;
		return 0;
	});
	return err ? -err : id;
}

void cmesip_unsubscribe(int id)
{
	(void)run(^int(void) {
		struct le *le = list_head(&subl);
		while (le) {
			struct csub *cs = le->data;
			le = le->next;
			if (id == 0 || cs->id == id)
				mem_deref(cs);   /* beendet das Abo (SUBSCRIBE Expires: 0) */
		}
		return 0;
	});
}


/* --- Umleiten, Weitergeben --- */

int cmesip_redirect(const char *call_id, const char *uri)
{
	if (!uri)
		return EINVAL;
	return run(^int(void) {
		struct call *call = find_call(call_id);
		if (!call)
			return ENOENT;
		/* ua_hangupf: meldet CALL_CLOSED und gibt das Gespräch frei (call_hangupf allein tut das nicht) */
		ua_hangupf(call_get_ua(call), call, 302, "Moved Temporarily", "Contact: <%s>\r\nContent-Length: 0\r\n\r\n", uri);
		return 0;
	});
}

int cmesip_transfer(const char *call_id, const char *uri)
{
	if (!uri)
		return EINVAL;
	return run(^int(void) {
		struct call *call = find_call(call_id);
		return call ? call_transfer(call, uri) : ENOENT;
	});
}


/* --- Audiogeräte --- */

static void devlist_print(const struct list *l, char *buf, size_t size)
{
	struct le *le;
	size_t n = 0;
	buf[0] = '\0';
	for (le = list_head(l); le; le = le->next) {
		const struct mediadev *d = le->data;
		int w = re_snprintf(buf + n, size - n, "%s\n", d->name);
		if (w < 0)
			break;
		n += (size_t)w;
	}
}

int cmesip_audio_devices(bool input, char *buf, size_t size)
{
	if (!buf || !size)
		return EINVAL;
	return run(^int(void) {
		struct config_audio *a = &conf_config()->audio;
		if (input) {
			const struct ausrc *s = ausrc_find(baresip_ausrcl(), a->src_mod);
			if (!s)
				return ENOENT;
			devlist_print(&s->dev_list, buf, size);
		}
		else {
			const struct auplay *p = auplay_find(baresip_auplayl(), a->play_mod);
			if (!p)
				return ENOENT;
			devlist_print(&p->dev_list, buf, size);
		}
		return 0;
	});
}

int cmesip_set_audio_devices(const char *input, const char *output)
{
	return run(^int(void) {
		struct config_audio *a = &conf_config()->audio;
		struct le *le;
		if (input)
			str_ncpy(a->src_dev, *input ? input : "default", sizeof(a->src_dev));
		if (output) {
			str_ncpy(a->play_dev, *output ? output : "default", sizeof(a->play_dev));
			str_ncpy(a->alert_dev, a->play_dev, sizeof(a->alert_dev));
		}
		/* laufende Gespräche umschalten */
		for (le = list_head(uag_list()); le; le = le->next) {
			struct le *lc;
			for (lc = list_head(ua_calls(le->data)); lc; lc = lc->next) {
				struct audio *au = call_audio(lc->data);
				if (input)
					(void)audio_set_source(au, a->src_mod, a->src_dev);
				if (output)
					(void)audio_set_player(au, a->play_mod, a->play_dev);
			}
		}
		return 0;
	});
}


/* --- Geräte-ID, Audio-Sitzung --- */

int cmesip_set_instance(const char *uuid)
{
	return run(^int(void) {
		struct config *cfg = conf_config();
		str_ncpy(cfg->sip.uuid, uuid ? uuid : "", sizeof(cfg->sip.uuid));
		return 0;
	});
}

#if TARGET_OS_IPHONE
/* modules/audiounit/sess.c: stoppt (true) bzw. startet (false) alle Audio-Units */
void audiosess_interrupt(bool interrupted);
#endif

int cmesip_audio_session(bool active)
{
#if TARGET_OS_IPHONE
	return run(^int(void) {
		struct le *le;
		audiosess_interrupt(!active);
		if (!active)
			return 0;
		/* Ton neu aufbauen: Startete er vor der Freigabe durch CallKit (Annehmen kommt vor didActivate),
		 * lehnte iOS die Audio-Units ab ('!pri'), und audio_update brach ab – ohne Player, Mikrofon
		 * und Empfang. */
		for (le = list_head(uag_list()); le; le = le->next) {
			struct le *lc;
			for (lc = list_head(ua_calls(le->data)); lc; lc = lc->next) {
				struct audio *au = call_audio(lc->data);
				if (au && stream_sdpmedia(audio_strm(au))) {
					int err = audio_update(au);
					info("cmesip: Audio nach Aktivierung neu gestartet (%m)\n", err);
				}
			}
		}
		return 0;
	});
#else
	(void)active;
	return 0;
#endif
}


/* --- Lokale Aufnahme an/aus --- */

int cmesip_record(const char *call_id, const char *path)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		const struct audio *au;
		struct le *le;
		int err = ENOENT;
		if (!call)
			return ENOENT;
		au = call_audio(call);
		mtx_lock(recl_mtx);
		for (le = list_head(&recl); le; le = le->next) {
			struct rec_ctx *c = le->data;
			if (c->au != au)
				continue;
			mtx_lock(c->mtx);
			rec_close(c);
			err = 0;
			if (path && *path) {
				c->f = fopen(path, "w+b");
				if (!c->f) {
					err = errno;
				}
				else {
					c->bytes = 0;
					c->rlen = c->rpos = 0;
					wav_header(c->f, c->srate ? c->srate : 16000, 0);
					info("cmesip: Aufnahme nach %s (%u Hz)\n", path, c->srate);
				}
			}
			mtx_unlock(c->mtx);
			break;
		}
		mtx_unlock(recl_mtx);
		return err;
	});
}


bool cmesip_call_video(const char *call_id)
{
	return 1 == run(^int(void) {
		struct call *call = find_call(call_id);
		return call && call_video(call) && call_has_video(call) ? 1 : 0;
	});
}


int cmesip_set_video_device(const char *dev)
{
	return run(^int(void) {
		struct config *cfg = conf_config();
		str_ncpy(cfg->video.src_mod, "cmecam", sizeof(cfg->video.src_mod));
		str_ncpy(cfg->video.src_dev, dev ? dev : "", sizeof(cfg->video.src_dev));
		return 0;
	});
}


int cmesip_set_video(const char *call_id, bool on)
{
	return run(^int(void) {
		struct call *call = find_call(call_id);
		if (!call)
			return ENOENT;
		if (!call_video(call))
			return ENOTSUP;   /* Gespräch ohne Video-Stream (als Audio-Anruf aufgebaut) */
		return call_set_video_dir(call, on ? SDP_SENDRECV : SDP_INACTIVE);
	});
}
