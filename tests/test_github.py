"""Tests voor de GitHub-bron: repo's lezen, releases en links oppikken."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl.github import (  # noqa: E402
    GithubError,
    GithubWatcher,
    normalize_repo,
    parse_repos,
)

REPO = "Theberas04/wetransfer-ophalen"


def release(tag: str, assets: list[dict[str, Any]] | None = None, draft: bool = False) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "draft": draft,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "assets": assets or [],
    }


def asset(identifier: int, name: str, size: int = 1000) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": name,
        "size": size,
        "browser_download_url": f"https://github.com/{REPO}/releases/download/v1/{name}",
    }


class FakeClient:
    """Doet zich voor als de GitHub-API."""

    def __init__(self, releases=None, issues=None, comments=None, error: str = "") -> None:
        self._releases = releases or []
        self._issues = issues or []
        self._comments = comments or []
        self._error = error
        self.calls: list[str] = []

    def releases(self, repo: str) -> list[dict[str, Any]]:
        self.calls.append(f"releases:{repo}")
        if self._error:
            raise GithubError(self._error)
        return self._releases

    def issues(self, repo: str) -> list[dict[str, Any]]:
        self.calls.append(f"issues:{repo}")
        if self._error:
            raise GithubError(self._error)
        return self._issues

    def comments(self, repo: str) -> list[dict[str, Any]]:
        self.calls.append(f"comments:{repo}")
        if self._error:
            raise GithubError(self._error)
        return self._comments


class HelperTest(unittest.TestCase):
    def test_repo_namen(self) -> None:
        for value in (
            "Theberas04/wetransfer-ophalen",
            "https://github.com/Theberas04/wetransfer-ophalen",
            "github.com/Theberas04/wetransfer-ophalen/",
            "git@github.com:Theberas04/wetransfer-ophalen.git",
        ):
            self.assertEqual(normalize_repo(value), REPO)

    def test_onzin_is_geen_repo(self) -> None:
        with self.assertRaises(GithubError):
            normalize_repo("alleen-een-woord")

    def test_meerdere_repo_scheiden(self) -> None:
        self.assertEqual(
            parse_repos("Theberas04/a\nTheberas04/b, Theberas04/a"),
            ["Theberas04/a", "Theberas04/b"],
        )
        self.assertEqual(parse_repos(["Theberas04/a"]), ["Theberas04/a"])
        self.assertEqual(parse_repos(""), [])


class WatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.logs: list[str] = []
        self.links: list[tuple[list[str], str]] = []
        self.assets: list[dict[str, Any]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def maak(self, client: FakeClient, **kwargs: Any) -> GithubWatcher:
        return GithubWatcher(
            [REPO],
            self.dir / "github.json",
            log=self.logs.append,
            on_links=lambda links, source: self.links.append((links, source)),
            on_asset=self.assets.append,
            client=client,
            **kwargs,
        )

    def test_eerste_controle_legt_ijkpunt_vast_zonder_te_downloaden(self) -> None:
        client = FakeClient(releases=[release("v1.0.0", [asset(1, "WeTransferOphalen-Windows.zip")])])
        watcher = self.maak(client)
        result = watcher.check_once()
        self.assertEqual(result["repos"][0]["new_releases"], ["v1.0.0"])
        self.assertEqual(result["assets"], [])
        self.assertEqual(self.assets, [])
        self.assertTrue(any("ijkpunt" in regel for regel in self.logs))

    def test_nieuwe_release_geeft_bestanden_door(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")])
        watcher = self.maak(client)
        watcher.check_once()
        client._releases = [release("v1.1.0", [asset(7, "app.zip", 12345)]), release("v1.0.0")]
        result = watcher.check_once()
        self.assertEqual(result["repos"][0]["new_releases"], ["v1.1.0"])
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(self.assets[0]["name"], "app.zip")
        self.assertEqual(self.assets[0]["tag"], "v1.1.0")
        self.assertEqual(self.assets[0]["size"], 12345)

    def test_hetzelfde_bestand_komt_niet_twee_keer(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")])
        watcher = self.maak(client)
        watcher.check_once()
        client._releases = [release("v1.1.0", [asset(7, "app.zip")]), release("v1.0.0")]
        watcher.check_once()
        tweede = watcher.check_once()
        self.assertEqual(tweede["assets"], [])

    def test_conceptreleases_worden_overgeslagen(self) -> None:
        client = FakeClient(releases=[release("v9.9.9", [asset(3, "geheim.zip")], draft=True)])
        watcher = self.maak(client)
        result = watcher.check_once()
        self.assertEqual(result["repos"][0]["new_releases"], [])
        self.assertEqual(self.assets, [])

    def test_links_uit_issues_en_opmerkingen(self) -> None:
        link = "https://we.tl/t-Zaj2e92uBwQWkVBN"
        client = FakeClient(
            issues=[{"title": "Opnames", "body": f"Hier is de transfer: {link}", "html_url": "https://github.com/x/y/issues/1"}],
            comments=[{"body": "ook nog https://we.tl/t-abcdefghijkl", "html_url": "https://github.com/x/y/issues/1#issuecomment-2", "issue_url": "https://api.github.com/repos/x/y/issues/1"}],
        )
        watcher = self.maak(client)
        result = watcher.check_once()
        gevonden = [entry["url"] for entry in result["links"]]
        self.assertIn(link, gevonden)
        self.assertIn("https://we.tl/t-abcdefghijkl", gevonden)
        self.assertEqual(self.links[0][1], "github")
        self.assertEqual(len(self.links[0][0]), 2)

    def test_gevonden_links_staan_na_een_herstart_nog_steeds_in_de_toestand(self) -> None:
        link = "https://we.tl/t-Zaj2e92uBwQWkVBN"
        client = FakeClient(issues=[{"title": "", "body": link, "html_url": "https://github.com/x/y/issues/1"}])
        watcher = self.maak(client)
        watcher.check_once()
        opnieuw = self.maak(client)
        tweede = opnieuw.check_once()
        self.assertEqual(tweede["links"], [])

    def test_fout_bij_een_repo_stopt_de_controle_niet(self) -> None:
        client = FakeClient(error="GitHub was niet bereikbaar: ConnectionError.")
        watcher = self.maak(client)
        result = watcher.check_once()
        self.assertTrue(result["errors"])
        self.assertIn("niet bereikbaar", result["repos"][0]["error"])
        self.assertFalse(result["links"])

    def test_alleen_releases_of_alleen_links(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")], issues=[{"body": "https://we.tl/t-abc", "html_url": "x"}])
        alleen_links = self.maak(client, watch_releases=False)
        alleen_links.check_once()
        self.assertEqual(client.calls, ["issues:Theberas04/wetransfer-ophalen", "comments:Theberas04/wetransfer-ophalen"])
        client.calls.clear()
        alleen_releases = self.maak(client, watch_links=False)
        alleen_releases.check_once()
        self.assertEqual(client.calls, ["releases:Theberas04/wetransfer-ophalen"])

    def test_zonder_bestandsoverdracht_alleen_melden(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")])
        watcher = self.maak(client, download_releases=False)
        watcher.check_once()
        client._releases = [release("v1.1.0", [asset(7, "app.zip")]), release("v1.0.0")]
        result = watcher.check_once()
        self.assertEqual(result["repos"][0]["new_releases"], ["v1.1.0"])
        self.assertEqual(self.assets, [])

    def test_toestand_wordt_bewaard_als_json(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")])
        watcher = self.maak(client)
        watcher.check_once()
        data = json.loads((self.dir / "github.json").read_text(encoding="utf-8"))
        self.assertIn(REPO, data["repos"])
        self.assertEqual(data["repos"][REPO]["release_tags"], ["v1.0.0"])

    def test_status_toont_de_laatste_controle(self) -> None:
        client = FakeClient(releases=[release("v1.0.0")])
        watcher = self.maak(client)
        watcher.check_once()
        status = watcher.status()
        self.assertEqual(status["repos"], [REPO])
        self.assertTrue(status["states"][REPO]["last_check_iso"])
        self.assertTrue(status["states"][REPO]["initialized"])


if __name__ == "__main__":
    unittest.main()
