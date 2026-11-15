"""Tests voor de webversie: de pagina op een IP-adres, met toegangscode."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl.server import WEB_METHODS, WebServer, new_code  # noqa: E402
from wtdl.sources import SourceRunner  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "ui"
CODE = "testcode123"


class StubApi:
    """Doet zich voor als de app, zodat de server los te testen is."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_state(self) -> dict[str, Any]:
        return {"version": "1.1.0", "items": [], "settings": {"web_port": 8756}}

    def add_links(self, text: str = "", password: str = "") -> dict[str, Any]:
        self.calls.append(("add_links", {"text": text, "password": password}))
        return {"ok": True, "added": 1, "message": "1 link toegevoegd."}

    def check_github(self) -> dict[str, Any]:
        return {"ok": True, "message": "niets nieuws"}

    def shutdown(self) -> dict[str, Any]:  # mag niet vanaf het web
        self.calls.append(("shutdown", {}))
        return {"ok": True}


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.api = StubApi()
        self.logs: list[str] = []

    def tearDown(self) -> None:
        if getattr(self, "server", None) is not None:
            self.server.stop()
        self.tmp.cleanup()

    def start(self, code: str = "") -> str:
        self.server = WebServer(
            self.api,
            UI,
            host="127.0.0.1",
            port=0,
            code=code,
            log=self.logs.append,
        )
        ok, error = self.server.start()
        self.assertTrue(ok, error)
        return f"http://127.0.0.1:{self.server.port}"

    # -- zonder toegangscode ----------------------------------------------
    def test_pagina_en_bestanden_komen_uit_de_ui_map(self) -> None:
        basis = self.start()
        index = requests.get(f"{basis}/", timeout=5)
        self.assertEqual(index.status_code, 200)
        self.assertIn("WeTransfer Ophalen", index.text)
        self.assertIn("text/html", index.headers["content-type"])
        script = requests.get(f"{basis}/app.js", timeout=5)
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", script.headers["content-type"])

    def test_status_van_de_app_is_op_te_halen(self) -> None:
        basis = self.start()
        antwoord = requests.get(f"{basis}/api/state", timeout=5)
        self.assertEqual(antwoord.status_code, 200)
        self.assertEqual(antwoord.json()["version"], "1.1.0")

    def test_handeling_uitvoeren_gaat_naar_de_app(self) -> None:
        basis = self.start()
        antwoord = requests.post(
            f"{basis}/api/call",
            json={"method": "add_links", "arguments": {"text": "https://we.tl/t-abc", "password": ""}},
            timeout=5,
        )
        self.assertEqual(antwoord.status_code, 200)
        self.assertTrue(antwoord.json()["ok"])
        self.assertEqual(self.api.calls, [("add_links", {"text": "https://we.tl/t-abc", "password": ""})])

    def test_afsluiten_mag_niet_vanaf_het_web(self) -> None:
        basis = self.start()
        antwoord = requests.post(f"{basis}/api/call", json={"method": "shutdown", "arguments": {}}, timeout=5)
        self.assertEqual(antwoord.status_code, 403)
        self.assertEqual(self.api.calls, [])

    def test_onbekende_handeling_geweigerd(self) -> None:
        """Alles wat niet op de lijst staat wordt geweigerd (ook als het bestaat)."""

        basis = self.start()
        antwoord = requests.post(f"{basis}/api/call", json={"method": "bestaat_niet", "arguments": {}}, timeout=5)
        self.assertEqual(antwoord.status_code, 403)
        self.assertIn("mag niet", antwoord.json()["error"])

    def test_buiten_de_map_komen_kan_niet(self) -> None:
        basis = self.start()
        for pad in ("/../main.py", "/..%2fmain.py", "/images/../../main.py"):
            antwoord = requests.get(f"{basis}{pad}", timeout=5)
            self.assertEqual(antwoord.status_code, 404, pad)
            self.assertNotIn("def main", antwoord.text)

    def test_ping_vertelt_welke_app_er_loopt(self) -> None:
        basis = self.start()
        antwoord = requests.get(f"{basis}/api/ping", timeout=5)
        self.assertTrue(antwoord.json()["ok"])
        self.assertEqual(antwoord.json()["app"], "WeTransfer Ophalen")

    # -- met toegangscode --------------------------------------------------
    def test_zonder_code_geen_toegang(self) -> None:
        basis = self.start(CODE)
        self.assertEqual(requests.get(f"{basis}/api/state", timeout=5).status_code, 401)
        pagina = requests.get(f"{basis}/", timeout=5)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Toegangscode", pagina.text)
        self.assertNotIn("Links toevoegen", pagina.text)

    def test_verkeerde_code_wordt_geweigerd(self) -> None:
        basis = self.start(CODE)
        antwoord = requests.post(f"{basis}/api/login", json={"code": "fout"}, timeout=5)
        self.assertEqual(antwoord.status_code, 401)
        self.assertFalse(antwoord.json()["ok"])

    def test_juiste_code_zet_een_cookie_en_geeft_toegang(self) -> None:
        basis = self.start(CODE)
        sessie = requests.Session()
        antwoord = sessie.post(f"{basis}/api/login", json={"code": CODE}, timeout=5)
        self.assertEqual(antwoord.status_code, 200)
        self.assertIn("wto_code", antwoord.headers.get("set-cookie", ""))
        self.assertEqual(sessie.get(f"{basis}/api/state", timeout=5).status_code, 200)
        self.assertIn("Links toevoegen", sessie.get(f"{basis}/", timeout=5).text)

    def test_code_in_de_adresbalk_werkt_ook(self) -> None:
        basis = self.start(CODE)
        antwoord = requests.get(f"{basis}/?code={CODE}", allow_redirects=False, timeout=5)
        self.assertEqual(antwoord.status_code, 302)
        self.assertIn("wto_code", antwoord.headers.get("set-cookie", ""))

    def test_info_vertelt_het_adres_en_of_er_een_code_nodig_is(self) -> None:
        self.start(CODE)
        info = self.server.info()
        self.assertTrue(info["enabled"])
        self.assertTrue(info["needs_code"])
        self.assertEqual(info["code"], CODE)
        self.assertIn(str(self.server.port), info["address"])

    def test_stoppen_maakt_het_adres_weer_vrij(self) -> None:
        basis = self.start()
        self.server.stop()
        with self.assertRaises(requests.RequestException):
            requests.get(f"{basis}/api/state", timeout=2)

    def test_alle_toegestane_handelingen_bestaan_echt(self) -> None:
        """Zo valt meteen op als een knop naar iets verwijst dat niet bestaat."""

        import main  # noqa: PLC0415 - pas hier nodig

        api = main.DesktopApi(config_dir=Path(self.tmp.name))
        try:
            ontbreekt = [naam for naam in WEB_METHODS if not callable(getattr(api, naam, None))]
            self.assertEqual(ontbreekt, [], f"deze webhandelingen bestaan niet: {ontbreekt}")
            self.assertNotIn("choose_folder", WEB_METHODS)
            self.assertNotIn("shutdown", WEB_METHODS)
        finally:
            api.shutdown()

    def test_code_is_elk_keer_anders(self) -> None:
        self.assertNotEqual(new_code(), new_code())
        self.assertGreaterEqual(len(new_code()), 8)


class SourceRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logs: list[str] = []
        self.runner = SourceRunner(log=self.logs.append, tick=0.2)

    def tearDown(self) -> None:
        self.runner.stop()

    def test_bron_wordt_meteen_gecontroleerd(self) -> None:
        gedaan: list[str] = []
        self.runner.register("test", lambda: (gedaan.append("ja"), {"links": ["a"], "messages": 2})[1], 5)
        result = self.runner.check_now("test")
        self.assertTrue(result["ok"])
        self.assertEqual(gedaan, ["ja"])
        self.assertIn("2 bericht(en)", self.runner.status()["sources"][0]["last_summary"])

    def test_fout_in_een_bron_geeft_een_nette_melding(self) -> None:
        def kapot() -> dict[str, Any]:
            raise RuntimeError("boem")

        self.runner.register("kapot", kapot, 5)
        result = self.runner.check_now("kapot")
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        self.assertIn("RuntimeError", self.runner.status()["sources"][0]["last_error"])

    def test_onbekende_bron(self) -> None:
        self.assertFalse(self.runner.check_now("bestaat-niet")["ok"])

    def test_bron_loopt_op_de_achtergrond(self) -> None:
        import time  # noqa: PLC0415

        gedaan: list[int] = []
        self.runner.register("tik", lambda: (gedaan.append(1), {})[1], 1)
        self.runner.start()
        deadline = time.time() + 5
        while not gedaan and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(gedaan, "de bron is niet op de achtergrond gedraaid")
        self.runner.stop()
        self.assertFalse(self.runner.running)

    def test_alles_in_een_keer(self) -> None:
        self.runner.register("een", lambda: {}, 5)
        self.runner.register("twee", lambda: {}, 5)
        result = self.runner.check_all()
        self.assertEqual(sorted(result["results"]), ["een", "twee"])


if __name__ == "__main__":
    unittest.main()
