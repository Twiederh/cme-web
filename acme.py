"""Let's-Encrypt-Zertifikat für das SIP-Gateway (Kamailio: TLS 5061 unter SIP_GW_NAME und den SIP-Domains).

Ausgestellt per DNS-Challenge mit lego (im Image unter /usr/local/bin/lego), wie im Nginx Proxy Manager:
DNS-Anbieter wählen und die Zugangsdaten als KEY=Wert-Zeilen eintragen (Namen wie in der lego-Doku,
https://go-acme.github.io/lego/dns/). Ablage im Volume softphone-data:
  /data/push/acme/              Konto und Zertifikate von lego
  /data/push/tls/cert.pem       Zertifikat mit Kette  } liest Kamailio (TLS_CERT_DIR), übernimmt Änderungen
  /data/push/tls/key.pem        privater Schlüssel    } binnen 20 s ohne Neustart (tls.reload)
Einstellungen (mit den Zugangsdaten) in cme-acme.json (0600). Verlängert wird automatisch 30 Tage vor Ablauf.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import jsonify, request

LEGO = os.environ.get("LEGO_BIN", "/usr/local/bin/lego")
MASK = "********"
RENEW_DAYS = 30

# Häufige Anbieter: lego-Name -> (Anzeige, Variablen). Andere: Name aus der lego-Doku und eigene Variablen.
PROVIDERS = {
    "allinkl": ("All-Inkl (KAS)", ["ALL_INKL_LOGIN", "ALL_INKL_PASSWORD"]),
    "cloudflare": ("Cloudflare", ["CF_DNS_API_TOKEN"]),
    "hetzner": ("Hetzner", ["HETZNER_API_KEY"]),
    "ionos": ("IONOS", ["IONOS_API_KEY"]),
    "netcup": ("netcup", ["NETCUP_CUSTOMER_NUMBER", "NETCUP_API_KEY", "NETCUP_API_PASSWORD"]),
    "inwx": ("INWX", ["INWX_USERNAME", "INWX_PASSWORD"]),
    "ovh": ("OVH", ["OVH_ENDPOINT", "OVH_APPLICATION_KEY", "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY"]),
    "gandiv5": ("Gandi", ["GANDIV5_PERSONAL_ACCESS_TOKEN"]),
    "desec": ("deSEC", ["DESEC_TOKEN"]),
    "duckdns": ("Duck DNS", ["DUCKDNS_TOKEN"]),
    "digitalocean": ("DigitalOcean", ["DO_AUTH_TOKEN"]),
    "route53": ("Amazon Route 53", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"]),
    "godaddy": ("GoDaddy", ["GODADDY_API_KEY", "GODADDY_API_SECRET"]),
    "porkbun": ("Porkbun", ["PORKBUN_API_KEY", "PORKBUN_SECRET_API_KEY"]),
}

RE_PROVIDER = re.compile(r"^[a-z0-9]{2,30}$")
RE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,60}$")
RE_DOMAIN = re.compile(r"^(\*\.)?(?=.{4,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
RE_RESOLVER = re.compile(r"^[0-9a-fA-F.:\[\]]{3,60}(:[0-9]{1,5})?$")


class AcmeError(Exception):
    pass


def parse_env(text: str) -> dict[str, str]:
    """KEY=Wert-Zeilen (Kommentare mit #); ungültige Namen -> Fehler"""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or not RE_KEY.match(k):
            raise AcmeError(f"Zugangsdaten: „{k[:40]}“ ist keine Zeile der Form NAME=Wert.")
        out[k] = v.strip()
    return out


def merge_env(new: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    """Ausgeblendete Werte (********) aus der Oberfläche behalten den gespeicherten Wert"""
    return {k: (old.get(k, "") if v == MASK else v) for k, v in new.items()}


def check_domains(v) -> list[str]:
    items = v if isinstance(v, list) else re.split(r"[,;\s]+", str(v or ""))
    out = []
    for d in (str(x).strip().lower().rstrip(".") for x in items):
        if not d:
            continue
        if not RE_DOMAIN.match(d):
            raise AcmeError(f"„{d}“ ist kein gültiger Domainname.")
        if d not in out:
            out.append(d)
    if len(out) > 20:
        raise AcmeError("Höchstens 20 Namen in einem Zertifikat.")
    return out


def lego_args(path: str, email: str, provider: str, domains: list[str], resolver: str, staging: bool,
              renew: bool) -> list[str]:
    if not domains or not RE_PROVIDER.match(provider) or not RE_EMAIL.match(email):
        raise AcmeError("Domains, DNS-Anbieter und E-Mail-Adresse für das Konto bei Let's Encrypt angeben.")
    a = [LEGO, "--accept-tos", "--email", email, "--path", path, "--dns", provider, "--key-type", "ec256"]
    if resolver:
        if not RE_RESOLVER.match(resolver):
            raise AcmeError("DNS-Server für die Prüfung: IP oder IP:Port.")
        a += ["--dns.resolvers", resolver]
    if staging:
        a += ["--server", "https://acme-staging-v02.api.letsencrypt.org/directory"]
    for d in domains:
        a += ["--domains", d]
    return a + (["renew", "--days", str(RENEW_DAYS), "--no-random-sleep"] if renew else ["run"])


def cert_info(pem: bytes) -> dict:
    """Namen, Aussteller und Ablauf des ersten Zertifikats in der PEM-Datei"""
    from cryptography import x509
    c = x509.load_pem_x509_certificate(pem)
    try:
        names = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    until = c.not_valid_after_utc.timestamp()
    org = c.issuer.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)
    cn = c.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return {"names": list(names), "until": until, "days": int((until - time.time()) // 86400),
            "issuer": " ".join(x.value for x in org + cn)}


def lego_file(domain: str) -> str:
    """lego legt certificates/<erste Domain>.crt/.key ab ("*" wird zu "_")"""
    return domain.replace("*", "_")


def register(app, demo: bool, cfgdir: Path, data: Path) -> None:
    store = cfgdir / "cme-acme.json"
    acme_dir = data / "acme"
    tls_dir = data / "tls"
    lock = threading.Lock()
    state = {"running": False, "log": "", "time": 0, "ok": None, "action": ""}

    def default_domains() -> list[str]:
        names = [os.environ.get("SIP_GW_NAME", "").strip().lower()]
        return [n for n in names if n]

    def settings() -> dict:
        s = {"domains": default_domains(), "provider": "allinkl", "env": {}, "email": "", "resolver": "1.1.1.1:53",
             "staging": False, "auto": True}
        try:
            s.update(json.loads(store.read_text()))
        except (FileNotFoundError, ValueError):
            pass
        return s

    def save(s: dict) -> None:
        tmp = store.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(store)

    def current() -> dict | None:
        try:
            return cert_info((tls_dir / "cert.pem").read_bytes())
        except Exception:  # noqa: BLE001
            return None

    def install(first: str) -> None:
        d, name = acme_dir / "certificates", lego_file(first)
        crt, key = (d / f"{name}.crt").read_bytes(), (d / f"{name}.key").read_bytes()
        tls_dir.mkdir(parents=True, exist_ok=True)
        for name, body, mode in (("key.pem", key, 0o600), ("cert.pem", crt, 0o644)):
            tmp = tls_dir / f".{name}.tmp"
            tmp.write_bytes(body)
            os.chmod(tmp, mode)
            tmp.replace(tls_dir / name)

    def run(renew: bool) -> None:
        s = settings()
        try:
            if not shutil.which(LEGO) and not Path(LEGO).exists():
                raise AcmeError("lego fehlt im Image.")
            args = lego_args(str(acme_dir), s["email"], s["provider"], s["domains"], s.get("resolver", ""),
                             bool(s.get("staging")), renew)
            have = (acme_dir / "certificates" / f"{lego_file(s['domains'][0])}.crt").exists()
            if renew and not have:
                args = lego_args(str(acme_dir), s["email"], s["provider"], s["domains"], s.get("resolver", ""),
                                 bool(s.get("staging")), False)
            acme_dir.mkdir(parents=True, exist_ok=True)
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TZ", "SSL_CERT_FILE")}
            env.update(s.get("env", {}))
            p = subprocess.run(args, capture_output=True, text=True, timeout=900, env=env)
            log = (p.stdout + p.stderr)[-6000:]
            for v in s.get("env", {}).values():      # Zugangsdaten nie im Protokoll
                if len(v) >= 4:
                    log = log.replace(v, MASK)
            if p.returncode != 0:
                raise AcmeError(log.strip().splitlines()[-1] if log.strip() else f"lego: Fehler {p.returncode}")
            install(s["domains"][0])
            state.update(ok=True, log=log)
            print(f"Zertifikat: {'verlängert' if renew else 'ausgestellt'} für {', '.join(s['domains'])}", flush=True)
        except (AcmeError, OSError, subprocess.TimeoutExpired) as e:
            state.update(ok=False, log=str(e))
            print(f"Zertifikat: {e}", flush=True)
        finally:
            state.update(running=False, time=time.time())

    def start(renew: bool, action: str) -> bool:
        with lock:
            if state["running"]:
                return False
            state.update(running=True, action=action, log="", ok=None)
        threading.Thread(target=run, args=(renew,), daemon=True).start()
        return True

    @app.errorhandler(AcmeError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/api/acme")
    def acme_status():
        s = settings()
        return jsonify(ok=True, providers={k: {"name": v[0], "vars": v[1]} for k, v in PROVIDERS.items()},
                       settings={"domains": s["domains"], "provider": s["provider"], "email": s["email"],
                                 "resolver": s.get("resolver", ""), "staging": bool(s.get("staging")),
                                 "auto": bool(s.get("auto", True)),
                                 "env": "\n".join(f"{k}={MASK if v else ''}" for k, v in s.get("env", {}).items())},
                       suggest=sorted({*default_domains(), os.environ.get("SIP_PUBLIC_DOMAIN", "").strip().lower()} - {""}),
                       cert=current(), state=state, lego=bool(shutil.which(LEGO) or Path(LEGO).exists()),
                       kamailio_dir=str(tls_dir))

    @app.post("/api/acme")
    def acme_save():
        d = request.get_json(force=True) or {}
        provider = str(d.get("provider", "")).strip().lower()
        if not RE_PROVIDER.match(provider):
            raise AcmeError("DNS-Anbieter: Name wie in der lego-Doku (z. B. allinkl, cloudflare).")
        email = str(d.get("email", "")).strip()
        if email and not RE_EMAIL.match(email):
            raise AcmeError("E-Mail-Adresse ungültig.")
        resolver = str(d.get("resolver", "")).strip()
        if resolver and not RE_RESOLVER.match(resolver):
            raise AcmeError("DNS-Server für die Prüfung: IP oder IP:Port.")
        with lock:
            s = settings()
            s.update(domains=check_domains(d.get("domains", "")), provider=provider, email=email, resolver=resolver,
                     staging=bool(d.get("staging")), auto=bool(d.get("auto", True)),
                     env=merge_env(parse_env(d.get("env", "")), s.get("env", {})))
            save(s)
        return jsonify(ok=True)

    @app.post("/api/acme/issue")
    def acme_issue():
        s = settings()
        lego_args(str(acme_dir), s["email"], s["provider"], s["domains"], s.get("resolver", ""), False, False)
        if not start(False, "ausstellen"):
            raise AcmeError("Läuft schon.")
        return jsonify(ok=True)

    def auto_renew() -> None:
        while True:
            time.sleep(6 * 3600)
            s = settings()
            c = current()
            if s.get("auto", True) and s.get("email") and s.get("domains") and c and c["days"] < RENEW_DAYS \
                    and (acme_dir / "certificates").exists():
                start(True, "verlängern")

    if not demo:
        threading.Thread(target=auto_renew, daemon=True).start()
