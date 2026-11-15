"""GitHub in de gaten houden: nieuwe releases én WeTransfer-links in issues.

De app kijkt met de gewone GitHub-API (zonder sleutel) naar de repo's die de
gebruiker opgeeft. Twee dingen worden opgepikt:

* **nieuwe releases** — de bestanden uit een nieuwe release worden opgehaald;
* **WeTransfer-links** — links die in een issue, pull request of opmerking staan.

De eerste controle van een repo legt alleen een ijkpunt vast: bestaande releases
worden onthouden (niet gedownload, anders haalt de app bij het toevoegen van een
repo meteen alle oude versies binnen), maar bestaande links worden wél opgehaald —
daarvoor voegt de gebruiker de repo juist toe.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from .wetransfer import extract_links

API_ROOT = "https://api.github.com"
USER_AGENT = "WeTransfer-Ophalen"
MAX_IDS = 500
MAX_ISSUES = 30
MAX_COMMENTS = 100
TIMEOUT = 20.0


class GithubError(Exception):
    """Fout met een Nederlandse uitleg die in de app te zien is."""


def normalize_repo(value: str) -> str:
    """Maak van een repo-URL of -naam de vorm ``eigenaar/naam``."""

    text = (value or "").strip().rstrip("/")
    lowered = text.lower()
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "https://www.github.com/",
        "github.com/",
        "git@github.com:",
    ):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.lower().endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise GithubError(
            f"'{value}' is geen repo. Gebruik eigenaar/naam, bijvoorbeeld Theberas04/wetransfer-ophalen."
        )
    return f"{parts[0]}/{parts[1]}"


def parse_repos(value: Any) -> list[str]:
    """Lees de ingestelde repo's: een lijst, of tekst met komma's of enters."""

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = [str(entry) for entry in value]
    else:
        raw = str(value).replace("\n", ",").split(",")
    repos: list[str] = []
    for entry in raw:
        entry = entry.strip()
        if not entry:
            continue
        try:
            repo = normalize_repo(entry)
        except GithubError:
            continue
        if repo not in repos:
            repos.append(repo)
    return repos


@dataclass
class RepoState:
    """Wat de app al gezien heeft bij één repo."""

    release_tags: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    link_ids: list[str] = field(default_factory=list)
    initialized: bool = False
    last_check: float = 0.0
    last_error: str = ""
    last_new: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_tags": self.release_tags[-MAX_IDS:],
            "asset_ids": self.asset_ids[-MAX_IDS:],
            "link_ids": self.link_ids[-MAX_IDS:],
            "initialized": self.initialized,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "last_new": self.last_new,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoState":
        state = cls()
        for key in ("release_tags", "asset_ids", "link_ids"):
            value = data.get(key)
            if isinstance(value, list):
                setattr(state, key, [str(entry) for entry in value])
        state.initialized = bool(data.get("initialized"))
        state.last_check = float(data.get("last_check") or 0.0)
        state.last_error = str(data.get("last_error") or "")
        state.last_new = int(data.get("last_new") or 0)
        return state


class GithubClient:
    """Dun laagje om de GitHub-API, met Nederlandse fouten."""

    def __init__(
        self,
        token: str = "",
        session: Optional[requests.Session] = None,
        timeout: float = TIMEOUT,
        api_root: str = API_ROOT,
    ) -> None:
        self.token = (token or "").strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        # Alleen voor de zelftest: dan wijst de API naar een lokale nabootser.
        self.api_root = (api_root or API_ROOT).rstrip("/")

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self.api_root}{path}"
        try:
            response = self.session.get(url, headers=self.headers(), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GithubError(f"GitHub was niet bereikbaar: {exc.__class__.__name__}.") from exc
        if response.status_code == 404:
            raise GithubError(
                "Deze repo bestaat niet of is privé; vul dan een toegangssleutel in bij de instellingen."
            )
        if response.status_code in (401, 403) and "rate limit" in response.text.lower():
            raise GithubError(
                "GitHub-limiet bereikt (60 controles per uur zonder sleutel). Zet de controle op een langere "
                "tussenpoos of vul een toegangssleutel in."
            )
        if response.status_code >= 400:
            raise GithubError(f"GitHub antwoordde met status {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise GithubError("GitHub gaf geen leesbaar antwoord.") from exc

    def releases(self, repo: str) -> list[dict[str, Any]]:
        data = self.get(f"/repos/{repo}/releases", {"per_page": 20})
        return data if isinstance(data, list) else []

    def issues(self, repo: str) -> list[dict[str, Any]]:
        data = self.get(
            f"/repos/{repo}/issues",
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": MAX_ISSUES},
        )
        return data if isinstance(data, list) else []

    def comments(self, repo: str) -> list[dict[str, Any]]:
        data = self.get(
            f"/repos/{repo}/issues/comments",
            {"sort": "updated", "direction": "desc", "per_page": MAX_COMMENTS},
        )
        return data if isinstance(data, list) else []


class GithubWatcher:
    """Kijkt periodiek naar de ingestelde repo's en meldt wat nieuw is."""

    def __init__(
        self,
        repos: Any,
        state_path: Path,
        log: Optional[Callable[[str], None]] = None,
        on_links: Optional[Callable[[list[str], str], None]] = None,
        on_asset: Optional[Callable[[dict[str, Any]], None]] = None,
        client: Optional[GithubClient] = None,
        watch_releases: bool = True,
        watch_links: bool = True,
        download_releases: bool = True,
        allow_hosts: tuple[str, ...] = (),
    ) -> None:
        self.repos = parse_repos(repos)
        self.state_path = Path(state_path)
        self.log = log
        self.on_links = on_links
        self.on_asset = on_asset
        self.client = client or GithubClient()
        self.watch_releases = watch_releases
        self.watch_links = watch_links
        self.download_releases = download_releases
        self.allow_hosts = tuple(allow_hosts)
        self.states: dict[str, RepoState] = {}
        self.last_result: dict[str, Any] = {}
        self.load()

    # -- toestand ----------------------------------------------------------
    def load(self) -> None:
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        stored = data.get("repos") if isinstance(data, dict) else None
        if isinstance(stored, dict):
            self.states = {repo: RepoState.from_dict(value or {}) for repo, value in stored.items()}

    def save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"saved_at": time.time(), "repos": {r: s.as_dict() for r, s in self.states.items()}}
            tmp = self.state_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "repos": self.repos,
            "states": {
                repo: {
                    "last_check": state.last_check,
                    "last_check_iso": _iso(state.last_check),
                    "last_error": state.last_error,
                    "last_new": state.last_new,
                    "initialized": state.initialized,
                }
                for repo, state in self.states.items()
            },
            "last_result": self.last_result,
        }

    # -- controleren -------------------------------------------------------
    def check_once(self) -> dict[str, Any]:
        """Controleer alle repo's; geeft terug wat er nieuw was."""

        result: dict[str, Any] = {
            "checked_at": time.time(),
            "repos": [],
            "links": [],
            "assets": [],
            "errors": [],
        }
        for repo in self.repos:
            state = self.states.setdefault(repo, RepoState())
            entry: dict[str, Any] = {"repo": repo, "new_releases": [], "new_assets": [], "new_links": [], "error": ""}
            try:
                if self.watch_releases:
                    self._check_releases(repo, state, entry)
                if self.watch_links:
                    self._check_links(repo, state, entry)
                state.initialized = True
                state.last_error = ""
            except GithubError as exc:
                entry["error"] = str(exc)
                state.last_error = str(exc)
                self._write(f"GitHub ({repo}): {exc}")
            except Exception as exc:  # noqa: BLE001 - de bron mag nooit de app laten vallen
                entry["error"] = f"Onverwachte fout: {exc.__class__.__name__}: {exc}"
                state.last_error = entry["error"]
                self._write(f"GitHub ({repo}): {entry['error']}")
            state.last_check = time.time()
            state.last_new = len(entry["new_links"]) + len(entry["new_assets"])
            if entry["error"]:
                result["errors"].append(f"{repo}: {entry['error']}")
            result["repos"].append(entry)
            result["links"].extend(entry["new_links"])
            result["assets"].extend(entry["new_assets"])
        self.save()
        self.last_result = result
        if result["links"] and self.on_links:
            self.on_links([entry["url"] for entry in result["links"]], "github")
        for asset in result["assets"]:
            if self.on_asset:
                self.on_asset(asset)
        return result

    def _check_releases(self, repo: str, state: RepoState, entry: dict[str, Any]) -> None:
        releases = self.client.releases(repo)
        first = not state.initialized
        for release in releases:
            if release.get("draft"):
                continue
            tag = str(release.get("tag_name") or release.get("name") or "").strip()
            if not tag:
                continue
            known = tag in state.release_tags
            if not known:
                state.release_tags.append(tag)
                entry["new_releases"].append(tag)
                self._write(
                    f"GitHub ({repo}): nieuwe release {tag}"
                    + (" (ijkpunt, niet gedownload)." if first else ".")
                )
            if first or not self.download_releases:
                continue
            for asset in release.get("assets") or []:
                key = str(asset.get("id") or asset.get("browser_download_url") or asset.get("name") or "")
                if not key or key in state.asset_ids:
                    continue
                state.asset_ids.append(key)
                entry["new_assets"].append(
                    {
                        "repo": repo,
                        "tag": tag,
                        "name": str(asset.get("name") or ""),
                        "url": str(asset.get("browser_download_url") or ""),
                        "size": asset.get("size"),
                        "release": str(release.get("html_url") or ""),
                    }
                )

    def _check_links(self, repo: str, state: RepoState, entry: dict[str, Any]) -> None:
        items: list[dict[str, Any]] = []
        for issue in self.client.issues(repo):
            items.append(
                {
                    "title": issue.get("title"),
                    "body": issue.get("body"),
                    "where": issue.get("html_url") or repo,
                    "kind": "issue",
                }
            )
        for comment in self.client.comments(repo):
            number = str(comment.get("issue_url") or "").rstrip("/").rsplit("/", 1)[-1]
            items.append(
                {
                    "title": "",
                    "body": comment.get("body"),
                    "where": comment.get("html_url") or repo,
                    "kind": f"opmerking bij #{number}" if number else "opmerking",
                }
            )
        for item in items:
            text = " ".join(str(item.get(key) or "") for key in ("title", "body"))
            for link in extract_links(text, self.allow_hosts):
                key = link.lower()
                if key in state.link_ids:
                    continue
                state.link_ids.append(key)
                entry["new_links"].append({"url": link, "where": item["where"], "kind": item["kind"]})
                self._write(f"GitHub ({repo}): WeTransfer-link gevonden in {item['kind']} — {item['where']}")

    def _write(self, message: str) -> None:
        if self.log:
            self.log(message)


def _iso(stamp: float) -> str:
    if not stamp:
        return ""
    return time.strftime("%d-%m-%Y %H:%M", time.localtime(stamp))
