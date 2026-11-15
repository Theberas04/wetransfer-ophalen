"""Bronnen periodiek controleren: GitHub, mailbox en de wachtmap.

Elke bron is een functie die ``check_once()``-achtig werk doet en een samenvatting
teruggeeft. Deze loper zorgt dat ze op hun eigen tussenpoos draaien, dat ze niet
dubbel lopen en dat een fout in de ene bron de andere niet stopt.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

MIN_INTERVAL = 1  # minuten


class Source:
    """Eén bron met een eigen tussenpoos en toestand."""

    def __init__(
        self,
        name: str,
        check: Callable[[], dict[str, Any]],
        interval_minutes: int,
        label: str = "",
    ) -> None:
        self.name = name
        self.check = check
        self.interval = max(MIN_INTERVAL, int(interval_minutes or MIN_INTERVAL)) * 60
        self.label = label or name
        self.last_run = 0.0
        self.last_error = ""
        self.last_summary = ""
        self.running = False
        self._lock = threading.Lock()

    def due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return not self.running and (now - self.last_run) >= self.interval

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "interval_minutes": self.interval // 60,
            "last_run": self.last_run,
            "last_run_iso": time.strftime("%d-%m-%Y %H:%M", time.localtime(self.last_run)) if self.last_run else "",
            "last_error": self.last_error,
            "last_summary": self.last_summary,
            "running": self.running,
        }


class SourceRunner:
    """Laat alle bronnen op hun eigen ritme lopen."""

    def __init__(self, log: Optional[Callable[[str], None]] = None, tick: float = 5.0) -> None:
        self.log = log
        self.tick = max(1.0, float(tick))
        self.sources: dict[str, Source] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, name: str, check: Callable[[], dict[str, Any]], interval_minutes: int, label: str = "") -> None:
        self.sources[name] = Source(name, check, interval_minutes, label)

    def unregister(self, name: str) -> None:
        self.sources.pop(name, None)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running or not self.sources:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bronnen", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def check_now(self, name: str) -> dict[str, Any]:
        """Controleer één bron meteen; veilig om vanaf het web aan te roepen."""

        source = self.sources.get(name)
        if source is None:
            return {"ok": False, "error": f"Onbekende bron: {name}"}
        if source.running:
            return {"ok": False, "error": f"'{source.label}' is al bezig."}
        return self._run(source)

    def check_all(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name in list(self.sources):
            results[name] = self.check_now(name)
        return {"ok": True, "results": results}

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "sources": [source.status() for source in self.sources.values()],
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            for source in list(self.sources.values()):
                if self._stop.is_set():
                    return
                if source.due():
                    self._run(source)
            for _ in range(int(self.tick * 2)):
                if self._stop.is_set():
                    return
                time.sleep(0.5)

    def _run(self, source: Source) -> dict[str, Any]:
        if not source._lock.acquire(blocking=False):  # noqa: SLF001 - eigen slot
            return {"ok": False, "error": f"'{source.label}' is al bezig."}
        source.running = True
        try:
            result = source.check() or {}
        except Exception as exc:  # noqa: BLE001 - een bron mag nooit de app laten vallen
            source.last_error = f"{exc.__class__.__name__}: {exc}"
            result = {"ok": False, "error": source.last_error}
        finally:
            source.running = False
            source.last_run = time.time()
            source._lock.release()  # noqa: SLF001
        errors = result.get("errors") or []
        if errors:
            source.last_error = "; ".join(str(entry) for entry in errors)
        elif not result.get("ok", True) and result.get("error"):
            source.last_error = str(result["error"])
        else:
            source.last_error = ""
        source.last_summary = _summary(source.name, result)
        return {"ok": not source.last_error, **result}


def _summary(name: str, result: dict[str, Any]) -> str:
    links = result.get("links") or []
    assets = result.get("assets") or []
    messages = result.get("messages")
    pieces: list[str] = []
    if isinstance(messages, int):
        pieces.append(f"{messages} bericht(en) gelezen")
    if links:
        pieces.append(f"{len(links)} link(s) gevonden")
    if assets:
        pieces.append(f"{len(assets)} releasebestand(en) gevonden")
    if not pieces:
        pieces.append("niets nieuws")
    return ", ".join(pieces)
