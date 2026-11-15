"""De wachtrij: links één voor één ophalen en daarna melden via Slack."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import notify
from .downloader import DownloadCancelled, download
from .store import AppLog, QueueStore, Settings
from .wetransfer import ShareLink, TransferError, TransferInfo, WetransferClient, extract_links

PENDING = "wachtend"
RUNNING = "ophalen"
DONE = "klaar"
FAILED = "mislukt"
CANCELLED = "gestopt"

ACTIVE_STATES = (PENDING, RUNNING)


@dataclass
class QueueItem:
    url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    password: str = ""
    state: str = PENDING
    name: str = ""
    size: Optional[int] = None
    received: int = 0
    speed: float = 0.0
    path: str = ""
    error: str = ""
    hint: str = ""
    added_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    seconds: float = 0.0
    source: str = "handmatig"
    attempts: int = 0
    skipped: bool = False
    direct: bool = False  # True voor een gewoon bestand (bijvoorbeeld een releasebestand van GitHub)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["added_at_iso"] = time.strftime("%d-%m-%Y %H:%M", time.localtime(self.added_at))
        if self.finished_at:
            data["finished_at_iso"] = time.strftime("%d-%m-%Y %H:%M", time.localtime(self.finished_at))
        else:
            data["finished_at_iso"] = ""
        data["percent"] = round(100 * self.received / self.size, 1) if self.size else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueItem":
        allowed = {key: value for key, value in (data or {}).items() if key in cls.__dataclass_fields__}
        item = cls(**allowed)
        if item.state == RUNNING:
            item.state = PENDING
        return item


class DownloadQueue:
    """Verwerkt de wachtrij in één achtergrondthread (WeTransfer houdt niet van parallel)."""

    def __init__(
        self,
        settings_provider: Callable[[], Settings],
        client_factory: Callable[[], WetransferClient] = WetransferClient,
        log: Optional[AppLog] = None,
        store: Optional[QueueStore] = None,
        on_change: Optional[Callable[[], None]] = None,
        allow_hosts: tuple[str, ...] = (),
        skip_webhook_host_check: bool = False,
        on_finished: Optional[Callable[["QueueItem"], None]] = None,
        direct_session: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.settings_provider = settings_provider
        self.client_factory = client_factory
        self.log = log or AppLog()
        self.store = store or QueueStore()
        self.on_change = on_change
        self.allow_hosts = tuple(allow_hosts)
        # Alleen voor tests en de zelftest: dan mag de webhook naar de nabootser wijzen.
        self.skip_webhook_host_check = skip_webhook_host_check
        self.on_finished = on_finished
        # Levert een requests-sessie voor rechtstreekse bestanden (bijvoorbeeld met
        # een GitHub-sleutel erin). Nooit nodig voor gewone WeTransfer-links.
        self.direct_session = direct_session
        self.items: list[QueueItem] = []
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[WetransferClient] = None
        self._batch: list[str] = []

    # -- wachtrij beheren -------------------------------------------------
    def load(self) -> None:
        loaded = [QueueItem.from_dict(data) for data in self.store.load()]
        with self._lock:
            self.items = loaded
        pending = [item for item in self.items if item.state == PENDING]
        if pending:
            self.log.write(f"{len(pending)} download(s) uit de vorige sessie opnieuw in de wachtrij gezet.")

    def save(self) -> None:
        with self._lock:
            self.store.save([item.as_dict() for item in self.items])

    def add_links(self, text: str, password: str = "", source: str = "handmatig") -> tuple[int, list[str]]:
        links = extract_links(text, self.allow_hosts)
        known = {item.url.lower() for item in self.items if item.state in ACTIVE_STATES}
        added: list[QueueItem] = []
        skipped: list[str] = []
        for link in links:
            if link.lower() in known:
                skipped.append(link)
                continue
            item = QueueItem(url=link, password=password, source=source)
            with self._lock:
                self.items.append(item)
            added.append(item)
        if added:
            self.log.write(f"{len(added)} link(s) toegevoegd aan de wachtrij.")
            self.save()
            self._changed()
        return len(added), skipped

    def add_direct(self, url: str, name: str = "", source: str = "github") -> tuple[bool, str]:
        """Zet een gewoon bestand in de wachtrij (geen WeTransfer-link).

        Wordt gebruikt voor de bestanden uit een nieuwe GitHub-release: dat is een
        normale download, maar hij hoort wel in dezelfde wachtrij met dezelfde
        meldingen.
        """

        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return False, "Dit is geen webadres."
        key = url.lower()
        with self._lock:
            if any(item.url.lower() == key and item.state in ACTIVE_STATES for item in self.items):
                return False, "Dit bestand staat al in de wachtrij."
            self.items.append(QueueItem(url=url, name=name, source=source, direct=True))
        self.log.write(f"Bestand van {source} in de wachtrij: {name or url}")
        self.save()
        self._changed()
        return True, ""

    def retry(self, item_id: str) -> bool:
        with self._lock:
            for item in self.items:
                if item.id == item_id:
                    item.state = PENDING
                    item.error = ""
                    item.hint = ""
                    item.received = 0
                    item.speed = 0.0
                    item.seconds = 0.0
                    item.finished_at = None
                    self.save()
                    self._changed()
                    return True
        return False

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [item for item in self.items if item.id != item_id]
            changed = len(self.items) != before
        if changed:
            self.save()
            self._changed()
        return changed

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self.items)
            self.items = [item for item in self.items if item.state in ACTIVE_STATES]
            removed = before - len(self.items)
        if removed:
            self.save()
            self._changed()
        return removed

    # -- verwerken ---------------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            if not any(item.state == PENDING for item in self.items):
                return False
            self._stop.clear()
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name="download-worker", daemon=True)
            self._thread.start()
        self.log.write("Wachtrij gestart.")
        return True

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self.log.write("Wachtrij gestopt; de huidige download gaat verder bij de volgende start.")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for item in self.items if item.state == PENDING)

    def _run(self) -> None:
        self._batch = []
        try:
            while not self._stop.is_set():
                item = self._next_pending()
                if item is None:
                    break
                self._process(item)
        finally:
            self._finish_batch()
            self._changed()

    def _next_pending(self) -> Optional[QueueItem]:
        with self._lock:
            for item in self.items:
                if item.state == PENDING:
                    return item
        return None

    def _process(self, item: QueueItem) -> None:
        settings = self.settings_provider()
        self._batch.append(item.id)
        item.state = RUNNING
        item.error = ""
        item.hint = ""
        item.received = 0
        item.attempts += 1
        self._changed()

        directory = Path(settings.download_dir)
        self.log.write(f"Ophalen gestart: {item.url}")

        def progress(received: int, total: Optional[int], speed: float) -> None:
            item.received = received
            if total:
                item.size = total
            item.speed = speed
            self._changed()

        try:
            if item.direct:
                # Een gewoon bestand, bijvoorbeeld een bestand uit een nieuwe GitHub-release.
                info = TransferInfo(
                    link=ShareLink(
                        url=item.url, short_url=item.url, transfer_id="", security_hash="", direct=True
                    ),
                    state="downloadable",
                    recommended_filename=item.name,
                    size=item.size,
                )
                result = download(
                    info,
                    item.url,
                    directory,
                    on_progress=progress,
                    cancel=self._cancel,
                    session=self.direct_session() if self.direct_session else None,
                    name=item.name,
                )
            else:
                if self._client is None:
                    self._client = self.client_factory()
                info, direct_link = self._client.resolve(item.url, password=item.password)
                item.name = info.recommended_filename or item.name
                item.size = info.size or item.size
                if not item.name:
                    item.name = Path(direct_link).name or "transfer"
                self._changed()

                def refresh() -> str:
                    assert self._client is not None
                    self.log.write(f"Nieuwe downloadlink aanvragen voor {item.url}")
                    _, fresh = self._client.resolve(item.url, password=item.password)
                    return fresh

                result = download(
                    info,
                    direct_link,
                    directory,
                    refresh=refresh,
                    on_progress=progress,
                    cancel=self._cancel,
                    session=self._client.session if self._client else None,
                )
            item.state = DONE
            item.path = str(result.path)
            item.size = result.size
            item.received = result.size
            item.seconds = result.seconds
            item.skipped = result.skipped
            item.finished_at = time.time()
            self.log.write(
                f"Klaar: {result.path.name} ({result.size} bytes, {result.seconds:.1f} s"
                + (f", hervat na {result.resumed_from} bytes" if result.resumed_from else "")
                + ")"
            )
        except DownloadCancelled:
            item.state = PENDING
            item.speed = 0.0
            self.log.write(f"Download gestopt: {item.url}")
        except TransferError as exc:
            item.state = FAILED
            item.error = exc.message
            item.hint = exc.hint
            item.finished_at = time.time()
            self.log.write(f"Mislukt ({exc.code}): {exc.message} {exc.hint}".strip())
        except Exception as exc:  # noqa: BLE001 - altijd zichtbaar maken in de app
            item.state = FAILED
            item.error = f"Onverwachte fout: {exc.__class__.__name__}: {exc}"
            item.finished_at = time.time()
            self.log.write(f"Mislukt (onverwacht): {exc!r}")
        self._notify(item, settings)
        if self.on_finished:
            try:
                self.on_finished(item)
            except Exception:  # noqa: BLE001 - een geluidje mag nooit de wachtrij breken
                pass
        self.save()
        self._changed()

    def _notify(self, item: QueueItem, settings: Settings) -> None:
        mode = settings.notify_mode
        if mode == "uit":
            return
        if item.state == DONE and mode in ("altijd",):
            self._send(notify.done_text(item, settings.download_dir))
        elif item.state == FAILED and mode in ("altijd", "alleen_bij_fouten"):
            self._send(notify.failed_text(item))

    def _finish_batch(self) -> None:
        settings = self.settings_provider()
        if settings.notify_mode == "uit" or not self._batch:
            return
        finished = [item for item in self.items if item.id in self._batch]
        if len(finished) <= 1:
            return
        if all(item.state == DONE for item in finished):
            self._send(notify.summary_text(finished))

    def _send(self, text: str) -> None:
        webhook = self.settings_provider().slack_webhook
        ok, error = notify.send_slack(
            webhook, text, skip_host_check=self.skip_webhook_host_check
        )
        if ok:
            self.log.write("Slack-melding verstuurd.")
        else:
            self.log.write(f"Slack-melding mislukt: {error}")

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass

    def state(self) -> dict[str, Any]:
        with self._lock:
            items = [item.as_dict() for item in self.items]
        return {
            "items": items,
            "running": self.running,
            "pending": self.pending_count(),
        }
