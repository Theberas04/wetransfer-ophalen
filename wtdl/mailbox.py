"""Aangevraagde opnames: lees de WeTransfer-mails uit je mailbox via IMAP.

WeTransfer mailt je zodra er iets binnenkomt bij een aangevraagde overdracht
("Request files"). Die mail bevat de downloadlink. Deze module leest alleen die
mails, haalt de link eruit en laat de wachtrij de rest doen.

De module raakt de mailbox niet aan: er wordt niets verwijderd, niets als gelezen
gemarkeerd en niets verstuurd (``readonly=True``). Welke berichten al bekeken zijn
houdt de app zelf bij in ``mailbox.json``.
"""

from __future__ import annotations

import email
import html
import imaplib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any, Callable, Optional

from .wetransfer import extract_links

MAX_IDS = 1000
DEFAULT_SENDER = "wetransfer"
DEFAULT_DAYS = 30
# Mailservers van de bekende aanbieders, zodat een adres genoeg is.
KNOWN_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
}
SHORT_NAMES = {
    "gmail": "imap.gmail.com",
    "googlemail": "imap.gmail.com",
    "icloud": "imap.mail.me.com",
}
# Bij deze aanbieders werkt IMAP met een wachtwoord of app-wachtwoord niet (meer).
NO_PASSWORD_IMAP = (
    "outlook.com",
    "outlook.nl",
    "hotmail.com",
    "hotmail.nl",
    "live.com",
    "live.nl",
    "msn.com",
    "office365.com",
)


def normalize_host(host: str) -> str:
    """Maak van 'gmail' of 'gmail.com' de echte mailserver."""

    value = (host or "").strip().lower()
    if not value:
        return ""
    if value in SHORT_NAMES:
        return SHORT_NAMES[value]
    if "." not in value:
        return value
    return value


class MailboxError(Exception):
    """Fout met een Nederlandse uitleg die in de app te zien is."""


@dataclass
class MailboxConfig:
    """Instellingen van de mailbox."""

    host: str = ""
    port: int = 993
    user: str = ""
    password: str = ""
    folder: str = "INBOX"
    sender: str = DEFAULT_SENDER
    days: int = DEFAULT_DAYS
    use_ssl: bool = True

    def effective_host(self) -> str:
        """De mailserver: wat is ingevuld, of die van de aanbieder van het adres."""

        host = normalize_host(self.host)
        if host:
            return host
        domain = (self.user or "").strip().lower().rsplit("@", 1)[-1]
        return KNOWN_HOSTS.get(domain, "")

    def problem(self) -> str:
        """Geef terug wat er nog mist, of een lege tekst als alles ingevuld is."""

        if not self.user.strip():
            return "Er is nog geen e-mailadres ingevuld."
        domain = self.user.strip().lower().rsplit("@", 1)[-1]
        if domain in NO_PASSWORD_IMAP:
            return (
                "Bij Microsoft 365 (Outlook, Hotmail, Live) werkt IMAP met een wachtwoord niet meer. "
                "Gebruik een Gmail- of iCloud-adres, of laat je WeTransfer-mails daarheen doorsturen."
            )
        if not self.effective_host():
            return "Er is nog geen mailserver ingevuld."
        if not self.password:
            return "Er is nog geen wachtwoord (of app-wachtwoord) ingevuld."
        return ""

    @property
    def ready(self) -> bool:
        return not self.problem()

    def as_dict(self) -> dict[str, Any]:
        data = {
            "host": self.host,
            "effective_host": self.effective_host(),
            "port": self.port,
            "user": self.user,
            "folder": self.folder,
            "sender": self.sender,
            "days": self.days,
            "use_ssl": self.use_ssl,
        }
        return data


def decode_header_value(raw: Any) -> str:
    """Lees een onderwerp of afzender netjes uit, ook met rare tekensets."""

    if raw is None:
        return ""
    try:
        parts = decode_header(str(raw))
    except Exception:  # noqa: BLE001 - kapotte header mag geen fout geven
        return str(raw)
    chunks: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                chunks.append(text.decode(charset or "utf-8", errors="ignore"))
            except LookupError:
                chunks.append(text.decode("utf-8", errors="ignore"))
        else:
            chunks.append(text)
    return "".join(chunks)


def message_text(raw: bytes) -> str:
    """Alle leesbare tekst uit een bericht (zowel platte tekst als HTML)."""

    try:
        message = email.message_from_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise MailboxError(f"Bericht kon niet worden gelezen: {exc}") from exc
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks.append(bytes(payload).decode(charset, errors="ignore"))
        except LookupError:
            chunks.append(bytes(payload).decode("utf-8", errors="ignore"))
    if not chunks:
        payload = message.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            chunks.append(bytes(payload).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def links_from_message(raw: bytes, allow_hosts: tuple[str, ...] = ()) -> list[str]:
    """Haal de WeTransfer-links uit één e-mailbericht."""

    text = html.unescape(message_text(raw))
    return extract_links(text, allow_hosts)


def message_meta(raw: bytes) -> dict[str, str]:
    """Afzender, onderwerp en datum van een bericht (voor het logboek)."""

    try:
        message = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return {"sender": "", "subject": "", "date": ""}
    return {
        "sender": decode_header_value(message.get("From")),
        "subject": decode_header_value(message.get("Subject")),
        "date": decode_header_value(message.get("Date")),
    }


class MailboxWatcher:
    """Leest de WeTransfer-mails en geeft de gevonden links door."""

    def __init__(
        self,
        config: MailboxConfig,
        state_path: Path,
        log: Optional[Callable[[str], None]] = None,
        on_links: Optional[Callable[[list[str], str], None]] = None,
        client_factory: Optional[Callable[[MailboxConfig], Any]] = None,
        allow_hosts: tuple[str, ...] = (),
    ) -> None:
        self.config = config
        self.state_path = Path(state_path)
        self.log = log
        self.on_links = on_links
        self.client_factory = client_factory or default_client
        self.allow_hosts = tuple(allow_hosts)
        self.seen: list[str] = []
        self.last_result: dict[str, Any] = {}
        self.load()

    # -- toestand ----------------------------------------------------------
    def load(self) -> None:
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        seen = data.get("seen") if isinstance(data, dict) else None
        if isinstance(seen, list):
            self.seen = [str(entry) for entry in seen]

    def save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"saved_at": time.time(), "seen": self.seen[-MAX_IDS:]}
            tmp = self.state_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.config.ready,
            "problem": self.config.problem(),
            "host": self.config.host,
            "user": self.config.user,
            "folder": self.config.folder,
            "seen": len(self.seen),
            "last_result": self.last_result,
        }

    # -- controleren -------------------------------------------------------
    def check_once(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "checked_at": time.time(),
            "messages": 0,
            "links": [],
            "subjects": [],
            "errors": [],
        }
        problem = self.config.problem()
        if problem:
            result["errors"].append(problem)
            self.last_result = result
            return result

        client = None
        try:
            client = self._connect()
            client.select(self.config.folder, readonly=True)
            self._write(f"Mailbox: verbonden met {self.config.effective_host()} ({self.config.folder}).")
            for uid in self._new_uids(client):
                raw = self._fetch(client, uid)
                if not raw:
                    continue
                meta = message_meta(raw)
                found = links_from_message(raw, self.allow_hosts)
                result["messages"] += 1
                if found:
                    result["links"].extend(found)
                    result["subjects"].append(meta.get("subject", "") or meta.get("sender", ""))
                    self._write(
                        f"Mailbox: '{meta.get('subject', '')}' van {meta.get('sender', '')} — "
                        f"{len(found)} WeTransfer-link(s)."
                    )
                else:
                    self._write(f"Mailbox: bericht zonder WeTransfer-link overgeslagen ({meta.get('subject', '')}).")
                self.seen.append(uid)
        except imaplib.IMAP4.error as exc:
            message = _login_message(str(exc))
            result["errors"].append(message)
            self._write(f"Mailbox: {message}")
        except (OSError, MailboxError) as exc:
            message = f"Mailbox niet bereikbaar: {exc}"
            result["errors"].append(message)
            self._write(message)
        except Exception as exc:  # noqa: BLE001 - de bron mag nooit de app laten vallen
            message = f"Mailbox: onverwachte fout: {exc.__class__.__name__}: {exc}"
            result["errors"].append(message)
            self._write(message)
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001 - afsluiten mag niet klagen
                    pass

        self.seen = self.seen[-MAX_IDS:]
        self.save()
        self.last_result = result
        if result["links"] and self.on_links:
            self.on_links(result["links"], "mailbox")
        return result

    def _connect(self) -> Any:
        """Inloggen; probeer bij een app-wachtwoord met spaties ook de variant zonder.

        Google toont een app-wachtwoord als vier groepjes van vier tekens. De spaties
        horen niet bij het wachtwoord, maar als ze meegeplakt worden mislukt de login.
        """

        config = self.config
        client = self.client_factory(config)
        try:
            client.login(config.user, config.password)
            return client
        except imaplib.IMAP4.error:
            zonder_spaties = (config.password or "").replace(" ", "")
            if not zonder_spaties or zonder_spaties == config.password:
                raise
            self._write("Mailbox: met spaties mislukte het inloggen; opnieuw zonder spaties.")
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - afsluiten mag niet klagen
                pass
            client = self.client_factory(config)
            client.login(config.user, zonder_spaties)
            return client

    def _new_uids(self, client: Any) -> list[str]:
        since = (date.today() - timedelta(days=max(1, int(self.config.days or DEFAULT_DAYS)))).strftime("%d-%b-%Y")
        criteria = f'(FROM "{self.config.sender}") SINCE {since}'
        status, data = client.uid("search", None, criteria)
        if status != "OK":
            raise MailboxError(f"Zoeken in de mailbox mislukte ({status}).")
        raw = data[0] if data else b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="ignore")
        uids = [part.decode("ascii", errors="ignore") for part in (raw or b"").split()]
        return [uid for uid in uids if uid and uid not in self.seen]

    @staticmethod
    def _fetch(client: Any, uid: str) -> bytes:
        status, data = client.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            return b""
        chunks: list[bytes] = []
        for part in data or []:
            if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
                chunks.append(bytes(part[1]))
        return b"".join(chunks)

    def _write(self, message: str) -> None:
        if self.log:
            self.log(message)


def default_client(config: MailboxConfig) -> Any:
    """Open een echte IMAP-verbinding."""

    host = config.effective_host()
    if config.use_ssl:
        return imaplib.IMAP4_SSL(host, int(config.port or 993))
    return imaplib.IMAP4(host, int(config.port or 143))


def _login_message(raw: str) -> str:
    lowered = raw.lower()
    if "authenticationfailed" in lowered or "login failed" in lowered or "invalid credentials" in lowered:
        return (
            "Inloggen op de mailbox mislukte. Gebruik een app-wachtwoord; bij Microsoft 365 werkt een gewoon "
            "wachtwoord niet meer (IMAP met wachtwoord is daar uitgezet)."
        )
    return f"Mailbox gaf een fout: {raw[:160]}"
