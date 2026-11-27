/*
 * cmesip.h – schlanke C-Schnittstelle zwischen CME Phone (Swift) und baresip.
 *
 * baresip läuft in einem eigenen Thread (re_main). Alle Funktionen dürfen aus jedem Thread aufgerufen
 * werden, außer aus dem Ereignis-Rückruf selbst (der läuft im baresip-Thread – dort nur Daten kopieren
 * und an die App weiterreichen, z.B. per DispatchQueue.main.async).
 *
 * Gespräche werden über ihre SIP-Call-ID angesprochen.
 */
#ifndef CMESIP_H
#define CMESIP_H

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum cmesip_event {
	CMESIP_REGISTER_OK = 1,     /* text: Statuszeile */
	CMESIP_REGISTER_FAIL,       /* text: Fehler/Statuszeile */
	CMESIP_UNREGISTERED,
	CMESIP_CALL_INCOMING,       /* peer: Gegenstelle, text: Anzeigename */
	CMESIP_CALL_OUTGOING,
	CMESIP_CALL_RINGING,
	CMESIP_CALL_PROGRESS,       /* frühe Medien (183) */
	CMESIP_CALL_ESTABLISHED,
	CMESIP_CALL_CLOSED,         /* text: Grund */
	CMESIP_CALL_HOLD,           /* Gegenstelle hält */
	CMESIP_CALL_RESUME,         /* Gegenstelle setzt fort */
	CMESIP_CALL_MENC,           /* text: Verschlüsselung, z.B. "srtp" */
	CMESIP_CALL_DTMF,           /* text: Taste */
	CMESIP_LOG,                 /* text: Logzeile */
	CMESIP_TRACE                /* text: SIP-Nachricht; peer: "tx"/"rx" + Gegenstelle */
};

/* ev: enum cmesip_event; call_id/peer/text können NULL sein */
typedef void (cmesip_event_h)(int ev, const char *call_id, const char *peer,
			      const char *text, void *arg);

/* Startet baresip. config: Inhalt einer baresip-Konfiguration (Zeilen "name wert"),
 * modules: durch Komma getrennte Modulnamen (z.B. "g711,libg722,opus,srtp,audiounit"). */
int  cmesip_start(const char *software, const char *config, const char *modules,
		  cmesip_event_h *eh, void *arg);
void cmesip_stop(void);
bool cmesip_running(void);

/* Konto setzen (ersetzt ein vorhandenes). aor im baresip-Format, z.B.
 * "<sip:+49...@cme.local>;auth_user=...;auth_pass=...;outbound=\"sip:gw:5062;transport=tcp\";regint=600"
 * uri_params: hängt an jeden Contact in <...> (z.B. "pn-provider=apns;pn-prid=...;pn-param=...")
 * hdr_params: hängt hinter jeden Contact (z.B. "+u.sip!devicename.ccm.cisco.com=\"SIPMacMini\"")
 * Mit regint > 0 meldet sich das Konto gleich an. */
int  cmesip_set_account(const char *aor, const char *uri_params, const char *hdr_params);
int  cmesip_register(void);
int  cmesip_unregister(void);
void cmesip_remove_account(void);
/* Netz gewechselt (WLAN/Mobilfunk, VPN): Transporte neu aufbauen und neu anmelden */
int  cmesip_network_changed(void);

/* Anruf. from_uri: eigener From (NULL = Konto), replaces: "call-id;to-tag=..;from-tag=.." oder NULL.
 * call_id: Puffer für die Call-ID des neuen Gesprächs. */
int  cmesip_dial(const char *uri, const char *from_uri, const char *replaces,
		 char *call_id, size_t call_id_size);
int  cmesip_answer(const char *call_id);
int  cmesip_hangup(const char *call_id, unsigned scode);
/* Halten/Fortsetzen; hdrs: zusätzliche Header für das re-INVITE inkl. CRLF (z.B. Call-Info) oder NULL */
int  cmesip_hold(const char *call_id, bool hold, const char *hdrs);
int  cmesip_mute(const char *call_id, bool mute);
int  cmesip_send_dtmf(const char *call_id, char key);
/* Kurzinfo für die Anzeige: "codec=G722/16000 menc=srtp" */
int  cmesip_call_info(const char *call_id, char *buf, size_t size);
/* Anzahl laufender Gespräche */
int  cmesip_call_count(void);
/* SIP-Nachrichten als CMESIP_TRACE-Ereignisse melden (für das Log der App) */
int  cmesip_set_trace(bool on);

#ifdef __cplusplus
}
#endif

#endif
