"""De webversie: dezelfde pagina op een IP-adres in je eigen netwerk.

De app opent een klein HTTP-servertje. Daarmee is hij vanaf elke telefoon of
computer in het netwerk te gebruiken (``http://192.168.1.x:8756``), met dezelfde
pagina als in het venster op de pc.

Beveiliging: er wordt een toegangscode verwacht (een cookie die je één keer
intoetst). Zonder code antwoordt de server alleen de inlogpagina. Zet je de code
leeg, dan staat de pagina echt open voor iedereen in het netwerk.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .store import APP_NAME
from .version import __version__

COOKIE_NAME = "wto_code"
MAX_BODY = 1024 * 1024
DEFAULT_PORT = 8756

# Wat de webpagina mag aanroepen. Bewust géén vensters (die horen bij de pc) en
# geen afsluiten.
WEB_METHODS = (
    "get_state",
    "add_links",
    "import_file",
    "save_settings",
    "start_queue",
    "stop_queue",
    "retry_item",
    "remove_item",
    "clear_finished",
    "scan_watch_folder",
    "check_github",
    "check_mailbox",
    "check_all_sources",
    "test_slack",
    "open_folder",
    "clear_log",
    "web_info",
)

MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


def new_code() -> str:
    """Een korte toegangscode die je kunt overtypen op je telefoon."""

    return secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")


def local_ip() -> str:
    """Het eigen adres in het netwerk, om aan de gebruiker te tonen."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


LOGIN_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeTransfer Ophalen — toegangscode</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #f4f7fb; color: #0f172a;
         font: 16px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .kaart { background: #fff; border: 1px solid #e3eaf4; border-radius: 16px; padding: 28px;
           width: min(92vw, 380px); box-shadow: 0 18px 40px rgba(15, 23, 42, .08); }
  h1 { margin: 0 0 6px; font-size: 20px; }
  p { margin: 0 0 18px; color: #475569; font-size: 14px; }
  input { width: 100%; padding: 12px 14px; font-size: 16px; border: 1px solid #e3eaf4;
          border-radius: 10px; box-sizing: border-box; }
  button { margin-top: 12px; width: 100%; padding: 12px; font-size: 16px; font-weight: 600;
           color: #fff; border: 0; border-radius: 10px; cursor: pointer;
           background: linear-gradient(135deg, #5ff0e8, #2ea3f2 55%, #1e73be); }
  .fout { margin-top: 12px; color: #b42318; font-size: 14px; min-height: 20px; }
</style>
</head>
<body>
  <form class="kaart" id="form">
    <h1>WeTransfer Ophalen</h1>
    <p>Vul de toegangscode in die in het venster op de pc staat.</p>
    <input id="code" autocomplete="one-time-code" autofocus placeholder="Toegangscode">
    <button type="submit">Openen</button>
    <div class="fout" id="fout"></div>
  </form>
<script>
  document.getElementById("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = document.getElementById("code").value.trim();
    const fout = document.getElementById("fout");
    fout.textContent = "";
    try {
      const antwoord = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code })
      });
      if (antwoord.ok) { location.href = "/"; return; }
      const data = await antwoord.json().catch(() => ({}));
      fout.textContent = data.error || "Deze code klopt niet.";
    } catch (foutmelding) {
      fout.textContent = "Geen verbinding met de app.";
    }
  });
</script>
</body>
</html>
"""


class WebServer:
    """Het HTTP-servertje achter de webversie."""

    def __init__(
        self,
        api: Any,
        ui_dir: Path,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        code: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.api = api
        self.ui_dir = Path(ui_dir)
        self.host = (host or "0.0.0.0").strip()
        self.port = int(port or DEFAULT_PORT)
        self.code = (code or "").strip()
        self.log = log
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.started_at = 0.0

    # -- starten en stoppen ------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, ""
        try:
            handler = self._handler_class()
            self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
            self.httpd.daemon_threads = True
            # Poort 0 betekent "kies er zelf een"; dan is dit de echte poort.
            self.port = int(self.httpd.server_address[1])
        except OSError as exc:
            return False, f"Webversie starten mislukte op poort {self.port}: {exc}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="webserver", daemon=True)
        self.thread.start()
        self.started_at = time.time()
        self._write(f"Webversie actief op {self.address()}" + ("" if self.code else " (zonder toegangscode!)"))
        return True, ""

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except OSError:
                pass
        self.httpd = None
        self.thread = None

    def address(self) -> str:
        host = local_ip() if self.host in ("0.0.0.0", "", "::") else self.host
        return f"http://{host}:{self.port}"

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self.running,
            "address": self.address() if self.running else "",
            "host": self.host,
            "port": self.port,
            "code": self.code,
            "needs_code": bool(self.code),
            "started_at": self.started_at,
        }

    def _write(self, message: str) -> None:
        if self.log:
            self.log(message)

    # -- verzoeken ---------------------------------------------------------
    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"WeTransferOphalen/{__version__}"
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API van de basisklasse
                """Het eigen logboek doet het loggen; de standaardregels zijn te veel."""
                return

            # -- helpers ---------------------------------------------------
            def _authorized(self) -> bool:
                if not server.code:
                    return True
                raw = self.headers.get("Cookie") or ""
                jar = SimpleCookie()
                try:
                    jar.load(raw)
                except Exception:  # noqa: BLE001 - kapotte cookie is geen fout
                    return False
                morsel = jar.get(COOKIE_NAME)
                return bool(morsel and secrets.compare_digest(morsel.value, server.code))

            def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, payload: Any, status: int = 200, extra: Optional[dict[str, str]] = None) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self._send(status, body, "application/json; charset=utf-8", extra)

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return {}
                if length <= 0 or length > MAX_BODY:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8")) or {}
                except (ValueError, UnicodeDecodeError):
                    return {}

            def _cookie_header(self) -> dict[str, str]:
                value = f"{COOKIE_NAME}={server.code}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
                return {"Set-Cookie": value}

            # -- routes ----------------------------------------------------
            def do_HEAD(self) -> None:  # noqa: N802 - API van de basisklasse
                self.do_GET()

            def do_GET(self) -> None:  # noqa: N802 - API van de basisklasse
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802 - API van de basisklasse
                self._route("POST")

            def _route(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query)

                if path == "/api/login":
                    self._login()
                    return

                # Een code in de adresbalk (?code=...) zet meteen het cookie.
                if not self._authorized() and server.code and query.get("code", [""])[0] == server.code:
                    self._send(302, b"", "text/plain", {"Location": "/", **self._cookie_header()})
                    return

                if not self._authorized():
                    if path.startswith("/api/"):
                        self._json({"ok": False, "error": "Eerst de toegangscode intoetsen."}, 401)
                    else:
                        body = LOGIN_PAGE.encode("utf-8")
                        self._send(200, body, "text/html; charset=utf-8")
                    return

                if path == "/api/ping":
                    self._json({"ok": True, "app": APP_NAME, "version": __version__})
                    return
                if path == "/api/state":
                    self._json(server.api.get_state())
                    return
                if path == "/api/call":
                    self._call()
                    return
                if path.startswith("/api/"):
                    self._json({"ok": False, "error": f"Onbekende vraag: {path}"}, 404)
                    return
                self._static(path)

            def _login(self) -> None:
                data = self._read_json()
                code = str(data.get("code") or "").strip()
                if not server.code:
                    self._json({"ok": True})
                    return
                if not code or not secrets.compare_digest(code, server.code):
                    self._json({"ok": False, "error": "Deze code klopt niet."}, 401)
                    return
                self._json({"ok": True}, 200, self._cookie_header())

            def _call(self) -> None:
                data = self._read_json()
                name = str(data.get("method") or "")
                arguments = data.get("arguments") or {}
                if name not in WEB_METHODS:
                    self._json({"ok": False, "error": f"Deze handeling mag niet vanaf het web: {name}"}, 403)
                    return
                function = getattr(server.api, name, None)
                if function is None:
                    self._json({"ok": False, "error": f"Onbekende handeling: {name}"}, 404)
                    return
                if not isinstance(arguments, dict):
                    arguments = {}
                try:
                    result = function(**arguments)
                except TypeError as exc:
                    self._json({"ok": False, "error": f"Verkeerde gegevens: {exc}"}, 400)
                    return
                except Exception as exc:  # noqa: BLE001 - netjes teruggeven aan de pagina
                    self._json({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, 500)
                    return
                self._json(result if isinstance(result, dict) else {"ok": True, "result": result})

            def _static(self, path: str) -> None:
                relative = "index.html" if path in ("/", "") else path.lstrip("/")
                target = (server.ui_dir / relative).resolve()
                try:
                    inside = target.is_relative_to(server.ui_dir.resolve())
                except AttributeError:  # Python 3.8 en ouder
                    inside = str(target).startswith(str(server.ui_dir.resolve()))
                if not inside or not target.is_file():
                    self._send(404, "Niet gevonden".encode("utf-8"), "text/plain; charset=utf-8")
                    return
                try:
                    body = target.read_bytes()
                except OSError as exc:
                    self._send(500, f"Bestand lezen mislukte: {exc}".encode("utf-8"), "text/plain; charset=utf-8")
                    return
                content_type = MIME_OVERRIDES.get(target.suffix.lower())
                if content_type is None:
                    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, body, content_type)

        return Handler
