"""Een map in de gaten houden op opgeslagen WeTransfer-links.

Handig voor links die per e-mail binnenkomen: sla de e-mail (of alleen de link)
op als bestand in de map en de app pakt hem automatisch op. Verwerkte bestanden
gaan naar de submap ``verwerkt`` zodat ze niet nog een keer worden opgepakt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .wetransfer import extract_links

PROCESSED_DIR = "verwerkt"
TEXT_SUFFIXES = {
    ".txt", ".url", ".webloc", ".eml", ".html", ".htm", ".md", ".log", ".json", ".csv", ".nfo",
}
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass
class ScanResult:
    links: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()[:MAX_FILE_BYTES]
    except OSError as exc:
        raise OSError(f"{path.name}: {exc}") from exc
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def scan_folder(
    folder: str | Path, keep_files: bool = False, allow_hosts: tuple[str, ...] = ()
) -> ScanResult:
    """Zoek nieuwe WeTransfer-links in de map en zet verwerkte bestanden apart."""

    result = ScanResult()
    root = Path(folder)
    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(f"Wachtmap aanmaken mislukte: {exc}")
            return result

    processed = root / PROCESSED_DIR
    for path in sorted(root.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            result.errors.append(f"{path.name} is te groot om te lezen en is overgeslagen.")
            continue
        try:
            text = _read_text(path)
        except OSError as exc:
            result.errors.append(str(exc))
            continue
        found = extract_links(text, allow_hosts)
        if not found:
            continue
        result.links.extend(found)
        result.files.append(path.name)
        if keep_files:
            continue
        try:
            processed.mkdir(exist_ok=True)
            target = processed / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
            if target.exists():
                target = processed / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() % 1000)}-{path.name}"
            path.replace(target)
        except OSError as exc:
            result.errors.append(f"{path.name} kon niet naar '{PROCESSED_DIR}' worden verplaatst: {exc}")
    return result


class FolderWatcher:
    """Roept ``on_links`` aan met nieuwe links die in de wachtmap verschijnen."""

    def __init__(
        self, folder: str, interval: int, on_links, on_log=None, allow_hosts: tuple[str, ...] = ()
    ) -> None:
        self.folder = folder
        self.interval = max(5, int(interval))
        self.on_links = on_links
        self.on_log = on_log
        self.allow_hosts = tuple(allow_hosts)
        self._seen: set[str] = set()
        self._stop = False
        self._thread: Optional[object] = None

    def scan_once(self) -> ScanResult:
        result = scan_folder(self.folder, allow_hosts=self.allow_hosts)
        fresh = [link for link in result.links if link.lower() not in self._seen]
        for link in fresh:
            self._seen.add(link.lower())
        if fresh and self.on_links:
            self.on_links(fresh)
        if result.errors and self.on_log:
            for message in result.errors:
                self.on_log(f"Wachtmap: {message}")
        return result

    def run(self) -> None:
        import threading  # noqa: PLC0415 - alleen nodig in de achtergrondthread

        self._thread = threading.current_thread()
        while not self._stop:
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - de wachter mag nooit sterven
                if self.on_log:
                    self.on_log(f"Wachtmap: onverwachte fout: {exc}")
            for _ in range(self.interval * 2):
                if self._stop:
                    return
                time.sleep(0.5)

    def stop(self) -> None:
        self._stop = True
