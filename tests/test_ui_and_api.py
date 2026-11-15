"""Tests voor de pagina zelf en voor de koppeling tussen pagina en app."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from wtdl.store import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"


class UiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (UI / "index.html").read_text(encoding="utf-8")
        self.js = (UI / "app.js").read_text(encoding="utf-8")
        self.css = (UI / "style.css").read_text(encoding="utf-8")

    def test_bestanden_bestaan(self) -> None:
        for name in ("index.html", "app.js", "style.css", "images/werdens-media-logo.png"):
            self.assertTrue((UI / name).exists(), f"ui/{name} ontbreekt")

    def test_pagina_laadt_de_eigen_bestanden(self) -> None:
        self.assertIn('src="app.js"', self.html)
        self.assertIn('href="style.css"', self.html)
        self.assertIn('src="images/werdens-media-logo.png"', self.html)

    def test_elk_element_dat_de_code_zoekt_bestaat(self) -> None:
        ids = set(re.findall(r'\$\("([a-z0-9_]+)"\)', self.js))
        ids |= set(re.findall(r'getElementById\("([a-z0-9_]+)"\)', self.js))
        self.assertGreater(len(ids), 10)
        ontbreekt = sorted(i for i in ids if f'id="{i}"' not in self.html)
        self.assertEqual(ontbreekt, [], f"deze id's staan niet in index.html: {ontbreekt}")

    def test_geen_dubbele_ids_in_de_pagina(self) -> None:
        ids = re.findall(r'\sid="([^"]+)"', self.html)
        dubbel = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dubbel, [], f"dubbele id's: {dubbel}")

    def test_wachtrij_wordt_gesorteerd_op_wat_aandacht_nodig_heeft(self) -> None:
        self.assertIn("byImportance", self.js)
        self.assertIn("ORDER", self.js)

    def test_huisstijlkleuren(self) -> None:
        for kleur in ("#f4f7fb", "#2ea3f2", "#1e73be", "#e3eaf4"):
            self.assertIn(kleur, self.css, f"huisstijlkleur {kleur} ontbreekt")


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.api = main.DesktopApi(config_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_heeft_alles_wat_de_pagina_nodig_heeft(self) -> None:
        state = self.api.get_state()
        for sleutel in ("version", "settings", "items", "running", "pending", "log", "config_dir"):
            self.assertIn(sleutel, state)
        self.assertEqual(state["items"], [])
        self.assertFalse(state["running"])

    def test_links_toevoegen_en_weer_lezen(self) -> None:
        result = self.api.add_links("Zie https://we.tl/t-abc123 en https://we.tl/t-abc123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(self.api.get_state()["items"]), 1)

    def test_geen_link_levert_duidelijke_boodschap(self) -> None:
        result = self.api.add_links("hier staat geen link in")
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 0)
        self.assertIn("Geen WeTransfer-link", result["message"])

    def test_instellingen_opslaan_en_teruglezen(self) -> None:
        result = self.api.save_settings({"download_dir": str(Path(self.tmp.name) / "map"), "notify_mode": "uit"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.settings.notify_mode, "uit")
        opnieuw = main.DesktopApi(config_dir=Path(self.tmp.name))
        self.assertEqual(opnieuw.settings.download_dir, str(Path(self.tmp.name) / "map"))

    def test_lege_webhook_geeft_nette_fout(self) -> None:
        result = self.api.test_slack("")
        self.assertFalse(result["ok"])
        self.assertIn("webhook", result["error"].lower())

    def test_map_openen_van_ontbrekende_map(self) -> None:
        result = self.api.open_folder(str(Path(self.tmp.name) / "bestaat-niet"))
        self.assertFalse(result["ok"])
        self.assertIn("bestaat niet", result["error"])

    def test_starten_zonder_wachtrij(self) -> None:
        result = self.api.start_queue()
        self.assertFalse(result["ok"])
        self.assertIn("niets in de wachtrij", result["message"])

    def test_wachtmap_scannen_staat_uit(self) -> None:
        result = self.api.scan_watch_folder()
        self.assertFalse(result["ok"])
        self.assertIn("staat uit", result["error"])

    def test_wachtmap_aanzetten_start_de_wachter(self) -> None:
        watch = Path(self.tmp.name) / "links"
        watch.mkdir()
        self.assertTrue(self.api.save_settings({"watch_enabled": True, "watch_dir": str(watch)})["ok"])
        try:
            self.assertIsNotNone(self.api.watcher)
            self.assertTrue(self.api.scan_watch_folder()["ok"])
        finally:
            self.api.shutdown()


class DialoogTest(unittest.TestCase):
    """Het mapvenster moet op de GUI-thread openen, anders weigert WinForms."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.api = main.DesktopApi(config_dir=Path(self.tmp.name))
        self.calls: list[str] = []
        self.origineel = main.ui_thread_delegate

    def tearDown(self) -> None:
        main.ui_thread_delegate = self.origineel
        self.tmp.cleanup()

    def test_met_invoke_gaat_het_venster_naar_de_gui_thread(self) -> None:
        def invoke(delegate):  # WinForms' Control.Invoke
            self.calls.append("invoke")
            return delegate()

        self.api.window = SimpleNamespace(native=SimpleNamespace(Invoke=invoke))
        main.ui_thread_delegate = lambda callback: callback
        result = self.api._run_dialog(lambda: (self.calls.append("dialoog"), ["C:\\keuze"])[1])
        self.assertEqual(self.calls, ["invoke", "dialoog"])
        self.assertEqual(result, ["C:\\keuze"])

    def test_zonder_invoke_gewoon_direct(self) -> None:
        self.api.window = SimpleNamespace(native=None)
        main.ui_thread_delegate = lambda callback: callback
        result = self.api._run_dialog(lambda: (self.calls.append("dialoog"), ["pad"])[1])
        self.assertEqual(self.calls, ["dialoog"])
        self.assertEqual(result, ["pad"])

    def test_zonder_pythonnet_valt_hij_terug(self) -> None:
        self.api.window = SimpleNamespace(native=SimpleNamespace(Invoke=lambda delegate: self.calls.append("invoke")))
        main.ui_thread_delegate = lambda callback: None  # pythonnet ontbreekt
        result = self.api._run_dialog(lambda: ["direct"])
        self.assertEqual(self.calls, [])
        self.assertEqual(result, ["direct"])

    def test_fout_in_het_venster_komt_bij_de_aanroeper_terecht(self) -> None:
        self.api.window = SimpleNamespace(native=SimpleNamespace(Invoke=lambda delegate: delegate()))
        main.ui_thread_delegate = lambda callback: callback

        def kapot():
            raise RuntimeError("venster kapot")

        with self.assertRaises(RuntimeError):
            self.api._run_dialog(kapot)

    def test_map_kiezen_kiest_de_downloadmap(self) -> None:
        self.api.window = SimpleNamespace(
            native=SimpleNamespace(Invoke=lambda delegate: delegate()),
            create_file_dialog=lambda *args, **kwargs: [str(Path(self.tmp.name) / "gekozen")],
        )
        main.ui_thread_delegate = lambda callback: callback
        result = self.api.choose_folder("download")
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.settings.download_dir, str(Path(self.tmp.name) / "gekozen"))

    def test_geannuleerd_mapvenster_geeft_geen_fout(self) -> None:
        self.api.window = SimpleNamespace(
            native=SimpleNamespace(Invoke=lambda delegate: delegate()),
            create_file_dialog=lambda *args, **kwargs: None,
        )
        main.ui_thread_delegate = lambda callback: callback
        result = self.api.choose_folder("download")
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])


class InstellingenTest(unittest.TestCase):
    def test_standaardmap_heet_wetransfer(self) -> None:
        self.assertTrue(str(Settings().download_dir).endswith("WeTransfer"))

    def test_standaard_mailserver_is_gmail(self) -> None:
        settings = Settings()
        self.assertEqual(settings.mailbox_host, "imap.gmail.com")
        self.assertEqual(settings.mailbox_port, 993)
        self.assertEqual(settings.mailbox_sender, "wetransfer")
        self.assertEqual(settings.github_repos, "Theberas04/wetransfer-ophalen")


class FakeGithub:
    """Een GitHub die precies teruggeeft wat de test wil."""
    def __init__(self) -> None:
        self.release_list: list[dict[str, Any]] = []
        self.issue_list: list[dict[str, Any]] = []

    def releases(self, repo: str) -> list[dict[str, Any]]:
        return self.release_list

    def issues(self, repo: str) -> list[dict[str, Any]]:
        return self.issue_list

    def comments(self, repo: str) -> list[dict[str, Any]]:
        return []


class FakeImap:
    """Een mailbox met één bericht erin."""
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def uid(self, commando: str, *args: Any) -> tuple[str, list[Any]]:
        if commando == "search":
            return "OK", [b"7"]
        return "OK", [(b"7 (RFC822 {1}", self.raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]


class BronnenTest(unittest.TestCase):
    """De webversie, de GitHub-bron en de mailbox vanaf het niveau van de app."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.api = main.DesktopApi(config_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.api.shutdown()
        self.tmp.cleanup()

    # -- webversie ---------------------------------------------------------
    def test_web_info_geeft_een_adres_ook_als_hij_uit_staat(self) -> None:
        info = self.api.web_info()
        self.assertFalse(info["enabled"])
        self.assertTrue(info["address"].startswith("http://"))
        self.assertIn(str(self.api.settings.web_port), info["address"])

    def test_webversie_aanzetten_start_een_echte_server(self) -> None:
        result = self.api.save_settings(
            {"web_enabled": True, "web_host": "127.0.0.1", "web_port": 0, "web_code": "abc123"}
        )
        self.assertTrue(result["ok"], result)
        self.assertIsNotNone(self.api.server)
        server = self.api.server
        assert server is not None
        basis = f"http://127.0.0.1:{server.port}"

        # Zonder code geen toegang.
        self.assertEqual(requests.get(f"{basis}/api/state", timeout=5).status_code, 401)

        sessie = requests.Session()
        self.assertEqual(
            sessie.post(f"{basis}/api/login", json={"code": "abc123"}, timeout=5).status_code, 200
        )
        toestand = sessie.get(f"{basis}/api/state", timeout=5).json()
        self.assertEqual(toestand["version"], main.__version__)
        self.assertIn("settings", toestand)
        self.assertIn("web", toestand)

        # Vanaf de telefoon een link toevoegen komt echt in de wachtrij.
        antwoord = sessie.post(
            f"{basis}/api/call",
            json={"method": "add_links", "arguments": {"text": "https://we.tl/t-vanaftelefoon", "password": ""}},
            timeout=5,
        )
        self.assertTrue(antwoord.json()["ok"])
        self.assertEqual(len(self.api.get_state()["items"]), 1)
        self.assertEqual(self.api.get_state()["items"][0]["source"], "handmatig")

    def test_webversie_uitzetten_stopt_de_server(self) -> None:
        self.api.save_settings({"web_enabled": True, "web_host": "127.0.0.1", "web_port": 0})
        self.assertIsNotNone(self.api.server)
        server = self.api.server
        assert server is not None
        poort = server.port
        self.api.save_settings({"web_enabled": False})
        self.assertIsNone(self.api.server)
        with self.assertRaises(requests.RequestException):
            requests.get(f"http://127.0.0.1:{poort}/api/state", timeout=2)

    def test_toegangscode_wordt_automatisch_gemaakt(self) -> None:
        self.api.save_settings({"web_enabled": True, "web_host": "127.0.0.1", "web_port": 0})
        self.assertTrue(self.api.settings.web_code)
        self.assertEqual(self.api.web_info()["code"], self.api.settings.web_code)

    # -- GitHub ------------------------------------------------------------
    def test_github_controleren_zonder_repo(self) -> None:
        self.api.save_settings({"github_repos": ""})
        result = self.api.check_github()
        self.assertFalse(result["ok"])
        self.assertIn("repo", result["error"].lower())

    def test_nieuwe_release_van_github_komt_in_de_wachtrij(self) -> None:
        fake = FakeGithub()
        self.api.github_client_factory = lambda: fake
        self.api.save_settings({"github_repos": "Theberas04/wetransfer-ophalen"})

        fake.release_list = [{"tag_name": "v1.0.0", "assets": []}]
        eerste = self.api.check_github()
        self.assertTrue(eerste["ok"], eerste)
        self.assertIn("ijkpunt", " ".join(self.api.log.tail(20)))
        self.assertEqual(self.api.get_state()["items"], [])

        fake.release_list = [
            {
                "tag_name": "v1.1.0",
                "assets": [
                    {
                        "id": 42,
                        "name": "WeTransferOphalen-Windows.zip",
                        "size": 1234,
                        "browser_download_url": "https://github.com/Theberas04/wetransfer-ophalen/releases/download/v1.1.0/x.zip",
                    }
                ],
            },
            {"tag_name": "v1.0.0", "assets": []},
        ]
        tweede = self.api.check_github()
        self.assertTrue(tweede["ok"], tweede)
        self.assertIn("nieuwe release", tweede["message"])
        items = self.api.get_state()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "github")
        self.assertTrue(items[0]["direct"])
        self.assertEqual(items[0]["name"], "WeTransferOphalen-Windows.zip")

    def test_link_in_een_issue_komt_in_de_wachtrij(self) -> None:
        fake = FakeGithub()
        self.api.github_client_factory = lambda: fake
        self.api.save_settings({"github_repos": "Theberas04/wetransfer-ophalen"})
        fake.issue_list = [
            {
                "title": "Opnames",
                "body": "Hier de opnames: https://we.tl/t-Zaj2e92uBwQWkVBN",
                "html_url": "https://github.com/Theberas04/wetransfer-ophalen/issues/1",
            }
        ]
        result = self.api.check_github()
        self.assertTrue(result["ok"], result)
        items = self.api.get_state()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "github")
        self.assertIn("we.tl", items[0]["url"])

    # -- mailbox -----------------------------------------------------------
    def test_mailbox_zonder_gegevens(self) -> None:
        result = self.api.check_mailbox()
        self.assertFalse(result["ok"])
        self.assertIn("e-mailadres", result["error"])

    def test_gevonden_mail_link_komt_in_de_wachtrij(self) -> None:
        from email.message import EmailMessage  # noqa: PLC0415

        bericht = EmailMessage()
        bericht["From"] = "WeTransfer <noreply@wetransfer.com>"
        bericht["Subject"] = "Je aangevraagde opnames staan klaar"
        bericht.set_content("Download hier: https://we.tl/t-opnames12345")

        self.api.mailbox_client_factory = lambda config: FakeImap(bericht.as_bytes())
        self.api.save_settings(
            {
                "mailbox_host": "imap.example.com",
                "mailbox_user": "tom@example.com",
                "mailbox_password": "geheim",
            }
        )
        result = self.api.check_mailbox()
        self.assertTrue(result["ok"], result)
        self.assertIn("1 link", result["message"])
        items = self.api.get_state()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "mailbox")

        # Een tweede keer lezen levert niets nieuws op.
        opnieuw = self.api.check_mailbox()
        self.assertTrue(opnieuw["ok"])
        self.assertEqual(len(self.api.get_state()["items"]), 1)

    def test_alles_controleren_meldt_elke_bron(self) -> None:
        result = self.api.check_all_sources()
        self.assertTrue(result["ok"])
        self.assertIn("github", result["results"])
        self.assertIn("mailbox", result["results"])

    def test_bronstatus_staat_in_de_toestand(self) -> None:
        state = self.api.get_state()
        self.assertIn("sources", state)
        self.assertIn("github", state)
        self.assertIn("mailbox", state)
        self.assertIn("web", state)


if __name__ == "__main__":
    unittest.main()
