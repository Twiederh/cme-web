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
	CMESIP_TRACE,               /* text: SIP-Nachricht; peer: "tx"/"rx" + Gegenstelle */
	CMESIP_NOTIFY,              /* call_id: Abo-Nummer, peer: Event, text: Body */
	CMESIP_SUB_CLOSED           /* call_id: Abo-Nummer, peer: Event, text: Grund */
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
 * reg_hdr: zusätzlicher Header nur für REGISTER ("Name: Wert") oder NULL
 * Mit regint > 0 meldet sich das Konto gleich an. */
int  cmesip_set_account(const char *aor, const char *uri_params, const char *hdr_params,
			const char *reg_hdr);
int  cmesip_register(void);
int  cmesip_unregister(void);
void cmesip_remove_account(void);
/* Netz gewechselt (WLAN/Mobilfunk, VPN): Transporte neu aufbauen und neu anmelden */
int  cmesip_network_changed(void);

/* Anruf. from_uri: eigener From (NULL = Konto), replaces: "call-id;to-tag=..;from-tag=.." oder NULL.
 * call_id: Puffer für die Call-ID des neuen Gesprächs. */
int  cmesip_dial(const char *uri, const char *from_uri, const char *replaces, bool video,
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

/* Abo (SUBSCRIBE) über das Konto. from_uri: eigener From (NULL = Konto), hdrs: zusätzliche Header inkl. CRLF.
 * NOTIFYs kommen als CMESIP_NOTIFY (200 OK schickt cmesip selbst), das Ende als CMESIP_SUB_CLOSED.
 * Ergebnis: Abo-Nummer > 0 oder -Fehler. */
int  cmesip_subscribe(const char *uri, const char *event, const char *from_uri, unsigned expires,
		      const char *hdrs);
/* Abo beenden (0 = alle) */
void cmesip_unsubscribe(int id);

/* Eingehenden Anruf per 302 umleiten (z.B. an die Voicemail) */
int  cmesip_redirect(const char *call_id, const char *uri);
/* Blind Transfer (REFER) */
int  cmesip_transfer(const char *call_id, const char *uri);

/* Audiogeräte des Audiomoduls (Namen, je Zeile einer) */
int  cmesip_audio_devices(bool input, char *buf, size_t size);
/* Geräte setzen (NULL = unverändert, "" = Systemstandard); gilt auch für laufende Gespräche */
int  cmesip_set_audio_devices(const char *input, const char *output);

/* Feste Geräte-ID: Contact mit +sip.instance="<urn:uuid:...>" (Registrar ersetzt dann die alte Anmeldung).
 * Vor cmesip_set_account aufrufen; NULL/"" = ohne. */
int  cmesip_set_instance(const char *uuid);
/* iOS: Audio-Sitzung aktiv (CallKit didActivate) bzw. inaktiv – startet/stoppt die Audio-Units */
int  cmesip_audio_session(bool active);

/* Lokale Aufnahme des Gesprächs (Mikrofon + Gegenstelle gemischt, WAV mono 16 bit); path NULL = beenden.
 * Endet auch mit dem Gespräch. */
int  cmesip_record(const char *call_id, const char *path);

#ifdef __cplusplus
}
#endif

/* Video (cmevideo.m): Kamera "cmecam", Anzeige "cmedisp", H.264/H.265 über VideoToolbox.
 * Der Handler bekommt jedes Bild als CVPixelBufferRef (nur während des Aufrufs gültig – CFRetain zum Behalten);
 * remote = Bild der Gegenstelle, sonst Vorschau der eigenen Kamera. Aufruf aus beliebigen Threads. */
typedef void (cmesip_video_h)(bool remote, void *pixel_buffer, void *arg);
void cmesip_set_video_handler(cmesip_video_h *h, void *arg);
/* Kameras: Zeilen "uniqueID<TAB>Name\n" */
int  cmesip_video_devices(char *buf, size_t size);
/* Kamera für neue Gespräche (uniqueID, leer = Standard/Frontkamera) */
int  cmesip_set_video_device(const char *dev);
/* Video im Gespräch an/aus (re-INVITE); ENOTSUP, wenn das Gespräch ohne Video aufgebaut wurde */
int  cmesip_set_video(const char *call_id, bool on);
/* Gespräch mit Video? */
bool cmesip_call_video(const char *call_id);
/* macOS: Auswahl für die Bildschirmfreigabe (SCContentFilter aus SCContentSharingPicker, als
 * Unmanaged-Zeiger; cmesip hält ihn selbst fest). NULL = keine. */
void cmesip_screen_filter(void *filter);
/* Im Gespräch Bildschirm (on, Auswahl vorher mit cmesip_screen_filter) statt Kamera senden bzw. zurück zur
 * Kamera. Braucht ein Gespräch mit Video (ENOTSUP sonst). */
int  cmesip_set_screen(const char *call_id, bool on);

#endif
