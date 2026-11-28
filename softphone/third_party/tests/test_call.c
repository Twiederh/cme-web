#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include "cmesip.h"
static char role; static volatile int estab = 0, closed = 0; static char cid[256];
static void eh(int ev, const char *c, const char *peer, const char *text, void *arg) {
  if (ev == CMESIP_LOG) { if (getenv("LOGALL")) printf("%c LOG %s", role, text); return; }
  if (ev == CMESIP_TRACE) {   /* nur Startzeile + interessante Header */
    char buf[4096]; snprintf(buf, sizeof buf, "%s", text ? text : ""); char *l = strtok(buf, "\r\n"); int first = 1;
    while (l) { if (first || !strncmp(l, "Replaces", 8) || !strncmp(l, "Call-Info", 9) || !strncmp(l, "m=audio", 7) || !strncmp(l, "a=crypto", 8) || !strncmp(l, "a=send", 6) || !strncmp(l, "a=recvonly", 10) || !strncmp(l, "Contact", 7))
        printf("%c %s   %s\n", role, first ? peer : "  ", l); first = 0; l = strtok(NULL, "\r\n"); }
    return; }
  printf("%c EV %d cid=%s text=%s\n", role, ev, c ? c : "-", text ? text : "-");
  if (ev == CMESIP_CALL_INCOMING && role == 'B') { snprintf(cid, sizeof cid, "%s", c); estab = 2; }
  if (ev == CMESIP_CALL_ESTABLISHED) estab = 1;
  if (ev == CMESIP_CALL_CLOSED) closed = 1;
}
static volatile int vremote = 0, vlocal = 0;
static void vh(bool remote, void *pb, void *arg) { (void)pb; (void)arg; if (remote) vremote++; else vlocal++; }
static void wait_for(volatile int *v, int ms) { for (int i = 0; i < ms / 50 && !*v; i++) usleep(50000); }
int main(int argc, char **argv) {
  setvbuf(stdout, NULL, _IONBF, 0); alarm(25);
  role = argv[1][0]; const char *port = argv[2];
  char cfg[1024]; snprintf(cfg, sizeof cfg, "sip_listen LAN_IP:%s\naudio_player coreaudio,default\naudio_source coreaudio,default\naudio_alert coreaudio,default\nrtp_ports 30000-30999\nvideo_source cmecam,test\nvideo_display cmedisp,x\nvideo_size 640x480\nvideo_fps 15\nvideo_bitrate 800000\n%s%s%s%s%s%s", port,
      getenv("CERT") ? "sip_certificate " : "", getenv("CERT") ? getenv("CERT") : "", getenv("CERT") ? "\n" : "",
      getenv("CAFILE") ? "sip_cafile " : "", getenv("CAFILE") ? getenv("CAFILE") : "", getenv("CAFILE") ? "\n" : "");
  int e = cmesip_start("CME Phone/test", cfg, "g711,libg722,opus,srtp,coreaudio", eh, NULL); printf("%c start %d\n", role, e); if (e) return 1;
  printf("%c trace %d\n", role, cmesip_set_trace(true));
  cmesip_set_video_handler(vh, NULL);
  char aor[256]; snprintf(aor, sizeof aor, "<sip:%c@LAN_IP:%s>;regint=0;mediaenc=%s;audio_codecs=G722/16000/1,PCMA/8000/1,opus/48000/2",
      role, port, role == 'A' ? "srtp-mand" : "srtp");
  if (getenv("VCODEC")) { size_t l = strlen(aor); snprintf(aor + l, sizeof aor - l, ";video_codecs=%s", getenv("VCODEC")); }
  printf("%c account %d\n", role, cmesip_set_account(aor, NULL, role == 'C' ? "+u.sip!devicename.ccm.cisco.com=\"MOBtest\"" : NULL, NULL));
  if (role == 'B') {
    wait_for(&estab, 8000); if (estab == 2) { printf("B answer %d\n", cmesip_answer(cid)); }
    wait_for(&closed, 12000);
    char info[128] = ""; printf("B info: %s\n", info);
  } else if (role == 'A') {
    char c[256] = ""; printf("A dial %d\n", cmesip_dial(argv[3], NULL, NULL, getenv("VCODEC") != NULL, c, sizeof c)); wait_for(&estab, 6000);
    char info[128] = ""; cmesip_call_info(c, info, sizeof info); printf("A info: %s\n", info);
    if (getenv("RECFILE")) printf("A record %d\n", cmesip_record(c, getenv("RECFILE")));
    if (getenv("VCODEC")) {   /* Bildschirm ohne Auswahl: ENODEV, zurück zur Kamera: 0 */
      printf("A screen on (ohne Auswahl) %d\n", cmesip_set_screen(c, true));
      printf("A screen off %d\n", cmesip_set_screen(c, false));
    }
    sleep(1); cmesip_hold(c, true, "Call-Info: <urn:x-cisco-remotecc:hold>\r\n"); sleep(1);
    cmesip_hold(c, false, "Call-Info: <urn:x-cisco-remotecc:resume>\r\n"); sleep(1);
    cmesip_hangup(c, 0); sleep(1);
  } else {
    char c[256] = ""; cmesip_dial(argv[3], "sip:C@LAN_IP", "abc@1.2.3.4;to-tag=t1;from-tag=f2", false, c, sizeof c); sleep(2);
  }
  printf("%c video remote=%d local=%d\n", role, vremote, vlocal);
  cmesip_stop(); printf("%c stopped\n", role); return 0;
}
