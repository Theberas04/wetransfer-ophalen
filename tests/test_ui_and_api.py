"""Tests voor de pagina zelf en voor de koppeling tussen pagina en app."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
