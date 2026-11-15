"""Instellingen, wachtrij en logboek op schijf (%APPDATA%\\WeTransfer Ophalen)."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_NAME = "WeTransfer Ophalen"
# De repo van de app zelf: die kijkt hij standaard in de gaten (releases + links in issues).
DEFAULT_GITHUB_REPO = "Theberas04/wetransfer-ophalen"


def config_directory() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    return Path.home() / ".config" / "wetransfer-ophalen"


def default_download_directory() -> Path:
    if os.name == "nt":
        profile = Path(os.environ.get("USERPROFILE", Path.home()))
        downloads = profile / "Downloads"
        base = downloads if downloads.exists() else profile
    else:
        base = Path.home() / "Downloads"
    return base / "WeTransfer"


@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: str(default_download_directory()))
    slack_webhook: str = ""
    notify_mode: str = "altijd"  # altijd | alleen_bij_fouten | uit
    watch_enabled: bool = False
    watch_dir: str = ""
    watch_interval: int = 20
    auto_start: bool = True
    open_folder_after: bool = False
    sound_on_finish: bool = True
    autostart_with_windows: bool = False
    last_password: str = ""
    # Webversie: dezelfde pagina op een IP-adres in je netwerk.
    web_enabled: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = 8756
    web_code: str = ""
    # GitHub-bron: nieuwe releases en WeTransfer-links in issues en opmerkingen.
    github_enabled: bool = False
    github_repos: str = DEFAULT_GITHUB_REPO
    github_interval: int = 15  # minuten
    github_watch_releases: bool = True
    github_watch_links: bool = True
    github_download_releases: bool = True
    github_token: str = ""
    # Mailbox-bron: WeTransfer-mails (bijvoorbeeld van aangevraagde opnames) lezen.
    mailbox_enabled: bool = False
    mailbox_host: str = "imap.gmail.com"
    mailbox_port: int = 993
    mailbox_user: str = ""
    mailbox_password: str = ""
    mailbox_folder: str = "INBOX"
    mailbox_sender: str = "wetransfer"
    mailbox_days: int = 30
    mailbox_interval: int = 10  # minuten

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        settings = cls()
        for key, value in (data or {}).items():
            if hasattr(settings, key) and value is not None:
                setattr(settings, key, value)
        settings.watch_interval = max(5, int(settings.watch_interval or 20))
        settings.web_port = min(65535, max(1, int(settings.web_port or 8756)))
        settings.github_interval = max(5, int(settings.github_interval or 15))
        settings.mailbox_interval = max(5, int(settings.mailbox_interval or 10))
        settings.mailbox_days = min(365, max(1, int(settings.mailbox_days or 30)))
        settings.mailbox_port = min(65535, max(1, int(settings.mailbox_port or 993)))
        if not str(settings.github_repos or "").strip():
            settings.github_repos = DEFAULT_GITHUB_REPO
        return settings

    def effective_watch_dir(self) -> str:
        return self.watch_dir or str(default_download_directory().parent / "WeTransfer-links")


class SettingsStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config_directory() / "settings.json"
        self.load_error = ""

    def load(self) -> Settings:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            self.load_error = ""
            return Settings.from_dict(data)
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError) as exc:
            self.load_error = f"Instellingen konden niet worden gelezen: {exc}"
            return Settings()

    def save(self, settings: Settings) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(settings.as_dict(), handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


class QueueStore:
    """Bewaart de wachtrij zodat een herstart niet alles vergeet."""

    MAX_HISTORY = 200

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config_directory() / "queue.json"

    def load(self) -> list[dict[str, Any]]:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    def save(self, items: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"saved_at": time.time(), "items": items[-self.MAX_HISTORY :]}
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


class AppLog:
    """Logboek in het geheugen plus op schijf; 'er gebeurt niets' is anders niet te zien."""

    MAX_LINES = 400

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config_directory() / "app.log"
        self.lines: list[str] = []
        self._listeners: list[Any] = []

    def write(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        self.lines.append(line)
        if len(self.lines) > self.MAX_LINES:
            del self.lines[: len(self.lines) - self.MAX_LINES]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        for listener in list(self._listeners):
            try:
                listener(line)
            except Exception:  # noqa: BLE001 - loggen mag nooit de app laten vallen
                pass

    def tail(self, count: int = 200) -> list[str]:
        return self.lines[-count:]


def open_in_explorer(path: str) -> tuple[bool, str]:
    """Open een map in Verkenner (of de bestandsbeheerder op dit systeem)."""

    target = Path(path)
    if not target.exists():
        return False, f"De map bestaat niet: {path}"
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", str(target)])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        return False, f"Map openen mislukte: {exc}"
    return True, ""


def set_windows_autostart(enabled: bool, executable: str = "") -> tuple[bool, str]:
    """Zet de app wel/niet in de opstartmap van Windows (HKCU, geen rechten nodig)."""

    if os.name != "nt":
        return False, "Automatisch starten is alleen op Windows beschikbaar."
    import winreg  # noqa: PLC0415 - alleen op Windows beschikbaar

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                target = executable or sys.executable
                if getattr(sys, "frozen", False):
                    command = f'"{target}"'
                else:
                    command = f'"{target}" "{Path(sys.argv[0]).resolve()}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        return False, f"Opstartinstelling aanpassen mislukte: {exc}"
    return True, ""


def windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
        ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except (OSError, FileNotFoundError):
        return False
