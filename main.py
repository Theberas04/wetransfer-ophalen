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
from wtdl.github import GithubClient, GithubWatcher, parse_repos  # noqa: E402
from wtdl.mailbox import MailboxConfig, MailboxWatcher  # noqa: E402
from wtdl.queue import DownloadQueue  # noqa: E402
from wtdl.selftest import run_selftest  # noqa: E402
from wtdl.server import WebServer, local_ip, new_code  # noqa: E402
from wtdl.sources import SourceRunner  # noqa: E402
from wtdl.store import (  # noqa: E402
    APP_NAME,
    DEFAULT_GITHUB_REPO,
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

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        window: Any = None,
        allow_hosts: tuple[str, ...] = (),
        skip_webhook_host_check: bool = False,
        client_factory: Optional[Callable[[], WetransferClient]] = None,
    ) -> None:
        self.config_dir = Path(config_dir) if config_dir else config_directory()
        self.window = window
        # Alleen voor tests en de zelftest: dan geldt een link op 127.0.0.1 ook als
        # WeTransfer-link, zodat de hele keten tegen een nabootser te bewijzen is.
        self.allow_hosts = tuple(allow_hosts)
        # Idem: in de zelftest wijst de Slack-webhook naar de nabootser.
        self.skip_webhook_host_check = skip_webhook_host_check
        # Idem: in de zelftest wijst de WeTransfer-client naar de nabootser.
        self.client_factory = client_factory or WetransferClient
        self.settings_store = SettingsStore(self.config_dir / "settings.json")
        self.settings: Settings = self.settings_store.load()
        self.log = AppLog(self.config_dir / "app.log")
        if self.settings_store.load_error:
            self.log.write(self.settings_store.load_error)
        self.queue = DownloadQueue(
            settings_provider=lambda: self.settings,
            client_factory=self.client_factory,
            log=self.log,
            store=QueueStore(self.config_dir / "queue.json"),
            on_change=self._on_change,
            on_finished=self._on_item_finished,
            direct_session=self._direct_session,
            allow_hosts=self.allow_hosts,
            skip_webhook_host_check=self.skip_webhook_host_check,
        )
        self.queue.load()
        self.watcher: Optional[FolderWatcher] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self.github: Optional[GithubWatcher] = None
        self.mailbox: Optional[MailboxWatcher] = None
        # Haken voor de zelftest: dan praten GitHub en de mailbox met een nabootser
        # in plaats van met het echte internet.
        self.github_client_factory: Optional[Callable[[], Any]] = None
        self.mailbox_client_factory: Optional[Callable[[MailboxConfig], Any]] = None
        self.sources = SourceRunner(log=self.log.write)
        self.server: Optional[WebServer] = None
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
        self._links_from_source(links, "wachtmap")

    def _links_from_source(self, links: list[str], source: str = "handmatig") -> None:
        """Links uit een bron (wachtmap, GitHub, mailbox) in de wachtrij zetten."""

        if not links:
            return
        added, _ = self.queue.add_links(
            "\n".join(links), password=self.settings.last_password, source=source
        )
        if added:
            self.log.write(f"{added} link(s) uit {source} in de wachtrij gezet.")
            if self.settings.auto_start:
                self.queue.start()

    def _asset_from_github(self, asset: dict[str, Any]) -> None:
        """Een bestand uit een nieuwe GitHub-release in de wachtrij zetten."""

        ok, error = self.queue.add_direct(
            str(asset.get("url") or ""), str(asset.get("name") or ""), source="github"
        )
        if not ok:
            self.log.write(f"Releasebestand overgeslagen: {error}")
        elif self.settings.auto_start:
            self.queue.start()

    def _direct_session(self) -> Any:
        """Sessie voor rechtstreekse bestanden, met de GitHub-sleutel als die er is."""

        import requests  # noqa: PLC0415 - alleen nodig als er echt gedownload wordt

        session = requests.Session()
        token = (self.settings.github_token or "").strip()
        if token:
            session.headers.update(
                {"Authorization": f"Bearer {token}", "User-Agent": "WeTransfer-Ophalen"}
            )
        return session

    def _mailbox_config(self) -> MailboxConfig:
        return MailboxConfig(
            host=self.settings.mailbox_host,
            port=self.settings.mailbox_port,
            user=self.settings.mailbox_user,
            password=self.settings.mailbox_password,
            folder=self.settings.mailbox_folder,
            sender=self.settings.mailbox_sender,
            days=self.settings.mailbox_days,
            use_ssl=True,
        )

    def _build_github(self) -> Optional[GithubWatcher]:
        repos = parse_repos(self.settings.github_repos)
        if not repos:
            return None
        return GithubWatcher(
            repos,
            self.config_dir / "github.json",
            log=self.log.write,
            on_links=self._links_from_source,
            on_asset=self._asset_from_github,
            client=self.github_client_factory()
            if self.github_client_factory
            else GithubClient(self.settings.github_token),
            watch_releases=self.settings.github_watch_releases,
            watch_links=self.settings.github_watch_links,
            download_releases=self.settings.github_download_releases,
            allow_hosts=self.allow_hosts,
        )

    def _start_sources(self) -> None:
        """Zet de bronnen op die in de instellingen aanstaan."""

        self._stop_sources()
        self.sources = SourceRunner(log=self.log.write)
        if self.settings.github_enabled:
            watcher = self._build_github()
            if watcher is None:
                self.log.write("GitHub staat aan, maar er is geen repo ingevuld.")
            else:
                self.github = watcher
                self.sources.register(
                    "github", watcher.check_once, self.settings.github_interval, "GitHub"
                )
        if self.settings.mailbox_enabled:
            config = self._mailbox_config()
            problem = config.problem()
            if problem:
                self.log.write(f"Mailbox staat aan maar is nog niet compleet: {problem}")
            else:
                self.mailbox = MailboxWatcher(
                    config,
                    self.config_dir / "mailbox.json",
                    log=self.log.write,
                    on_links=self._links_from_source,
                    client_factory=self.mailbox_client_factory,
                    allow_hosts=self.allow_hosts,
                )
                self.sources.register(
                    "mailbox", self.mailbox.check_once, self.settings.mailbox_interval, "Mailbox"
                )
        if self.sources.sources:
            self.sources.start()
            labels = ", ".join(source.label for source in self.sources.sources.values())
            self.log.write(f"Bronnen actief: {labels}")

    def _stop_sources(self) -> None:
        self.sources.stop()

    def _start_server(self) -> None:
        """Zet de webversie aan (dezelfde pagina op een IP-adres in het netwerk)."""

        self._stop_server()
        if not self.settings.web_enabled:
            return
        if not (self.settings.web_code or "").strip():
            self.settings.web_code = new_code()
            self.settings_store.save(self.settings)
        self.server = WebServer(
            self,
            ui_path().parent,
            host=self.settings.web_host,
            port=self.settings.web_port,
            code=self.settings.web_code,
            log=self.log.write,
        )
        ok, error = self.server.start()
        if not ok:
            self.server = None
            self.log.write(error)

    def _stop_server(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None

    def start_background(self) -> None:
        """Alles wat op de achtergrond hoort te lopen: wachtmap, bronnen, webversie."""

        self._start_watcher()
        self._start_sources()
        self._start_server()
        if self.settings.auto_start and self.queue.pending_count():
            self.queue.start()

    # -- wat de pagina aanroept -------------------------------------------
    def get_state(self) -> dict[str, Any]:
        state = self.queue.state()
        mailbox = self._mailbox_config()
        return {
            "version": __version__,
            "app_name": APP_NAME,
            "config_dir": str(self.config_dir),
            "settings": self.settings.as_dict(),
            "watch_dir_effective": self.settings.effective_watch_dir(),
            "autostart_registered": windows_autostart_enabled(),
            "web": self.web_info(),
            "sources": self.sources.status(),
            "github": self.github.status()
            if self.github
            else {
                "repos": parse_repos(self.settings.github_repos),
                "states": {},
                "last_result": {},
                "default_repo": DEFAULT_GITHUB_REPO,
            },
            "mailbox": self.mailbox.status()
            if self.mailbox
            else {
                "configured": mailbox.ready,
                "problem": mailbox.problem(),
                "host": mailbox.host,
                "user": mailbox.user,
                "folder": mailbox.folder,
                "seen": 0,
                "last_result": {},
            },
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
        nu = self.settings.as_dict()
        bron_sleutels = (
            "github_enabled",
            "github_repos",
            "github_interval",
            "github_watch_releases",
            "github_watch_links",
            "github_download_releases",
            "github_token",
            "mailbox_enabled",
            "mailbox_host",
            "mailbox_port",
            "mailbox_user",
            "mailbox_password",
            "mailbox_folder",
            "mailbox_sender",
            "mailbox_days",
            "mailbox_interval",
        )
        if any(before.get(sleutel) != nu.get(sleutel) for sleutel in bron_sleutels):
            self._start_sources()
        web_sleutels = ("web_enabled", "web_host", "web_port", "web_code")
        if any(before.get(sleutel) != nu.get(sleutel) for sleutel in web_sleutels):
            self._start_server()
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

    # -- bronnen -----------------------------------------------------------
    def web_info(self) -> dict[str, Any]:
        """Waar de webversie te bereiken is (ook als hij uit staat)."""

        if self.server is not None and self.server.running:
            info = self.server.info()
        else:
            info = {
                "enabled": False,
                "address": "",
                "host": self.settings.web_host,
                "port": self.settings.web_port,
                "code": self.settings.web_code,
                "needs_code": bool(self.settings.web_code),
                "started_at": 0.0,
            }
        if not info.get("address"):
            host = local_ip() if self.settings.web_host in ("0.0.0.0", "", "::") else self.settings.web_host
            info["address"] = f"http://{host}:{self.settings.web_port}"
        info["local_ip"] = local_ip()
        info["enabled"] = bool(self.server is not None and self.server.running)
        return info

    def check_github(self) -> dict[str, Any]:
        """Nu meteen naar de GitHub-repo('s) kijken."""

        watcher = self.github or self._build_github()
        if watcher is None:
            return {"ok": False, "error": "Vul eerst een GitHub-repo in (eigenaar/naam)."}
        self.github = watcher
        try:
            result = watcher.check_once()
        except Exception as exc:  # noqa: BLE001 - netjes teruggeven aan de pagina
            self.log.write(f"GitHub controleren mislukte: {exc!r}")
            return {"ok": False, "error": f"GitHub controleren mislukte: {exc}"}
        summary = self._github_summary(result)
        return {"ok": not result.get("errors"), "message": summary, "errors": result.get("errors") or []}

    def _github_summary(self, result: dict[str, Any]) -> str:
        repos = result.get("repos") or []
        links = len(result.get("links") or [])
        assets = len(result.get("assets") or [])
        releases = sum(len(entry.get("new_releases") or []) for entry in repos)
        pieces: list[str] = []
        if releases:
            pieces.append(f"{releases} nieuwe release(s)")
        if assets:
            pieces.append(f"{assets} releasebestand(en) in de wachtrij")
        if links:
            pieces.append(f"{links} WeTransfer-link(s) gevonden")
        if not pieces:
            pieces.append("niets nieuws")
        errors = result.get("errors") or []
        text = ", ".join(pieces) + "."
        if errors:
            text += " " + " ".join(str(entry) for entry in errors)
        return text

    def check_mailbox(self) -> dict[str, Any]:
        """Nu meteen de mailbox lezen op WeTransfer-mails."""

        config = self._mailbox_config()
        problem = config.problem()
        if problem:
            return {"ok": False, "error": problem}
        watcher = MailboxWatcher(
            config,
            self.config_dir / "mailbox.json",
            log=self.log.write,
            on_links=self._links_from_source,
            client_factory=self.mailbox_client_factory,
            allow_hosts=self.allow_hosts,
        )
        if self.mailbox is not None:
            # Al bekeken berichten niet nog een keer opnieuw ophalen.
            watcher.seen = list(self.mailbox.seen)
        try:
            result = watcher.check_once()
        except Exception as exc:  # noqa: BLE001 - netjes teruggeven aan de pagina
            self.log.write(f"Mailbox lezen mislukte: {exc!r}")
            return {"ok": False, "error": f"Mailbox lezen mislukte: {exc}"}
        self.mailbox = watcher
        errors = result.get("errors") or []
        message = (
            f"{result.get('messages', 0)} bericht(en) gelezen, {len(result.get('links') or [])} link(s) gevonden."
        )
        if errors:
            message += " " + " ".join(str(entry) for entry in errors)
        return {
            "ok": not errors,
            "message": message,
            "errors": errors,
            "messages": result.get("messages", 0),
            "links": len(result.get("links") or []),
        }

    def check_all_sources(self) -> dict[str, Any]:
        """Alles in één keer controleren: GitHub, mailbox en de wachtmap."""

        results: dict[str, Any] = {}
        if parse_repos(self.settings.github_repos):
            results["github"] = self.check_github()
        else:
            results["github"] = {"ok": False, "error": "Geen GitHub-repo ingevuld."}
        results["mailbox"] = self.check_mailbox()
        if self.watcher is not None:
            results["wachtmap"] = self.scan_watch_folder()
        return {"ok": True, "results": results}

    def clear_log(self) -> dict[str, Any]:
        self.log.lines.clear()
        self.log.write("Logboek leeggemaakt.")
        return {"ok": True}

    def shutdown(self) -> dict[str, Any]:
        self._stop_watcher()
        self._stop_sources()
        self._stop_server()
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
        api.start_background()

    window.events.loaded += on_loaded
    try:
        webview.start(debug=debug)
    finally:
        api.shutdown()
    return 0


def run_headless(api: DesktopApi) -> int:
    """Alleen de webversie: geen venster, wel de pagina op een IP-adres.

    Handig op een pc of server zonder scherm: de app draait door en je gebruikt
    hem vanaf je telefoon of een andere computer in het netwerk.
    """

    api.log.write(f"Versie {__version__} gestart zonder venster (alleen webversie).")
    api.start_background()
    if api.server is None:
        print("De webversie staat uit. Zet 'web_enabled' aan in de instellingen of start met --serve --web.")
        api.shutdown()
        return 1
    info = api.web_info()
    print(f"Webversie actief op {info['address']}")
    if info.get("needs_code"):
        print(f"Toegangscode: {info['code']}")
    print("Stoppen met Ctrl+C.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStoppen.")
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
    if "--serve" in argv:
        # Zonder venster, alleen de webversie. --web forceert hem aan.
        if "--web" in argv or "--port" in argv or "--host" in argv:
            api.settings.web_enabled = True
        if "--host" in argv:
            api.settings.web_host = argv[argv.index("--host") + 1]
        if "--port" in argv:
            try:
                api.settings.web_port = int(argv[argv.index("--port") + 1])
            except (IndexError, ValueError):
                print("Gebruik --port <nummer>, bijvoorbeeld --port 8756.")
                return 2
        api.settings_store.save(api.settings)
        return run_headless(api)
    return run_gui(api, debug=debug)


if __name__ == "__main__":
    sys.exit(main())
