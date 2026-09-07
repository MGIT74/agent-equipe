#!/usr/bin/env python3
"""
Hermes Team App — Mini-application web multi-employés.
Proxy authentifié devant le WebUI Hermes (http://127.0.0.1:8787).

Chaque employé se connecte avec son compte et discute automatiquement
avec SON profil Hermes (mémoire / skills / historique isolés).

Architecture:
  Navigateur employé ──> ce serveur (port 8890) ──> WebUI Hermes (8787)
                        (auth par employé,         (routage par profil
                         injection cookie           via cookie hermes_profile)
                         hermes_profile)

Stockage:
  team_users.json   — comptes employés {username, password_hash, profile, display_name}
  team_sessions/    — token -> {username, profile, expires} (sessions de connexion)
"""
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Configuration ─────────────────────────────────────────────────────────────
UPSTREAM_HOST = os.getenv("TEAM_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("TEAM_UPSTREAM_PORT", "8787"))
PORT = int(os.getenv("TEAM_APP_PORT", "8890"))
HOST = os.getenv("TEAM_APP_HOST", "0.0.0.0")
SESSION_TTL = 86400 * 7  # 7 jours

BASE_DIR = Path(__file__).resolve().parent
# En Docker, les données persistantes vont dans /data (volume) ;
# en local, elles restent à côté des fichiers de l'application.
# Ordre de priorité : TEAM_DATA_DIR explicite > /data existant > dossier de l'app.
def _resolve_data_dir() -> Path:
    explicit = os.getenv("TEAM_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit)
    docker_data = Path("/data")
    if docker_data.is_dir():
        return docker_data
    return BASE_DIR

DATA_DIR = _resolve_data_dir()
USERS_FILE = DATA_DIR / "team_users.json"
LOGIN_SESSIONS_FILE = DATA_DIR / "team_sessions.json"
SALT_FILE = DATA_DIR / ".pbkdf2_salt"
PROFILE_COOKIE_NAME = "hermes_profile"
COOKIE_NAME = "team_session"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [team-app] %(levelname)s %(message)s")
logger = logging.getLogger("team-app")

# ── Gestion des utilisateurs ──────────────────────────────────────────────────
_USERS_LOCK = threading.Lock()
_LOGIN_LOCK = threading.Lock()


def _hash_password(pw: str) -> str:
    if not SALT_FILE.exists():
        SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SALT_FILE.write_text(secrets.token_hex(16))
        try:
            os.chmod(SALT_FILE, 0o600)
        except Exception:
            pass
    salt = SALT_FILE.read_text().strip().encode()
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 300_000).hex()


def _load_users() -> dict:
    with _USERS_LOCK:
        if not USERS_FILE.exists():
            return {}
        try:
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error("Failed to read users file: %s", e)
            return {}


def _save_users(users: dict) -> None:
    with _USERS_LOCK:
        tmp = USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(USERS_FILE)
        try:
            os.chmod(USERS_FILE, 0o600)
        except Exception:
            pass


def verify_login(username: str, password: str):
    users = _load_users()
    u = users.get(username)
    if not u:
        # Hash quand même pour uniformiser le temps de réponse
        _hash_password(password)
        return None
    if hmac.compare_digest(_hash_password(password), u["password_hash"]):
        return u
    return None


# ── Sessions de connexion (token -> infos employé) ───────────────────────────
def _load_login_sessions() -> dict:
    if not LOGIN_SESSIONS_FILE.exists():
        return {}
    try:
        data = json.loads(LOGIN_SESSIONS_FILE.read_text(encoding="utf-8"))
        now = time.time()
        if isinstance(data, dict):
            return {t: v for t, v in data.items() if v.get("expires", 0) > now}
    except Exception:
        pass
    return {}


def _save_login_sessions(sessions: dict) -> None:
    tmp = LOGIN_SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=1), encoding="utf-8")
    tmp.replace(LOGIN_SESSIONS_FILE)
    try:
        os.chmod(LOGIN_SESSIONS_FILE, 0o600)
    except Exception:
        pass


def create_login_session(username: str, profile: str) -> str:
    with _LOGIN_LOCK:
        sessions = _load_login_sessions()
        token = secrets.token_urlsafe(32)
        sessions[token] = {
            "username": username,
            "profile": profile,
            "expires": time.time() + SESSION_TTL,
        }
        _save_login_sessions(sessions)
        return token


def validate_login_session(token: str):
    with _LOGIN_LOCK:
        sessions = _load_login_sessions()
        info = sessions.get(token)
        if info and info["expires"] > time.time():
            return info
        if info:  # expirée : purge
            sessions.pop(token, None)
            _save_login_sessions(sessions)
    return None


def revoke_login_session(token: str) -> None:
    with _LOGIN_LOCK:
        sessions = _load_login_sessions()
        if token in sessions:
            sessions.pop(token, None)
            _save_login_sessions(sessions)


# ── Client HTTP vers le WebUI Hermes ──────────────────────────────────────────
_UPSTREAM_LOCK = threading.Lock()
_UPSTREAM_COOKIE: str | None = None  # cookie hermes_session du proxy


def _upstream_login() -> str | None:
    """Authentifie le proxy auprès du WebUI (une seule fois). Retourne le cookie."""
    global _UPSTREAM_COOKIE
    password = os.getenv("HERMES_WEBUI_PASSWORD", "").strip()
    if not password:
        # Auth désactivée côté WebUI → pas de cookie nécessaire
        return ""
    body = json.dumps({"password": password}).encode()
    req = urllib.request.Request(
        f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/api/auth/login",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            set_cookie = resp.headers.get("Set-Cookie", "")
            # Extrait hermes_session=<token>
            m = re.search(r"hermes_session=([^;]+)", set_cookie)
            if m:
                with _UPSTREAM_LOCK:
                    _UPSTREAM_COOKIE = m.group(1)
                logger.info("Proxy authenticated to upstream WebUI")
                return m.group(1)
            logger.warning("Upstream login 200 but no session cookie found")
            return ""
    except Exception as e:
        logger.error("Upstream login failed: %s", e)
        return None


def _upstream_cookies(profile: str) -> str:
    """Construit l'en-tête Cookie combinant session proxy + profil employé."""
    global _UPSTREAM_COOKIE
    parts = []
    with _UPSTREAM_LOCK:
        cookie = _UPSTREAM_COOKIE
    if cookie:
        parts.append(f"hermes_session={cookie}")
    if profile:
        parts.append(f"{PROFILE_COOKIE_NAME}={profile}")
    return "; ".join(parts)


def _maybe_relogin_on_401(status: int) -> bool:
    """Si l'amont répond 401, retente un login et retourne True si relogué."""
    global _UPSTREAM_COOKIE
    if status != 401:
        return False
    with _UPSTREAM_LOCK:
        _UPSTREAM_COOKIE = None
    logger.info("Upstream 401 — re-authenticating proxy")
    return (_upstream_login() not in (None,)) or not os.getenv("HERMES_WEBUI_PASSWORD", "").strip()


def upstream_request(path: str, method: str = "GET", body: dict | None = None,
                    profile: str = None, raw_body: bytes | None = None,
                    headers: dict | None = None, timeout: int = 60) -> tuple[int, dict, bytes]:
    """Appelle le WebUI Hermes en injectant les cookies (session proxy + profil)."""
    url = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}{path}"
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
    if headers:
        hdrs.update(headers)
    cookies = _upstream_cookies(profile)
    if cookies:
        hdrs["Cookie"] = cookies
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        # Re-tentative unique après re-login si session expirée
        if status == 401 and _maybe_relogin_on_401(status):
            cookies = _upstream_cookies(profile)
            hdrs2 = dict(hdrs)
            if cookies:
                hdrs2["Cookie"] = cookies
            req2 = urllib.request.Request(url, data=data, method=method, headers=hdrs2)
            try:
                with urllib.request.urlopen(req2, timeout=timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e2:
                return e2.code, dict(e2.headers), e2.read()
            except Exception as e2:
                logger.error("Upstream error (retry) %s %s: %s", method, path, e2)
                return 502, {}, b'{"error": "upstream unreachable"}'
        return status, dict(e.headers), e.read()
    except Exception as e:
        logger.error("Upstream error %s %s: %s", method, path, e)
        return 502, {}, b'{"error": "upstream unreachable"}'


def upstream_stream(path: str, profile: str):
    """Ouvre un flux SSE vers le WebUI et retourne la réponse brute."""
    global _UPSTREAM_COOKIE
    url = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}{path}"
    cookies = _upstream_cookies(profile)
    if not cookies:
        # Retente le login si jamais fait (ex: démarrage à froid)
        _upstream_login()
        cookies = _upstream_cookies(profile)
    headers = {"Accept": "text/event-stream"}
    if cookies:
        headers["Cookie"] = cookies
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=None)  # None = pas de timeout SSE
    except urllib.error.HTTPError as e:
        # Session expirée en cours de route : re-login puis nouvelle tentative
        if e.code == 401 and _maybe_relogin_on_401(401):
            cookies = _upstream_cookies(profile)
            headers2 = {"Accept": "text/event-stream"}
            if cookies:
                headers2["Cookie"] = cookies
            req2 = urllib.request.Request(url, headers=headers2)
            return urllib.request.urlopen(req2, timeout=None)
        raise


# ── Vérification de l'existence d'un profil côté Hermes ──────────────────────
def profile_exists(profile: str) -> bool:
    status, _, body = upstream_request("/api/profiles")
    if status != 200:
        return False
    try:
        data = json.loads(body)
        return any(p.get("name") == profile for p in data.get("profiles", []))
    except Exception:
        return False


def ensure_profile(profile: str) -> bool:
    """Crée le profil Hermes s'il n'existe pas (clone config du profil par défaut)."""
    if profile in ("", "default") or profile_exists(profile):
        return True
    status, _, body = upstream_request(
        "/api/profile/create", method="POST",
        body={"name": profile, "clone_config": True},
        profile="default",
    )
    ok = status == 200
    if ok:
        logger.info("Created Hermes profile %r", profile)
    else:
        logger.error("Profile create failed (%s): %s", status, body[:200])
    return ok


# ── Serveur HTTP ─────────────────────────────────────────────────────────────
class TeamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- utilitaires ----------------------------------------------------------
    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    def _json(self, payload: dict, status: int = 200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _session_token(self):
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(cookie_header)
        except http.cookies.CookieError:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _current_employee(self):
        token = self._session_token()
        if not token:
            return None
        info = validate_login_session(token)
        if not info:
            return None
        # Re-vérifie que le compte existe toujours (ex: révoqué entre-temps)
        users = _load_users()
        u = users.get(info["username"])
        if not u:
            return None
        return {"username": info["username"], "profile": u.get("profile", info["profile"]),
                "display_name": u.get("display_name", info["username"])}

    def _set_cookie(self, name: str, value: str, max_age: int, clear: bool = False):
        self.send_header("Set-Cookie", f"{name}={value if not clear else ''}; Path=/; Max-Age={0 if clear else max_age}; HttpOnly; SameSite=Lax")

    # -- pages ----------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            # Pages publiques
            if path == "/" or path == "/index.html":
                self._serve_file("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.html":
                self._serve_file("app.html", "text/html; charset=utf-8")
                return
            if path in ("/style.css", "/app.js", "/favicon.ico"):
                self._serve_file(path.lstrip("/"), self._guess_type(path))
                return
            if path == "/health":
                self._json({"ok": True, "service": "team-app"})
                return

            # Proxy SSE — nécessite une session valide
            if path.startswith("/proxy/"):
                emp = self._current_employee()
                if not emp:
                    self._json({"error": "unauthorized"}, 401)
                    return
                self._handle_sse_proxy(parsed, emp)
                return

            # API authentifiée (proxy vers WebUI avec isolation profil)
            if path.startswith("/api/"):
                emp = self._current_employee()
                if not emp:
                    self._json({"error": "unauthorized"}, 401)
                    return
                if path == "/api/team/me":
                    self._json({"ok": True, **emp})
                    return
                # self.path inclut la query string (?session_id=…) nécessaire au proxy
                self._proxy_api(self.path, emp)
                return

            self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception:
            logger.error("GET %s failed:\n%s", self.path, traceback.format_exc())
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_body()

            # ── Connexion employé ──
            if path == "/api/team/login":
                username = str(body.get("username", "")).strip()
                password = str(body.get("password", ""))
                if not username or not password:
                    self._json({"error": "Identifiant et mot de passe requis"}, 400)
                    return
                user = verify_login(username, password)
                if not user:
                    logger.warning("Login failed for %r", username)
                    self._json({"error": "Identifiants incorrects"}, 401)
                    return
                token = create_login_session(username, user["profile"])
                display = user.get("display_name", username)
                data = json.dumps({
                    "ok": True,
                    "username": username,
                    "display_name": display,
                    "profile": user["profile"],
                    "token": token,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._set_cookie(COOKIE_NAME, token, SESSION_TTL)
                self.end_headers()
                self.wfile.write(data)
                return

            # ── Déconnexion ──
            if path == "/api/team/logout":
                token = self._session_token()
                if token:
                    revoke_login_session(token)
                self.send_response(200)
                self._set_cookie(COOKIE_NAME, "", 0, clear=True)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return

            # ── Inscription publique (si activée) ──
            if path == "/api/team/register":
                self._json({"error": "registration disabled"}, 403)
                return

            # ── API authentifiée ──
            emp = self._current_employee()
            if not emp:
                self._json({"error": "unauthorized"}, 401)
                return

            if path == "/api/team/logout_everywhere":
                # Révoque toutes les sessions de connexion de cet employé
                with _LOGIN_LOCK:
                    sessions = _load_login_sessions()
                    kept = {t: v for t, v in sessions.items() if v.get("username") != emp["username"]}
                    _save_login_sessions(kept)
                self._json({"ok": True})
                return

            self._proxy_api(path, emp, body=body)
        except BrokenPipeError:
            pass
        except Exception:
            logger.error("POST %s failed:\n%s", self.path, traceback.format_exc())
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:
                pass

    # -- proxy API ------------------------------------------------------------
    def _proxy_api(self, path_with_qs: str, emp: dict, body: dict | None = None):
        """Transmet la requête au WebUI avec le profil de l'employé."""
        profile = emp["profile"]
        parsed = urlparse(path_with_qs)
        path = parsed.path
        qs = parsed.query

        # Liste des sessions de chat de cet employé — filtrée par profil
        if path == "/api/sessions":
            status, hdrs, data = upstream_request(path_with_qs, profile=profile)
            if status == 200:
                try:
                    payload = json.loads(data)
                    sessions = payload.get("sessions", [])
                    payload["sessions"] = [
                        s for s in sessions if s.get("profile") == profile
                    ]
                    data = json.dumps(payload, ensure_ascii=False).encode()
                except Exception:
                    pass
            self._relay_json(status, data)
            return

        # Détail d'une session — vérifie la propriété avant de relayer
        if path == "/api/session":
            qs_map = parse_qs(qs)
            sid = (qs_map.get("session_id") or [""])[0]
            status, _, data = self._owned_session_check(sid, profile)
            if status != 200:
                self._relay_json(status, data)
                return
            status, _, data = upstream_request(path_with_qs, profile=profile)
            self._relay_json(status, data)
            return

        # Annulation d'un flux
        if path == "/api/chat/cancel":
            qs_map = parse_qs(qs)
            stream_id = (qs_map.get("stream_id") or [""])[0]
            body = dict(body or {})
            if stream_id:
                body["stream_id"] = stream_id
            status, _, data = self._owned_stream(stream_id or body.get("stream_id", ""), profile)
            if status != 200:
                self._relay_json(status, data)
                return
            status, _, data = upstream_request(path, method="POST", body=body, profile=profile)
            self._relay_json(status, data)
            return

        # Création de session — force le profil de l'employé
        if path == "/api/session/new":
            body = dict(body or {})
            body["profile"] = profile
            status, _, data = upstream_request("/api/session/new", method="POST",
                                                body=body, profile=profile)
            self._relay_json(status, data)
            return

        # Démarrage d'un tour de chat — force le profil de l'employé
        if path == "/api/chat/start":
            body = dict(body or {})
            body["profile"] = profile
            status, _, data = upstream_request("/api/chat/start", method="POST",
                                               body=body, profile=profile)
            self._relay_json(status, data)
            return

        # Actions sur une session précise — vérifie la propriété
        if path in ("/api/session/delete", "/api/session/rename", "/api/session/move",
                    "/api/session/yolo", "/api/session/compress", "/api/session/pin"):
            body = dict(body or {})
            sid = body.get("session_id", "")
            if not sid:
                self._json({"error": "session_id required"}, 400)
                return
            status, _, data = self._owned_session_check(sid, profile)
            if status != 200:
                self._relay_json(status, data)
                return
            status, _, data = upstream_request(path, method="POST", body=body, profile=profile)
            self._relay_json(status, data)
            return

        # Liste des profils (info seulement)
        if path == "/api/profiles":
            status, _, data = upstream_request(path, profile=profile)
            self._relay_json(status, data)
            return

        # Par défaut : GET simple relayé avec le profil de l'employé
        status, _, data = upstream_request(path, profile=profile,
                                           method="POST" if body is not None else "GET",
                                           body=body)
        self._relay_json(status, data)

    def _owned_session_check(self, sid: str, profile: str):
        status, _, data = upstream_request(f"/api/session?session_id={sid}", profile=profile)
        if status != 200:
            return status, {}, json.dumps({"error": "session not found"}).encode()
        try:
            sess = json.loads(data).get("session", {})
            if sess.get("profile") != profile:
                return 403, {}, json.dumps({"error": "forbidden"}).encode()
        except Exception:
            return 500, {}, json.dumps({"error": "bad session data"}).encode()
        return 200, {}, b"{}"

    def _owned_stream(self, stream_id: str, profile: str):
        """Vérifie via stream/status que le flux existe et appartient à l'employé."""
        status, _, data = upstream_request(f"/api/chat/stream/status?stream_id={stream_id}",
                                           profile=profile)
        if status != 200:
            return status, {}, json.dumps({"error": "stream not found"}).encode()
        return 200, {}, b"{}"

    def _relay_json(self, status: int, data: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # -- proxy SSE ------------------------------------------------------------
    def _handle_sse_proxy(self, parsed, emp: dict):
        qs = parse_qs(parsed.query)
        stream_id = qs.get("stream_id", [""])[0]
        if not stream_id:
            self._json({"error": "stream_id required"}, 400)
            return
        # Vérifie que le flux appartient bien à l'employé avant d'ouvrir le flux
        status, _, data = self._owned_stream(stream_id, emp["profile"])
        if status != 200:
            self._json({"error": "stream not found"}, 404)
            return
        try:
            resp = upstream_stream(f"/api/chat/stream?stream_id={stream_id}", emp["profile"])
        except Exception as e:
            logger.error("SSE upstream open failed: %s", e)
            self._json({"error": "stream open failed"}, 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_http_headers_sse()
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def send_http_headers_sse(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    # -- fichiers statiques ----------------------------------------------------
    def _guess_type(self, path: str) -> str:
        if path.endswith(".html"):
            return "text/html; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        return "application/octet-stream"

    def _serve_file(self, name: str, mime: str):
        f = BASE_DIR / name
        if not f.exists():
            self._json({"error": "not found"}, 404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    # Bootstrap : crée le compte admin par défaut si aucun utilisateur
    if not _load_users():
        default_pw = os.getenv("TEAM_ADMIN_PASSWORD", "changeme")
        _save_users({
            "admin": {
                "password_hash": _hash_password(default_pw),
                "profile": "default",
                "display_name": "Administrateur",
            }
        })
        logger.info("No users found — created default admin (password from TEAM_ADMIN_PASSWORD env or 'changeme')")

    server = QuietHTTPServer((HOST, PORT), TeamHandler)
    logger.info("Hermes Team App listening on http://%s:%d (upstream %s:%d)", HOST, PORT, UPSTREAM_HOST, UPSTREAM_PORT)
    print(f"\n  Team App: http://0.0.0.0:{PORT}")
    print(f"  Upstream: http://{UPSTREAM_HOST}:{UPSTREAM_PORT}\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()