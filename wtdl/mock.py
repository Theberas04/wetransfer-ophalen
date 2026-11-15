"""Een lokale WeTransfer-nabootser voor tests en de zelftest van de .exe.

Zo kan de volledige keten (link -> metadata -> downloaden -> hervatten -> Slack)
worden getest zonder van de echte dienst afhankelijk te zijn.
"""

from __future__ import annotations

import hashlib
import json
import random
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

API_PATH = "/api/v4"


def make_payload(name: str, size: int) -> bytes:
    """Deterministische bytes zodat een test de inhoud kan narekenen."""

    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(size))


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class MockTransfer:
    transfer_id: str
    security_hash: str
    filename: str
    payload: bytes
    recipient_id: Optional[str] = None
    state: str = "downloadable"
    password: str = ""
    expires_at: str = "2027-01-01T00:00:00Z"
    uploaded_at: str = "2026-11-15T12:00:00Z"
    fail_after: Optional[int] = None  # verbinding één keer afbreken na N bytes

    @property
    def size(self) -> int:
        return len(self.payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.short_url(),
            "transfer_id": self.transfer_id,
            "security_hash": self.security_hash,
            "recipient_id": self.recipient_id,
            "filename": self.filename,
            "size": self.size,
            "state": self.state,
            "password": self.password,
            "sha256": sha256_of(self.payload),
        }

    def share_url(self, base_url: str) -> str:
        if self.recipient_id:
            return f"{base_url}/downloads/{self.transfer_id}/{self.recipient_id}/{self.security_hash}"
        return f"{base_url}/downloads/{self.transfer_id}/{self.security_hash}"

    def short_url(self) -> str:
        if self.recipient_id:
            return f"https://we.tl/t-{self.transfer_id[:10]}{self.recipient_id[:4]}"
        return f"https://we.tl/t-{self.transfer_id[:12]}"


class MockWetransferServer:
    """Draait op 127.0.0.1; de client wordt met ``api_base`` hiernaar verwezen."""

    def __init__(self, transfers: Optional[list[MockTransfer]] = None) -> None:
        self.transfers: dict[str, MockTransfer] = {t.transfer_id: t for t in (transfers or [])}
        self.prepare_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.file_requests: list[dict[str, Any]] = []
        self.slack_messages: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.base_url = ""
        self.api_base = ""

    # -- levensloop --------------------------------------------------------
    def start(self) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # geen ruis in de testuitvoer
                return

            # -- helpers --
            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = {}
                return data if isinstance(data, dict) else {}

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    # Een HEAD-antwoord mag geen body hebben; anders leest de client
                    # die bytes als het begin van het volgende antwoord.
                    self.wfile.write(body)

            # -- routes --
            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                body = self._body()
                if path.startswith(f"{API_PATH}/transfers/"):
                    rest = path[len(f"{API_PATH}/transfers/") :].split("/")
                    transfer_id = rest[0]
                    action = rest[1] if len(rest) > 1 else ""
                    with server._lock:
                        transfer = server.transfers.get(transfer_id)
                    if action == "prepare-download":
                        with server._lock:
                            server.prepare_calls.append({"id": transfer_id, "body": body})
                        if transfer is None:
                            return self._json(404, {"message": "Transfer not found"})
                        if transfer.password and body.get("password") != transfer.password:
                            if body.get("password"):
                                return self._json(
                                    403, {"message": "No download access to this Transfer"}
                                )
                        return self._json(
                            200,
                            {
                                "id": transfer.transfer_id,
                                "state": transfer.state,
                                "transfer_type": 4,
                                "shortened_url": transfer.short_url(),
                                "recommended_filename": transfer.filename,
                                "expires_at": transfer.expires_at,
                                "password_protected": bool(transfer.password),
                                "uploaded_at": transfer.uploaded_at,
                                "expiry_in_seconds": 3600,
                                "size": transfer.size,
                            },
                        )
                    if action == "download":
                        with server._lock:
                            server.download_calls.append({"id": transfer_id, "body": body})
                        if transfer is None:
                            return self._json(
                                404, {"message": "Couldn't find Transfer"}
                            )
                        if transfer.state != "downloadable":
                            return self._json(
                                403, {"message": "No download access to this Transfer"}
                            )
                        if transfer.password and body.get("password") != transfer.password:
                            return self._json(403, {"message": "password is invalid"})
                        direct = f"{server.base_url}/files/{quote(transfer.filename)}"
                        return self._json(200, {"direct_link": direct})
                if path == "/slack":
                    with server._lock:
                        server.slack_messages.append(body)
                    body_bytes = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return
                self._json(404, {"message": "unknown endpoint"})

            def do_HEAD(self) -> None:  # noqa: N802
                # Korte links: de echte dienst stuurt een 302 naar de downloads-pagina.
                path = urlparse(self.path).path
                if path.startswith("/t-"):
                    transfer_id = server._find_by_short(path)
                    transfer = server.transfers.get(transfer_id) if transfer_id else None
                    if transfer is None:
                        return self._json(404, {"message": "Transfer not found"})
                    self.send_response(302)
                    self.send_header("Location", transfer.share_url(server.base_url))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(404, {"message": "unknown endpoint"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.startswith("/files/"):
                    return self._serve_file(unquote(parsed.path[len("/files/") :]))
                self._json(404, {"message": "unknown endpoint"})

            # -- bestand met Range-ondersteuning --
            def _serve_file(self, name: str) -> None:
                transfer = None
                for candidate in server.transfers.values():
                    if candidate.filename == name:
                        transfer = candidate
                        break
                if transfer is None:
                    return self._json(404, {"message": "bestand onbekend"})
                data = transfer.payload
                total = len(data)
                start = 0
                status = 200
                range_header = self.headers.get("Range") or ""
                with server._lock:
                    server.file_requests.append(
                        {"name": name, "range": range_header, "headers": dict(self.headers)}
                    )
                if range_header.startswith("bytes="):
                    spec = range_header[len("bytes=") :].split(",")[0]
                    first, _, last = spec.partition("-")
                    if first:
                        start = int(first)
                    if last:
                        end = int(last)
                    else:
                        end = total - 1
                    if start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                    chunk = data[start : end + 1]
                    self.send_response(status)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                else:
                    chunk = data
                    self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()

                with server._lock:
                    fail_after = transfer.fail_after
                    if fail_after is not None:
                        transfer.fail_after = None  # alleen de eerste keer afbreken
                if fail_after is not None and len(chunk) > fail_after:
                    try:
                        self.wfile.write(chunk[:fail_after])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(chunk)
                except OSError:
                    self.close_connection = True

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address) -> None:  # noqa: ANN001
                # Een testclient die de verbinding bewust afbreekt is geen fout.
                import sys

                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        self._httpd = Server(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        host, port = self._httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.api_base = f"{self.base_url}{API_PATH}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-wetransfer", daemon=True)
        self._thread.start()
        return self.base_url

    def _find_by_short(self, path: str) -> Optional[str]:
        token = path.lstrip("/")
        for transfer in self.transfers.values():
            if transfer.short_url().rsplit("/", 1)[-1] == token:
                return transfer.transfer_id
            if token.startswith(f"t-{transfer.transfer_id[:10]}"):
                return transfer.transfer_id
        return None

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- gemak voor tests ---------------------------------------------------
    def default_transfers(self) -> list[MockTransfer]:
        return [
            MockTransfer(
                transfer_id="a" * 24,
                security_hash="hashplain",
                filename="wetransfer_fotos_2026-11-15_1200.zip",
                payload=make_payload("fotos", 400_000),
            ),
            MockTransfer(
                transfer_id="b" * 24,
                security_hash="hashmail",
                recipient_id="c" * 64,
                filename="Archiv.zip",
                payload=make_payload("archiv", 300_000),
            ),
            MockTransfer(
                transfer_id="d" * 24,
                security_hash="hashold",
                filename="wetransfer_oud_2026-01-23_0914.zip",
                payload=make_payload("oud", 1000),
                state="expired",
            ),
            MockTransfer(
                transfer_id="e" * 24,
                security_hash="hashsecret",
                filename="geheim.zip",
                payload=make_payload("geheim", 120_000),
                password="geheim123",
            ),
        ]
