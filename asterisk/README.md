# cme-voicemail – Asterisk als Voicemail für Cisco CME

Der Container nimmt nur Anrufe vom CME-Router an: umgeleitete Anrufe landen im Postfach
der umleitenden Rufnummer, Anrufe auf die Voicemail-Nummer fragen das eigene Postfach ab.
Neue Nachrichten meldet Asterisk per SIP NOTIFY (unsolicited MWI) an den Router, die
Telefone zeigen dann Lampe bzw. Briefumschlag. Ansagen sind deutsch.

Eingerichtet und bedient wird alles im Tab **Voicemail** von cme-web.

## Variablen im Portainer-Stack

| Variable | Beispiel | Bedeutung |
|---|---|---|
| `VM_HOST_IP` | `192.168.11.20` | IP des Docker-Hosts; dorthin schickt der Router die Anrufe |
| `VM_AMI_SECRET` | (lang, zufällig) | Passwort für die Verbindung cme-web → Asterisk |
| `CME_HOST` | `192.168.11.4` | ist schon gesetzt; nur von dort nimmt Asterisk Anrufe an |

Auf dem Docker-Host müssen **UDP 5060** und **UDP 10000–10049** frei sein.

## Was cme-web am Router einrichtet

```
sip-ua
 mwi-server ipv4:<VM_HOST_IP> expires 3600 port 5060 transport udp unsolicited
voice register global
 voicemail 8000
dial-peer voice 8000 voip
 description Voicemail (Asterisk)
 destination-pattern 8000
 session protocol sipv2
 session target ipv4:<VM_HOST_IP>:5060
 dtmf-relay rtp-nte
 codec g711alaw
voice register dn <n>          ! je Postfach
 mwi
 call-forward b2bua noan 8000 timeout 20
 call-forward b2bua busy 8000
```

Am Router muss außerdem `allow-connections sip to sip` unter `voice service voip` stehen.

## Dateien

- `etc/*.conf` – Vorlagen, das Startskript setzt IPs und Passwort ein
- Volume `vm-config` (`/etc/asterisk/cme`) – Postfächer usw., schreibt cme-web
- Volume `vm-spool` (`/var/spool/asterisk/voicemail`) – die Nachrichten (WAV)

Fehlersuche: `docker exec -it cme-voicemail asterisk -rvvv`, dort z. B.
`pjsip set logger on` oder `voicemail show users`.
