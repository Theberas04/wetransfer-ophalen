"""Bestanden ophalen met voortgang, hervatten en een nette bestandsnaam."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import requests

from .wetransfer import TransferError, TransferInfo, human_size

CHUNK_SIZE = 256 * 1024
PART_SUFFIX = ".wetransfer.part"
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadCancelled(Exception):
    """De gebruiker stopte de download; het .part-bestand blijft staan."""


@dataclass
class DownloadResult:
    path: Path
    size: int
    seconds: float
    attempts: int = 1
    resumed_from: int = 0
    skipped: bool = False
    server_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "seconds": round(self.seconds, 1),
            "attempts": self.attempts,
            "resumed_from": self.resumed_from,
            "skipped": self.skipped,
            "server_name": self.server_name,
        }


def sanitize_filename(name: str, fallback: str = "wetransfer-bestand") -> str:
    """Maak van een bestandsnaam iets dat Windows zonder klagen accepteert."""

    cleaned = unquote(name or "").strip().replace("\\", "/").split("/")[-1]
    cleaned = _BAD_CHARS.sub("_", cleaned).strip(" .")
    if not cleaned:
        return fallback
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if len(stem) > 120:
        stem = stem[:120].rstrip()
    return f"{stem}.{suffix}" if suffix else (stem or fallback)


def unique_path(directory: Path, name: str) -> Path:
    """Zoek een vrije bestandsnaam: bestand.zip, bestand (2).zip, ..."""

    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        alternative = directory / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative
    return directory / f"{stem} ({int(time.time())}){suffix}"


def name_from_direct_link(direct_link: str) -> str:
    path = urlparse(direct_link).path
    return sanitize_filename(path.rsplit("/", 1)[-1])


def name_from_headers(response: requests.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if not match:
        match = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    if not match:
        return ""
    return sanitize_filename(match.group(1))


def _total_from_content_range(value: str) -> Optional[int]:
    match = re.search(r"/(\d+)\s*$", value or "")
    return int(match.group(1)) if match else None


def download(
    info: TransferInfo,
    direct_link: str,
    directory: Path,
    refresh: Optional[Callable[[], str]] = None,
    on_progress: Optional[Callable[[int, Optional[int], float], None]] = None,
    cancel: Optional[Any] = None,
    max_attempts: int = 4,
    session: Optional[requests.Session] = None,
    chunk_size: int = CHUNK_SIZE,
    name: str = "",
) -> DownloadResult:
    """Download de hele transfer naar ``directory`` en hervat waar mogelijk.

    ``refresh`` levert een nieuwe directe link als de ondertekende link verloopt;
    de download gaat dan verder met een Range-verzoek vanaf de al ontvangen bytes.
    ``name`` overschrijft de bestandsnaam; nodig voor links waarvan het adres niet
    op een bestandsnaam eindigt (zoals een releasebestand van de GitHub-API).
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()

    base_name = (
        sanitize_filename(name)
        if (name or "").strip()
        else name_from_direct_link(direct_link)
        or sanitize_filename(info.recommended_filename, f"wetransfer-{info.link.transfer_id or 'bestand'}.zip")
    )
    part_path = directory / f"{base_name}{PART_SUFFIX}"

    started = time.monotonic()
    attempts = 0
    resumed_from = 0
    total: Optional[int] = None
    last_error: Optional[Exception] = None
    server_name = ""

    while attempts < max_attempts:
        attempts += 1
        received = part_path.stat().st_size if part_path.exists() else 0
        if received:
            resumed_from = max(resumed_from, received)
        headers = {"Range": f"bytes={received}-"} if received else {}
        try:
            response = http.get(
                direct_link, stream=True, allow_redirects=True, headers=headers, timeout=(20, 120)
            )
        except requests.RequestException as exc:
            last_error = TransferError("verbinding", f"Netwerkfout tijdens downloaden: {exc}")
            if attempts < max_attempts:
                direct_link = _safe_refresh(refresh, last_error) if refresh else direct_link
                continue
            raise last_error

        try:
            with response:
                if response.status_code == 416 and received:
                    # Het .part-bestand is al compleet; er valt niets meer te halen.
                    total = received
                    break
                if response.status_code in (403, 404, 410):
                    raise TransferError(
                        "geen_toegang",
                        "De downloadlink van WeTransfer is verlopen.",
                        "De app vraagt automatisch een nieuwe link aan.",
                    )
                if response.status_code not in (200, 206):
                    raise TransferError(
                        "onbekend",
                        f"Downloaden mislukt: server antwoordde met status {response.status_code}.",
                    )

                if response.status_code == 206:
                    total = _total_from_content_range(response.headers.get("content-range", ""))
                    if total is None:
                        length = response.headers.get("content-length")
                        total = received + int(length) if length else None
                else:
                    if received:
                        received = 0  # server negeerde het Range-verzoek: opnieuw beginnen
                    length = response.headers.get("content-length")
                    total = int(length) if length else None

                if not server_name:
                    server_name = name_from_headers(response)
                    if server_name and server_name != base_name:
                        new_part = directory / f"{server_name}{PART_SUFFIX}"
                        if part_path.exists() and not new_part.exists():
                            os.replace(part_path, new_part)
                        part_path = new_part
                        base_name = server_name

                speed = 0.0
                last_mark = time.monotonic()
                last_bytes = received
                with part_path.open("ab" if received else "wb") as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last_mark >= 0.4:
                            speed = (received - last_bytes) / (now - last_mark)
                            last_mark, last_bytes = now, received
                        if on_progress:
                            on_progress(received, total, speed)
                if on_progress:
                    on_progress(received, total, speed)
        except DownloadCancelled:
            raise
        except TransferError as exc:
            last_error = exc
            if attempts < max_attempts and refresh:
                direct_link = _safe_refresh(refresh, exc)
                continue
            raise
        except requests.RequestException as exc:
            last_error = TransferError(
                "verbinding",
                f"De verbinding viel weg tijdens het downloaden ({exc.__class__.__name__}).",
                "De app gaat verder waar hij gebleven was.",
            )
            if attempts < max_attempts:
                direct_link = _safe_refresh(refresh, last_error) if refresh else direct_link
                continue
            raise last_error

        if total is None or received >= total:
            break
        last_error = TransferError(
            "onvolledig",
            f"De download stopte voortijdig ({human_size(received)} van {human_size(total)}).",
        )
        if attempts < max_attempts:
            direct_link = _safe_refresh(refresh, last_error) if refresh else direct_link
            continue
        break

    if not part_path.exists():
        raise last_error or TransferError("onbekend", "Er is niets gedownload.")

    size = part_path.stat().st_size
    if total is not None and size < total:
        raise TransferError(
            "onvolledig",
            f"De download is onvolledig: {human_size(size)} van {human_size(total)}.",
            "Probeer het opnieuw; de app gaat verder waar hij gebleven was.",
        )

    target = directory / base_name
    if target.exists():
        if size and target.stat().st_size == size:
            part_path.unlink(missing_ok=True)
            return DownloadResult(
                path=target,
                size=size,
                seconds=time.monotonic() - started,
                attempts=attempts,
                resumed_from=resumed_from,
                skipped=True,
                server_name=server_name,
            )
        target = unique_path(directory, base_name)
    os.replace(part_path, target)
    return DownloadResult(
        path=target,
        size=size,
        seconds=time.monotonic() - started,
        attempts=attempts,
        resumed_from=resumed_from,
        server_name=server_name,
    )


def _safe_refresh(refresh: Callable[[], str], error: Exception) -> str:
    try:
        return refresh()
    except TransferError:
        raise
    except Exception as exc:  # noqa: BLE001 - doorgeven als nette fout
        raise TransferError("onbekend", f"Nieuwe downloadlink aanvragen mislukte: {exc}") from exc


def cleanup_partials(directory: Path) -> int:
    """Ruim lege .part-bestanden op die niet meer bij een wachtende download horen."""

    removed = 0
    directory = Path(directory)
    if not directory.exists():
        return 0
    for path in directory.glob(f"*{PART_SUFFIX}"):
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
