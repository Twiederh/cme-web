"""
auth.py - Anmeldung fuer cme_web

Unterstuetzt
  * OIDC / OAuth2 mit beliebig vielen Anbietern gleichzeitig (z.B. Authentik und Pocket-ID)
  * einen lokalen Benutzer (CME_WEB_USER / CME_WEB_PASS) als Notzugang

Konfiguration ueber Umgebungsvariablen:

  OIDC_PROVIDERS=authentik,pocketid            Liste der Anbieter (Namen frei waehlbar)
  OIDC_<NAME>_ISSUER=https://...               Issuer-URL (ohne /.well-known/...)
  OIDC_<NAME>_CLIENT_ID=...
  OIDC_<NAME>_CLIENT_SECRET=...                leer lassen bei "public client" (dann nur PKCE)
  OIDC_<NAME>_LABEL=Authentik                  Text auf dem Login-Knopf (optional)
  OIDC_<NAME>_SCOPES=openid email profile      Scopes (optional, z.B. "... groups" fuer Pocket-ID)

  OIDC_ALLOWED_GROUPS=cme-admins               Nur Mitglieder dieser Gruppen (optional, kommagetrennt)
  OIDC_ALLOWED_USERS=thomas@example.de         Nur diese Benutzer/E-Mails (optional, kommagetrennt)

  CME_PUBLIC_URL=https://cme.example.de        Oeffentliche URL (fuer die Redirect-URI hinter Traefik)
  CME_SECRET_KEY=...                           Schluessel fuer die Session-Cookies (sonst zufaellig)
  CME_SESSION_HOURS=12                         Wie lange eine Anmeldung gilt
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, redirect, request, send_from_directory, session

# /ipphone/: XML-Dienste fuer die Telefone; nur ueber den eigenen Telefon-Port erreichbar (vvm.py prueft das)
OPEN_PATHS = ("/login", "/healthz", "/api/auth/info", "/auth/", "/ipphone/")


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def load_providers() -> list[dict]:
    providers = []
    for name in _split(os.environ.get("OIDC_PROVIDERS", "")):
        key = name.upper().replace("-", "_")
        issuer = os.environ.get(f"OIDC_{key}_ISSUER", "").strip().rstrip("/")
        client_id = os.environ.get(f"OIDC_{key}_CLIENT_ID", "").strip()
        if not issuer or not client_id:
            print(f"Warnung: OIDC-Anbieter '{name}' unvollstaendig (ISSUER und CLIENT_ID noetig), "
                  "wird uebersprungen.", flush=True)
            continue
        metadata = issuer if "/.well-known/" in issuer else issuer + "/.well-known/openid-configuration"
        providers.append({
            "name": name.lower(),
            "label": os.environ.get(f"OIDC_{key}_LABEL", "").strip() or name.capitalize(),
            "metadata_url": metadata,
            "client_id": client_id,
            "client_secret": os.environ.get(f"OIDC_{key}_CLIENT_SECRET", "").strip(),
            "scopes": os.environ.get(f"OIDC_{key}_SCOPES", "").strip() or "openid email profile",
        })
    return providers


def setup_auth(app: Flask, base_dir: Path, web_user: str = "", web_pass: str = "") -> dict:
    """Richtet die Anmeldung ein. Gibt eine kurze Beschreibung der aktiven Verfahren zurueck."""
    from werkzeug.middleware.proxy_fix import ProxyFix

    providers = load_providers()
    allowed_groups = {g.lower() for g in _split(os.environ.get("OIDC_ALLOWED_GROUPS", ""))}
    allowed_users = {u.lower() for u in _split(os.environ.get("OIDC_ALLOWED_USERS", ""))}
    public_url = os.environ.get("CME_PUBLIC_URL", "").strip().rstrip("/")
    enabled = bool(providers or web_user)

    secret_key = os.environ.get("CME_SECRET_KEY", "").strip()
    if not secret_key:
        secret_key = secrets.token_hex(32)
        if enabled:
            print("Hinweis: CME_SECRET_KEY nicht gesetzt - nach einem Neustart muss man sich "
                  "neu anmelden.", flush=True)
    app.secret_key = secret_key
    app.config.update(
        SESSION_COOKIE_NAME="cme_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=public_url.startswith("https://")
        or os.environ.get("CME_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=float(os.environ.get("CME_SESSION_HOURS", "12"))),
    )
    # Hinter Traefik: Schema und Host aus X-Forwarded-* uebernehmen
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    oauth = None
    if providers:
        from authlib.integrations.flask_client import OAuth
        oauth = OAuth(app)
        for p in providers:
            kwargs = {"scope": p["scopes"], "code_challenge_method": "S256"}
            if not p["client_secret"]:
                kwargs["token_endpoint_auth_method"] = "none"
            oauth.register(
                name=p["name"],
                client_id=p["client_id"],
                client_secret=p["client_secret"] or None,
                server_metadata_url=p["metadata_url"],
                client_kwargs=kwargs,
            )

    def current_user() -> dict | None:
        return session.get("user")

    def basic_ok() -> bool:
        a = request.authorization
        return bool(web_user and a and secrets.compare_digest(a.username or "", web_user)
                    and secrets.compare_digest(a.password or "", web_pass))

    def is_allowed(info: dict) -> bool:
        if not allowed_groups and not allowed_users:
            return True
        groups = info.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        names = {str(info.get(k, "")).lower() for k in ("email", "preferred_username", "sub", "name")}
        return bool(allowed_groups & {str(g).lower() for g in groups}) or bool(allowed_users & names)

    def safe_next(target: str | None) -> str:
        # nur relative Pfade innerhalb der App zulassen
        if not target or not target.startswith("/") or target.startswith("//"):
            return "/"
        return target

    @app.before_request
    def require_login():
        if not enabled:
            return None
        path = request.path
        if path.startswith(OPEN_PATHS):
            return None
        if current_user() or basic_ok():
            # Schutz gegen CSRF: aendernde API-Aufrufe nur als JSON (Browser senden das
            # von fremden Seiten nicht ohne CORS-Freigabe).
            # Datei-Uploads (multipart) tragen stattdessen den Kopf X-CME-Request, den fremde
            # Seiten ebenfalls nicht ohne CORS-Freigabe setzen koennen.
            if request.method in ("POST", "PUT", "DELETE") and path.startswith("/api/") \
                    and not request.is_json and request.headers.get("X-CME-Request") != "1":
                return jsonify(ok=False, error="Anfrage muss JSON sein"), 415
            return None
        if path.startswith("/api/"):
            return jsonify(ok=False, error="Nicht angemeldet", login="/login"), 401
        return redirect("/login?next=" + quote(request.full_path.rstrip("?")))

    @app.get("/login")
    def login_page():
        if not enabled:
            return redirect("/")
        return send_from_directory(base_dir / "static", "login.html")

    @app.get("/api/auth/info")
    def auth_info():
        return jsonify(enabled=enabled, local=bool(web_user),
                       providers=[{"name": p["name"], "label": p["label"]} for p in providers],
                       user=current_user())

    @app.get("/api/me")
    def me():
        return jsonify(enabled=enabled, user=current_user())

    @app.post("/auth/local")
    def local_login():
        data = request.get_json(silent=True) or {}
        if web_user and secrets.compare_digest(str(data.get("username", "")), web_user) \
                and secrets.compare_digest(str(data.get("password", "")), web_pass):
            session.clear()
            session.permanent = True
            session["user"] = {"name": web_user, "email": "", "provider": "lokal", "ts": int(time.time())}
            return jsonify(ok=True, next=safe_next(data.get("next")))
        time.sleep(1.0)   # Raten erschweren
        return jsonify(ok=False, error="Benutzername oder Passwort falsch"), 401

    def redirect_uri(name: str) -> str:
        if public_url:
            return f"{public_url}/auth/callback/{name}"
        return request.host_url.rstrip("/") + f"/auth/callback/{name}"

    @app.get("/auth/login/<name>")
    def oidc_login(name):
        if not oauth or not any(p["name"] == name for p in providers):
            return redirect("/login?error=" + quote("Unbekannter Anbieter"))
        session["next"] = safe_next(request.args.get("next"))
        try:
            return oauth.create_client(name).authorize_redirect(redirect_uri(name))
        except Exception as e:
            app.logger.exception("OIDC-Start fehlgeschlagen")
            return redirect("/login?error=" + quote(f"{name} nicht erreichbar: {type(e).__name__}"))

    @app.get("/auth/callback/<name>")
    def oidc_callback(name):
        if not oauth or not any(p["name"] == name for p in providers):
            return redirect("/login?error=" + quote("Unbekannter Anbieter"))
        client = oauth.create_client(name)
        try:
            token = client.authorize_access_token()
            info = dict(token.get("userinfo") or {})
            # Gruppen stehen bei manchen Anbietern nur im Userinfo-Endpunkt
            if not info or (allowed_groups and "groups" not in info):
                try:
                    info.update(client.userinfo(token=token))
                except Exception:
                    pass
        except Exception as e:
            app.logger.exception("OIDC-Anmeldung fehlgeschlagen")
            msg = getattr(e, "description", None) or str(e) or type(e).__name__
            return redirect("/login?error=" + quote(f"Anmeldung bei {name} fehlgeschlagen: {msg}"))

        if not is_allowed(info):
            who = info.get("email") or info.get("preferred_username") or info.get("sub")
            return redirect("/login?error=" + quote(f"{who} hat keinen Zugriff auf diese Seite."))

        nxt = session.pop("next", "/")
        session.clear()
        session.permanent = True
        session["user"] = {
            "name": info.get("name") or info.get("preferred_username") or info.get("email") or info.get("sub"),
            "email": info.get("email", ""),
            "provider": next(p["label"] for p in providers if p["name"] == name),
            "ts": int(time.time()),
        }
        return redirect(safe_next(nxt))

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect("/login" if enabled else "/")

    parts = [p["label"] for p in providers] + (["lokaler Benutzer"] if web_user else [])
    if providers:
        base = public_url or "http(s)://<host>"
        print("OIDC Redirect-URIs: " + ", ".join(f"{base}/auth/callback/{p['name']}"
                                                  for p in providers), flush=True)
    return {"enabled": enabled, "methods": parts}
