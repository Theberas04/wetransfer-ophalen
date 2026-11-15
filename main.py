"""WeTransfer Ophalen — start de app, of draait de zelftest met --selftest."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from wtdl import notify  # noqa: E402
from wtdl.queue import DownloadQueue  # noqa: E402
from wtdl.selftest import run_selftest  # noqa: E402
from wtdl.store import (  # noqa: E402
    APP_NAME,
    AppLog,
    QueueStore,
    Settings,
    SettingsStore,
    config_directory,
    open_in_explorer,
    set_windows_autostart,
    windows_autostart_enabled,
)
from wtdl.version import __version__  # noqa: E402
from wtdl.watcher import FolderWatcher  # noqa: E402
from wtdl.wetransfer import WetransferClient  # noqa: E402

APP_DIR = Path(__file__).resolve().parent


def ui_path(name: str = "index.html") -> Path:
    """Vind de ui-map, ook in een PyInstaller-build (dan staat alles in _internal)."""

    candidates = [
        APP_DIR / "ui" / name,
        APP_DIR / "_internal" / "ui" / name,
        Path(getattr(sys, "_MEIPASS", APP_DIR)) / "ui" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _dialog_kind(name: str) -> int:
    """Het dialoogtype van pywebview, met dezelfde waarden als pywebview zelf.

    De waarden 10 (openen) en 20 (map kiezen) staan ook in pywebview; door ze hier
    als terugvalwaarde te gebruiken blijft de app testbaar zonder pywebview.
    """

    fallback = {"OPEN_DIALOG": 10, "FOLDER_DIALOG": 20}.get(name, 10)
    try:
        import webview  # noqa: PLC0415 - alleen nodig bij een echt venster

        return int(getattr(webview, name, fallback))
    except ImportError:
        return fallback


def ui_thread_delegate(callback: Callable[[], None]) -> Any:
    """Wikkel een Python-functie in de .NET-delegate die WinForms' Control.Invoke wil.

    Geeft None terug als pythonnet ontbreekt, zodat de aanroeper kan terugvallen.
    """

    try:
        from System import Action  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Action(callback)


class DesktopApi:
    """Alles wat de webpagina via ``window.pywebview.api`` kan aanroepen."""

    def __init__(self, config_dir: Optional[Path] = None, window: Any = None) -> None:
        self.config_dir = Path(config_dir) if config_dir else config_directory()
        self.window = window
        self.settings_store = SettingsStore(self.config_dir / "settings.json")
        self.settings: Settings = self.settings_store.load()
        self.log = AppLog(self.config_dir / "app.log")
        if self.settings_store.load_error:
            self.log.write(self.settings_store.load_error)
        self.queue = DownloadQueue(
            settings_provider=lambda: self.settings,
            client_factory=WetransferClient,
            log=self.log,
            store=QueueStore(self.config_dir / "queue.json"),
            on_change=self._on_change,
            on_finished=self._on_item_finished,
        )
        self.queue.load()
        self.watcher: Optional[FolderWatcher] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_state_push = 0.0

    # -- hulpjes -----------------------------------------------------------
    def _on_item_finished(self, item: Any) -> None:
        """Geluidje bij een geslaagde download en de map openen als alles klaar is."""

        from wtdl.queue import DONE  # noqa: PLC0415 - kleine import, geen cyclus

        if getattr(item, "state", "") != DONE:
            return
        if self.settings.sound_on_finish:
            threading.Thread(target=self._beep, name="piep", daemon=True).start()
        if self.settings.open_folder_after and self.queue.pending_count() == 0:
            open_in_explorer(self.settings.download_dir)

    @staticmethod
    def _beep() -> None:
        try:
            import winsound  # noqa: PLC0415 - alleen op Windows

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:  # noqa: BLE001 - geen geluid is geen fout
            pass

    def _on_change(self) -> None:
        # De pagina haalt de status zelf op; hier alleen een duwtje voor de voortgangsbalk.
        now = time.monotonic()
        if self.window is None or now - self._last_state_push < 0.5:
            return
        self._last_state_push = now
        try:
            self.window.evaluate_js("window.app && window.app.refresh && window.app.refresh()")
        except Exception:  # noqa: BLE001 - een gesloten venster is geen fout
            pass

    def _run_dialog(self, opener: Callable[[], Any]) -> Any:
        """Open een systeemvenster op de GUI-thread (WinForms weigert anders)."""

        form = getattr(self.window, "native", None)
        invoke = getattr(form, "Invoke", None)
        if invoke is None:
            return opener()
        outcome: list[Any] = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                outcome.append(opener())
            except BaseException as exc:  # noqa: BLE001 - gaat terug naar de aanroeper
                failure.append(exc)

        delegate = ui_thread_delegate(run)
        if delegate is None:
            return opener()
        invoke(delegate)
        if failure:
            raise failure[0]
        return outcome[0] if outcome else None

    def _choose_folder(self, initial: str = "") -> list[str]:
        return self._run_dialog(
            lambda: self.window.create_file_dialog(_dialog_kind("FOLDER_DIALOG"), directory=initial or "")
        ) or []

    def _choose_file(self) -> list[str]:
        return self._run_dialog(
            lambda: self.window.create_file_dialog(
                _dialog_kind("OPEN_DIALOG"),
                allow_multiple=False,
                file_types=("Linkbestanden (*.txt;*.eml;*.url;*.html)", "Alle bestanden (*.*)"),
            )
        ) or []

    def _start_watcher(self) -> None:
        self._stop_watcher()
        if not self.settings.watch_enabled:
            return
        folder = self.settings.effective_watch_dir()
        self.watcher = FolderWatcher(
            folder,
            self.settings.watch_interval,
            on_links=self._links_from_folder,
            on_log=self.log.write,
        )
        self._watcher_thread = threading.Thread(target=self.watcher.run, name="map-wachter", daemon=True)
        self._watcher_thread.start()
        self.log.write(f"Wachtmap actief: {folder} (elke {self.settings.watch_interval} s)")

    def _stop_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._watcher_thread = None

    def _links_from_folder(self, links: list[str]) -> None:
        added, _ = self.queue.add_links("\n".join(links), password=self.settings.last_password, source="wachtmap")
        if added:
            self.log.write(f"{added} link(s) uit de wachtmap in de wachtrij gezet.")
            if self.settings.auto_start:
                self.queue.start()

    # -- wat de pagina aanroept -------------------------------------------
    def get_state(self) -> dict[str, Any]:
        state = self.queue.state()
        return {
            "version": __version__,
            "app_name": APP_NAME,
            "config_dir": str(self.config_dir),
            "settings": self.settings.as_dict(),
            "watch_dir_effective": self.settings.effective_watch_dir(),
            "autostart_registered": windows_autostart_enabled(),
            "log": self.log.tail(150),
            "platform": os.name,
            **state,
        }

    def add_links(self, text: str = "", password: str = "") -> dict[str, Any]:
        try:
            added, skipped = self.queue.add_links(text or "", password=password or "")
        except Exception as exc:  # noqa: BLE001
            self.log.write(f"Links toevoegen mislukte: {exc!r}")
            return {"ok": False, "error": f"Toevoegen mislukte: {exc}"}
        if password:
            self.settings.last_password = password
            self.settings_store.save(self.settings)
        message = ""
        if added:
            message = f"{added} link(s) toegevoegd."
            if skipped:
                message += f" {len(skipped)} stond(en) al in de wachtrij."
            if self.settings.auto_start:
                self.queue.start()
        elif skipped:
            message = "Deze link staat al in de wachtrij."
        else:
            message = "Geen WeTransfer-link gevonden in de tekst."
        return {"ok": True, "added": added, "skipped": len(skipped), "message": message}

    def import_file(self, path: str = "") -> dict[str, Any]:
        """Lees links uit een bestand (bijvoorbeeld een opgeslagen e-mail)."""

        target = Path(path) if path else None
        if target is None or not target.exists():
            return {"ok": False, "error": "Kies eerst een bestand met WeTransfer-links."}
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return {"ok": False, "error": f"Bestand lezen mislukte: {exc}"}
        result = self.add_links(text)
        if result.get("ok"):
            result["message"] = f"{target.name}: {result.get('message', '')}"
        return result

    def choose_folder(self, kind: str = "download") -> dict[str, Any]:
        try:
            selection = self._choose_folder(self.settings.download_dir)
        except Exception as exc:  # noqa: BLE001
            self.log.write(f"Mapvenster openen mislukte: {exc!r}")
            return {"ok": False, "error": f"Mapvenster openen mislukte: {exc}"}
        if not selection:
            return {"ok": False, "cancelled": True}
        chosen = str(selection[0])
        if kind == "watch":
            self.settings.watch_dir = chosen
            self.settings.watch_enabled = True
            self._start_watcher()
        else:
            self.settings.download_dir = chosen
        self.settings_store.save(self.settings)
        self.log.write(f"Map gekozen ({kind}): {chosen}")
        return {"ok": True, "path": chosen}

    def choose_link_file(self) -> dict[str, Any]:
        try:
            selection = self._choose_file()
        except Exception as exc:  # noqa: BLE001
            self.log.write(f"Bestandsvenster openen mislukte: {exc!r}")
            return {"ok": False, "error": f"Bestandsvenster openen mislukte: {exc}"}
        if not selection:
            return {"ok": False, "cancelled": True}
        return self.import_file(str(selection[0]))

    def open_folder(self, path: str = "") -> dict[str, Any]:
        target = path or self.settings.download_dir
        ok, error = open_in_explorer(target)
        if not ok:
            self.log.write(f"Map openen mislukte: {error}")
        return {"ok": ok, "error": error}

    def open_log_file(self) -> dict[str, Any]:
        ok, error = open_in_explorer(str(self.log.path.parent))
        return {"ok": ok, "error": error, "path": str(self.log.path)}

    def save_settings(self, values: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        values = values or {}
        before = self.settings.as_dict()
        for key, value in values.items():
            if hasattr(self.settings, key) and value is not None:
                setattr(self.settings, key, value)
        self.settings.watch_interval = max(5, int(self.settings.watch_interval or 20))
        if not self.settings_store.save(self.settings):
            return {"ok": False, "error": "Instellingen opslaan mislukte (schijf vol of geen rechten?)."}
        if before.get("autostart_with_windows") != self.settings.autostart_with_windows:
            ok, error = set_windows_autostart(self.settings.autostart_with_windows)
            if not ok:
                self.log.write(f"Automatisch starten aanpassen mislukte: {error}")
                return {"ok": False, "error": error}
            self.log.write(
                "App start voortaan automatisch met Windows."
                if self.settings.autostart_with_windows
                else "Automatisch starten uitgezet."
            )
        if (
            before.get("watch_enabled") != self.settings.watch_enabled
            or before.get("watch_dir") != self.settings.watch_dir
            or before.get("watch_interval") != self.settings.watch_interval
        ):
            self._start_watcher()
        self.log.write("Instellingen opgeslagen.")
        return {"ok": True, "settings": self.settings.as_dict()}

    def test_slack(self, webhook: str = "") -> dict[str, Any]:
        target = (webhook or self.settings.slack_webhook or "").strip()
        ok, error = notify.send_slack(target, notify.test_text())
        if ok:
            self.log.write("Testbericht naar Slack verstuurd.")
        else:
            self.log.write(f"Testbericht mislukte: {error}")
        return {"ok": ok, "error": error}

    def start_queue(self) -> dict[str, Any]:
        started = self.queue.start()
        if not started and self.queue.pending_count() == 0:
            return {"ok": False, "message": "Er staat niets in de wachtrij."}
        return {"ok": True, "started": started}

    def stop_queue(self) -> dict[str, Any]:
        self.queue.stop()
        return {"ok": True}

    def retry_item(self, item_id: str = "") -> dict[str, Any]:
        ok = self.queue.retry(item_id)
        if ok and self.settings.auto_start:
            self.queue.start()
        return {"ok": ok}

    def remove_item(self, item_id: str = "") -> dict[str, Any]:
        return {"ok": self.queue.remove(item_id)}

    def clear_finished(self) -> dict[str, Any]:
        return {"ok": True, "removed": self.queue.clear_finished()}

    def scan_watch_folder(self) -> dict[str, Any]:
        if self.watcher is None:
            return {"ok": False, "error": "De wachtmap staat uit."}
        result = self.watcher.scan_once()
        return {"ok": True, "links": result.links, "files": result.files, "errors": result.errors}

    def shutdown(self) -> dict[str, Any]:
        self._stop_watcher()
        self.queue.save()
        self.log.write("App afgesloten.")
        return {"ok": True}


def run_gui(api: DesktopApi, debug: bool = False) -> int:
    import webview  # noqa: PLC0415 - pas nodig als er echt een venster komt

    window = webview.create_window(
        APP_NAME,
        str(ui_path()),
        js_api=api,
        width=1180,
        height=800,
        min_size=(1000, 640),
        background_color="#f4f7fb",
    )
    api.window = window

    def on_loaded() -> None:
        api.log.write(f"Versie {__version__} gestart. Instellingen in {api.config_dir}")
        api._start_watcher()
        if api.settings.auto_start and api.queue.pending_count():
            api.queue.start()

    window.events.loaded += on_loaded
    try:
        webview.start(debug=debug)
    finally:
        api.shutdown()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        index = argv.index("--selftest")
        report = argv[index + 1] if len(argv) > index + 1 else "selftest-report.json"
        return run_selftest(report, ui_path=ui_path(), core_only="--core-only" in argv)
    if "--version" in argv:
        print(__version__)
        return 0
    debug = "--debug" in argv
    api = DesktopApi()
    return run_gui(api, debug=debug)


if __name__ == "__main__":
    sys.exit(main())
