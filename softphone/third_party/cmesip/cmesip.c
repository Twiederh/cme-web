/*
 * cmesip.c – baresip für CME Phone (siehe cmesip.h).
 *
 * Ein Thread führt libre/baresip (re_main). Befehle aus anderen Threads laufen über eine mqueue im
 * baresip-Thread (run()); der Aufrufer wartet auf das Ergebnis. Mit re_thread_enter() hielte der Aufrufer
 * die Ereignisschleife fest – das Starten des Tons beim Annehmen wartet aber auf genau diese Schleife.
 */
#include <pthread.h>
#include <stdlib.h>
#include <semaphore.h>
#include <string.h>
#include <stdarg.h>
#include <re.h>
#include <baresip.h>
#include "cmesip.h"

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
		int err = ua ? ua_accept(ua, msg) : ENOENT;
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
	err = ua_init(g.software, true, true, false);
	if (err)
		goto out;
	err = load_modules(g.modules);
	if (err)
		goto out;
	err = bevent_register(event_handler, NULL);
	if (err)
		goto out;
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


int cmesip_dial(const char *uri, const char *from_uri, const char *replaces,
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
			err = ua_connect(g.ua, &call, from_uri, target, VIDMODE_OFF);
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
		return call ? ua_answer(call_get_ua(call), call, VIDMODE_OFF) : ENOENT;
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
		re_snprintf(buf, size, "codec=%s/%u menc=%s",
			    ac ? ac->name : "-", ac ? ac->srate : 0,
			    s && stream_is_secure(s) ? "srtp" : "none");
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
		call_hangupf(call, 302, "Moved Temporarily", "Contact: <%s>\r\n", uri);
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
