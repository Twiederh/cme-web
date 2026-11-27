/*
 * cmesip.c – baresip für CME Phone (siehe cmesip.h).
 *
 * Ein Thread führt libre/baresip (re_main). Aufrufe aus anderen Threads sperren die Ereignisschleife
 * mit re_thread_enter()/re_thread_leave().
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
enum { MQ_STOP = 1 };

static void mqueue_handler(int id, void *data, void *arg)
{
	(void)data; (void)arg;
	if (id == MQ_STOP)
		re_cancel();
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


#define ENTER() do { if (!cmesip_running()) return ESRCH; re_thread_enter(); } while (0)
#define LEAVE() re_thread_leave()


static struct call *find_call(const char *call_id)
{
	return call_id ? uag_call_find(call_id) : NULL;
}


int cmesip_set_account(const char *aor, const char *uri_params, const char *hdr_params)
{
	int err;

	ENTER();
	if (g.ua) {
		mem_deref(g.ua);
		g.ua = NULL;
	}
	err = sip_contact_set_params(uri_params, hdr_params);
	if (!err)
		err = ua_alloc(&g.ua, aor);
	if (!err && account_regint(ua_account(g.ua)) > 0)
		err = ua_register(g.ua);
	LEAVE();
	return err;
}


int cmesip_register(void)
{
	int err;
	ENTER();
	err = g.ua ? ua_register(g.ua) : ENOENT;
	LEAVE();
	return err;
}


int cmesip_unregister(void)
{
	ENTER();
	if (g.ua)
		ua_unregister(g.ua);
	LEAVE();
	return 0;
}


void cmesip_remove_account(void)
{
	if (!cmesip_running())
		return;
	re_thread_enter();
	if (g.ua) {
		ua_unregister(g.ua);
		mem_deref(g.ua);
		g.ua = NULL;
	}
	re_thread_leave();
}


int cmesip_network_changed(void)
{
	int err;
	ENTER();
	net_dns_refresh(baresip_network());
	err = uag_reset_transp(true, true);
	LEAVE();
	return err;
}


/* Replaces-Wert für den URI-Header escapen (;=@ und Leerzeichen) */
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
	struct call *call = NULL;
	char *target = NULL;
	int err;

	if (!uri)
		return EINVAL;

	ENTER();
	if (!g.ua) {
		LEAVE();
		return ENOENT;
	}
	if (replaces && *replaces)
		err = re_sdprintf(&target, "<%s?Replaces=%H>", uri, print_escaped, replaces);
	else
		err = re_sdprintf(&target, "%s", uri);
	if (!err)
		err = ua_connect(g.ua, &call, from_uri, target, VIDMODE_OFF);
	if (!err && call && cid_out && cid_size)
		str_ncpy(cid_out, call_id(call), cid_size);
	mem_deref(target);
	LEAVE();
	return err;
}


int cmesip_answer(const char *call_id)
{
	int err;
	ENTER();
	struct call *call = find_call(call_id);
	err = call ? ua_answer(call_get_ua(call), call, VIDMODE_OFF) : ENOENT;
	LEAVE();
	return err;
}


int cmesip_hangup(const char *call_id, unsigned scode)
{
	ENTER();
	struct call *call = find_call(call_id);
	if (call)
		ua_hangup(call_get_ua(call), call, (uint16_t)scode, NULL);
	LEAVE();
	return call ? 0 : ENOENT;
}


int cmesip_hold(const char *call_id, bool hold, const char *hdrs)
{
	int err;
	ENTER();
	struct call *call = find_call(call_id);
	if (!call) {
		LEAVE();
		return ENOENT;
	}
	err = call_set_reinvite_hdrs(call, hdrs);
	if (!err)
		err = call_hold(call, hold);
	(void)call_set_reinvite_hdrs(call, NULL);
	LEAVE();
	return err;
}


int cmesip_mute(const char *call_id, bool mute)
{
	ENTER();
	struct call *call = find_call(call_id);
	if (call)
		audio_mute(call_audio(call), mute);
	LEAVE();
	return call ? 0 : ENOENT;
}


int cmesip_send_dtmf(const char *call_id, char key)
{
	int err;
	ENTER();
	struct call *call = find_call(call_id);
	err = call ? call_send_digit(call, key) : ENOENT;
	if (!err)
		err = call_send_digit(call, KEYCODE_REL);
	LEAVE();
	return err;
}


int cmesip_call_info(const char *call_id, char *buf, size_t size)
{
	ENTER();
	struct call *call = find_call(call_id);
	if (!call || !buf || !size) {
		LEAVE();
		return ENOENT;
	}
	const struct aucodec *ac = audio_codec(call_audio(call), true);
	struct stream *s = audio_strm(call_audio(call));
	re_snprintf(buf, size, "codec=%s/%u menc=%s",
		    ac ? ac->name : "-", ac ? ac->srate : 0,
		    s && stream_is_secure(s) ? "srtp" : "none");
	LEAVE();
	return 0;
}


int cmesip_call_count(void)
{
	int n = 0;
	struct le *le;
	if (!cmesip_running())
		return 0;
	re_thread_enter();
	for (le = list_head(uag_list()); le; le = le->next)
		n += (int)list_count(ua_calls(le->data));
	re_thread_leave();
	return n;
}
