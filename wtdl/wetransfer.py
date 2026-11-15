"""WeTransfer-links omzetten naar een directe downloadlink.

WeTransfer heeft geen openbare API voor het ophalen van andermans transfers.
Deze module gebruikt daarom precies dezelfde twee verzoeken die de knop
"Downloaden" op wetransfer.com zelf doet:

1. ``POST /api/v4/transfers/<id>/prepare-download`` met het security-hash uit de
   link: levert de status van de transfer (klaar, verlopen, wachtwoord) plus de
   aanbevolen bestandsnaam en de grootte.
2. ``POST /api/v4/transfers/<id>/download``: levert ``direct_link``, een
   ondertekende link naar download.wetransfer.com die ongeveer tien minuten
   geldig blijft.

Deze code is zelf geschreven; de functionele inspiratie komt van transferwee
(https://github.com/iamleot/transferwee) en de genoemde endpoints komen uit de
JavaScript-bundel van wetransfer.com. Er is geen code van derden overgenomen.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

import requests

API_BASE = "https://wetransfer.com/api/v4"
WEB_BASE = "https://wetransfer.com"
DOWNLOAD_HOST = "download.wetransfer.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Een link eindigt vaak op een punt, komma of haakje in een e-mail; die knippen we eraf.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\"»"
_SHARE_HOSTS = ("we.tl", "go.wetransfer.com")
_WT_HOSTS = ("wetransfer.com", "download.wetransfer.com")


class TransferError(Exception):
    """Fout met een code en een Nederlandse uitleg die in de app te zien is."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ShareLink:
    """De onderdelen die uit een WeTransfer-link te halen zijn."""

    url: str  # de volledige link (na het volgen van een we.tl-omleiding)
    short_url: str  # de link zoals die is aangeleverd
    transfer_id: str
    security_hash: str
    recipient_id: Optional[str] = None
    direct: bool = False  # True voor een al ondertekende download.wetransfer.com-link

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "short_url": self.short_url,
            "transfer_id": self.transfer_id,
            "security_hash": self.security_hash,
            "recipient_id": self.recipient_id,
            "direct": self.direct,
        }


@dataclass
class TransferInfo:
    """Wat WeTransfer over de transfer vertelt, plus de link waarmee we begonnen."""

    link: ShareLink
    state: str = "onbekend"
    recommended_filename: str = ""
    size: Optional[int] = None
    password_protected: bool = False
    expires_at: Optional[str] = None
    uploaded_at: Optional[str] = None
    transfer_type: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def downloadable(self) -> bool:
        return self.state in ("downloadable", "ready")

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.link.short_url,
            "transfer_id": self.link.transfer_id,
            "state": self.state,
            "recommended_filename": self.recommended_filename,
            "size": self.size,
            "password_protected": self.password_protected,
            "expires_at": self.expires_at,
            "uploaded_at": self.uploaded_at,
        }


def host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host


def _normalized_hosts(allow_hosts: tuple[str, ...]) -> set[str]:
    return {entry.split(":")[0].strip().lower() for entry in allow_hosts if entry}


def is_wetransfer_url(url: str, allow_hosts: tuple[str, ...] = ()) -> bool:
    host = host_of(url)
    if host and host in _normalized_hosts(allow_hosts):
        return True
    return any(host == h or host.endswith("." + h) for h in _WT_HOSTS) or host in _SHARE_HOSTS


def is_short_url(url: str, allow_hosts: tuple[str, ...] = ()) -> bool:
    host = host_of(url)
    return host in _SHARE_HOSTS or (bool(host) and host in _normalized_hosts(allow_hosts))


def extract_links(text: str, allow_hosts: tuple[str, ...] = ()) -> list[str]:
    """Haal alle WeTransfer-links uit een lap tekst, zonder dubbelen."""

    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.findall(text or ""):
        candidate = match.rstrip(_TRAILING)
        if not is_wetransfer_url(candidate, allow_hosts):
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found


def parse_share_url(url: str, allow_hosts: tuple[str, ...] = ()) -> ShareLink:
    """Lees transfer-id, ontvanger-id en security-hash uit een link.

    Ondersteund:
      * ``https://we.tl/t-xxxxxxxx`` (korte link, wordt eerst omgeleid)
      * ``https://wetransfer.com/downloads/<id>/<hash>``
      * ``https://wetransfer.com/downloads/<id>/<ontvanger>/<hash>``
      * ``https://download.wetransfer.com/...`` (al ondertekende directe link)
    """

    clean = (url or "").strip().rstrip(_TRAILING)
    if not clean:
        raise TransferError("geen_link", "Er is geen link opgegeven.")
    if not is_wetransfer_url(clean, allow_hosts):
        raise TransferError(
            "geen_link",
            "Dit is geen WeTransfer-link.",
            "Plak een link die begint met https://we.tl/ of https://wetransfer.com/downloads/.",
        )

    host = host_of(clean)
    if host == DOWNLOAD_HOST:
        name = unquote(urlparse(clean).path.rsplit("/", 1)[-1])
        return ShareLink(
            url=clean,
            short_url=clean,
            transfer_id="",
            security_hash="",
            direct=True,
        )

    path = urlparse(clean).path.strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[0] != "downloads":
        raise TransferError(
            "geen_link",
            "Deze WeTransfer-link bevat geen transfer.",
            "Gebruik de downloadlink uit de e-mail, niet de uploadpagina.",
        )
    parts = segments[1:]
    if len(parts) == 2:
        transfer_id, security_hash = parts
        recipient_id = None
    elif len(parts) >= 3:
        # Sinds 2026 staat bij links uit e-mail de ontvanger vóór de hash.
        transfer_id, recipient_id, security_hash = parts[0], parts[1], parts[-1]
    else:
        raise TransferError("geen_link", "Deze WeTransfer-link is onvolledig.")
    if not transfer_id or not security_hash:
        raise TransferError("geen_link", "Deze WeTransfer-link is onvolledig.")
    return ShareLink(
        url=clean,
        short_url=clean,
        transfer_id=transfer_id,
        security_hash=security_hash,
        recipient_id=recipient_id,
    )


def human_size(size: Optional[int]) -> str:
    if not size:
        return "onbekend"
    units = ["B", "kB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class WetransferClient:
    """Praat met dezelfde endpoints als de website."""

    def __init__(
        self,
        api_base: str = API_BASE,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        user_agent: str = USER_AGENT,
        allow_hosts: tuple[str, ...] = (),
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        # Alleen voor tests: extra hosts die als WeTransfer-link gelden (de nabootser).
        self.allow_hosts = tuple(allow_hosts)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            }
        )

    # -- helpers ---------------------------------------------------------
    def _api_headers(self, referer: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "x-requested-with": "XMLHttpRequest"}
        if referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
                headers["Referer"] = referer
        return headers

    def _post(self, path: str, payload: dict[str, Any], referer: str) -> requests.Response:
        url = f"{self.api_base}{path}"
        try:
            return self.session.post(
                url, json=payload, headers=self._api_headers(referer), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TransferError(
                "verbinding",
                f"Geen verbinding met WeTransfer: {exc.__class__.__name__}.",
                "Controleer de internetverbinding en probeer het opnieuw.",
            ) from exc

    def follow_short_link(self, url: str) -> str:
        """Volg een we.tl-link naar de volledige downloads-pagina."""

        try:
            response = self.session.head(
                url, allow_redirects=True, timeout=self.timeout, headers={"User-Agent": self.user_agent}
            )
        except requests.RequestException as exc:
            raise TransferError(
                "verbinding",
                f"De korte link kon niet worden geopend: {exc.__class__.__name__}.",
            ) from exc
        return response.url or url

    def parse(self, url: str) -> ShareLink:
        clean = (url or "").strip().rstrip(_TRAILING)
        if is_short_url(clean, self.allow_hosts):
            full = self.follow_short_link(clean)
            link = parse_share_url(full, self.allow_hosts)
            return ShareLink(
                url=link.url,
                short_url=clean,
                transfer_id=link.transfer_id,
                security_hash=link.security_hash,
                recipient_id=link.recipient_id,
                direct=link.direct,
            )
        return parse_share_url(clean, self.allow_hosts)

    # -- de twee verzoeken ------------------------------------------------
    def prepare(self, link: ShareLink, password: str = "") -> TransferInfo:
        payload: dict[str, Any] = {"security_hash": link.security_hash}
        if link.recipient_id:
            payload["recipient_id"] = link.recipient_id
        if password:
            payload["password"] = password
        response = self._post(f"/transfers/{link.transfer_id}/prepare-download", payload, link.url)
        body = _json_or_empty(response)
        if response.status_code == 429:
            raise TransferError(
                "te_veel",
                "WeTransfer krijgt te veel verzoeken van dit adres.",
                "Wacht een minuut en probeer het opnieuw.",
            )
        if response.status_code == 404:
            raise TransferError(
                "niet_gevonden", "Deze transfer bestaat niet (meer) of is verwijderd."
            )
        if response.status_code in (401, 403):
            if password:
                raise TransferError("wachtwoord_fout", "Het wachtwoord van deze transfer is onjuist.")
            raise TransferError(
                "geen_toegang",
                "WeTransfer geeft geen toegang tot deze transfer.",
                "Mogelijk is de transfer verlopen of ingetrokken.",
            )
        if response.status_code != 200:
            raise TransferError(
                "onbekend",
                f"WeTransfer antwoordde met status {response.status_code}.",
                str(body)[:200],
            )
        info = TransferInfo(
            link=link,
            state=str(body.get("state") or "onbekend"),
            recommended_filename=str(body.get("recommended_filename") or ""),
            size=body.get("size") if isinstance(body.get("size"), int) else None,
            password_protected=bool(body.get("password_protected")),
            expires_at=body.get("expires_at"),
            uploaded_at=body.get("uploaded_at"),
            transfer_type=body.get("transfer_type"),
            raw=body if isinstance(body, dict) else {},
        )
        if not info.downloadable:
            if info.state in ("expired", "deleted"):
                raise TransferError(
                    "verlopen",
                    "Deze transfer is verlopen."
                    + (f" (verliep op {info.expires_at})" if info.expires_at else ""),
                    "Vraag de afzender om de bestanden opnieuw te sturen.",
                )
            raise TransferError(
                "niet_klaar",
                f"Deze transfer is nog niet op te halen (status: {info.state}).",
                "Probeer het later opnieuw; de upload is misschien nog bezig.",
            )
        if info.password_protected and not password:
            raise TransferError(
                "wachtwoord_nodig",
                "Deze transfer is met een wachtwoord beveiligd.",
                "Vul het wachtwoord in en voeg de link opnieuw toe.",
            )
        return info

    def request_download(self, info: TransferInfo, password: str = "") -> str:
        payload: dict[str, Any] = {
            "security_hash": info.link.security_hash,
            "intent": "entire_transfer",
        }
        if info.link.recipient_id:
            payload["recipient_id"] = info.link.recipient_id
        if password:
            payload["password"] = password
        response = self._post(f"/transfers/{info.link.transfer_id}/download", payload, info.link.url)
        body = _json_or_empty(response)
        if response.status_code == 429:
            raise TransferError(
                "te_veel",
                "WeTransfer krijgt te veel verzoeken van dit adres.",
                "Wacht een minuut en probeer het opnieuw.",
            )
        if response.status_code in (401, 403):
            message = str(body.get("message") or "")
            if "password" in message.lower() or password:
                raise TransferError("wachtwoord_fout", "Het wachtwoord van deze transfer is onjuist.")
            raise TransferError(
                "geen_toegang",
                "WeTransfer geeft geen downloadtoegang meer tot deze transfer.",
                "Meestal betekent dit dat de transfer is verlopen of ingetrokken.",
            )
        if response.status_code == 404:
            raise TransferError("niet_gevonden", "Deze transfer bestaat niet (meer).")
        if response.status_code != 200:
            raise TransferError(
                "onbekend",
                f"WeTransfer antwoordde met status {response.status_code} bij het downloaden.",
                str(body)[:200],
            )
        direct_link = str(body.get("direct_link") or "")
        if not direct_link:
            raise TransferError("onbekend", "WeTransfer gaf geen downloadlink terug.")
        return direct_link

    def resolve(self, url: str, password: str = "") -> tuple[TransferInfo, str]:
        """Volledige route: link -> metadata -> directe downloadlink."""

        link = self.parse(url)
        if link.direct:
            info = TransferInfo(link=link, state="downloadable")
            return info, link.url
        info = self.prepare(link, password=password)
        return info, self.request_download(info, password=password)


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def wait_for_retry(attempt: int, base: float = 2.0) -> None:
    time.sleep(min(base * attempt, 20.0))
