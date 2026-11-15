"""Tests voor de mailbox-bron: mails lezen, links eruit halen, niets aanraken."""

from __future__ import annotations

import imaplib
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl.mailbox import (  # noqa: E402
    MailboxConfig,
    MailboxWatcher,
    links_from_message,
    message_meta,
    message_text,
)

LINK = "https://we.tl/t-Zaj2e92uBwQWkVBN"


def bericht(tekst: str, onderwerp: str = "WeTransfer: je bestanden zijn klaar", afzender: str = "WeTransfer <noreply@wetransfer.com>") -> bytes:
    message = EmailMessage()
    message["From"] = afzender
    message["To"] = "tom@example.com"
    message["Subject"] = onderwerp
    message["Date"] = "Sun, 15 Nov 2026 12:00:00 +0100"
    message.set_content(tekst)
    return message.as_bytes()


def html_bericht(tekst: str, html: str) -> bytes:
    message = EmailMessage()
    message["From"] = "WeTransfer <noreply@wetransfer.com>"
    message["Subject"] = "Je transfer"
    message.set_content(tekst)
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


class FakeImap:
    """Doet zich voor als imaplib, zodat er geen echte mailbox nodig is."""

    def __init__(self, config: MailboxConfig, uids: list[str] | None = None, berichten: dict[str, bytes] | None = None, fout: str = "") -> None:
        self.config = config
        self.uids = uids if uids is not None else ["1"]
        self.berichten = berichten or {}
        self.fout = fout
        self.calls: list[tuple[str, ...]] = []
        self.ingelogd = False
        self.readonly = False
        self.gesloten = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user, password))
        if self.fout:
            raise imaplib.IMAP4.error(self.fout)
        self.ingelogd = True
        return "OK", [b"LOGIN completed"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", folder, str(readonly)))
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, commando: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", commando, *[str(arg) for arg in args]))
        if commando == "search":
            return "OK", [" ".join(self.uids).encode()]
        if commando == "fetch":
            uid = str(args[0])
            raw = self.berichten.get(uid, b"")
            return "OK", [(b"1 (RFC822 {123}", raw), b")"]
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        self.gesloten = True
        return "BYE", [b""]


class BerichtTest(unittest.TestCase):
    def test_link_uit_platte_tekst(self) -> None:
        raw = bericht(f"Bekijk je bestanden: {LINK}\nGroeten, WeTransfer")
        self.assertEqual(links_from_message(raw), [LINK])

    def test_link_uit_html_met_entiteiten(self) -> None:
        raw = html_bericht(
            "Bekijk je bestanden in de browser.",
            f'<p>Klik <a href="{LINK}?amp;x=1">hier</a></p>',
        )
        gevonden = links_from_message(raw)
        self.assertTrue(gevonden)
        self.assertIn("https://we.tl/t-Zaj2e92uBwQWkVBN", gevonden[0])

    def test_bericht_zonder_link_geeft_niets(self) -> None:
        self.assertEqual(links_from_message(bericht("Nieuwsbrief zonder transfer.")), [])

    def test_onderwerp_en_afzender_leesbaar(self) -> None:
        raw = bericht("tekst", onderwerp="=?utf-8?q?Opnames_van_WeTransfer?=")
        meta = message_meta(raw)
        self.assertEqual(meta["subject"], "Opnames van WeTransfer")
        self.assertIn("wetransfer.com", meta["sender"])

    def test_tekst_uit_een_bericht(self) -> None:
        self.assertIn("Bekijk je bestanden", message_text(bericht(f"Bekijk je bestanden: {LINK}")))

    def test_duidelijk_gemaakte_configuratie(self) -> None:
        config = MailboxConfig()
        self.assertFalse(config.ready)
        self.assertIn("mailserver", config.problem())
        config = MailboxConfig(host="imap.example.com", user="tom@example.com", password="geheim")
        self.assertTrue(config.ready)
        self.assertEqual(config.problem(), "")


class WatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.logs: list[str] = []
        self.links: list[tuple[list[str], str]] = []
        self.config = MailboxConfig(host="imap.example.com", user="tom@example.com", password="geheim")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def maak(self, imap: FakeImap) -> MailboxWatcher:
        return MailboxWatcher(
            self.config,
            self.dir / "mailbox.json",
            log=self.logs.append,
            on_links=lambda links, source: self.links.append((links, source)),
            client_factory=lambda config: imap,
        )

    def test_link_wordt_gevonden_en_doorgegeven(self) -> None:
        imap = FakeImap(self.config, uids=["1"], berichten={"1": bericht(f"Hier: {LINK}")})
        watcher = self.maak(imap)
        result = watcher.check_once()
        self.assertEqual(result["links"], [LINK])
        self.assertEqual(result["messages"], 1)
        self.assertEqual(self.links, [([LINK], "mailbox")])
        self.assertTrue(imap.ingelogd)
        self.assertTrue(imap.gesloten)
        # De mailbox blijft met rust: alleen lezen.
        self.assertTrue(imap.readonly)
        self.assertNotIn(("uid", "store"), [call[:2] for call in imap.calls])

    def test_hetzelfde_bericht_wordt_niet_twee_keer_gelezen(self) -> None:
        imap = FakeImap(self.config, uids=["1"], berichten={"1": bericht(f"Hier: {LINK}")})
        watcher = self.maak(imap)
        watcher.check_once()
        tweede = watcher.check_once()
        self.assertEqual(tweede["links"], [])
        self.assertEqual(tweede["messages"], 0)

    def test_bekeken_berichten_blijven_bewaard(self) -> None:
        imap = FakeImap(self.config, uids=["1"], berichten={"1": bericht(f"Hier: {LINK}")})
        self.maak(imap).check_once()
        data = json.loads((self.dir / "mailbox.json").read_text(encoding="utf-8"))
        self.assertEqual(data["seen"], ["1"])
        opnieuw = self.maak(imap)
        self.assertEqual(opnieuw.seen, ["1"])

    def test_bericht_zonder_link_wordt_wel_afgevinkt(self) -> None:
        imap = FakeImap(self.config, uids=["1"], berichten={"1": bericht("Algemene nieuwsbrief.")})
        watcher = self.maak(imap)
        result = watcher.check_once()
        self.assertEqual(result["links"], [])
        self.assertEqual(result["messages"], 1)
        self.assertEqual(watcher.seen, ["1"])

    def test_inloggen_mislukt_geeft_een_nette_uitleg(self) -> None:
        imap = FakeImap(self.config, fout="LOGIN failed: Invalid credentials")
        watcher = self.maak(imap)
        result = watcher.check_once()
        self.assertTrue(result["errors"])
        self.assertIn("app-wachtwoord", result["errors"][0])
        self.assertEqual(self.links, [])

    def test_zonder_gegevens_wordt_er_niets_geopend(self) -> None:
        gebruikt: list[str] = []

        def factory(config: MailboxConfig) -> Any:  # noqa: ANN401
            gebruikt.append("geopend")
            raise AssertionError("er mag geen verbinding worden gemaakt")

        watcher = MailboxWatcher(MailboxConfig(), self.dir / "mailbox.json", client_factory=factory)
        result = watcher.check_once()
        self.assertEqual(gebruikt, [])
        self.assertIn("mailserver", result["errors"][0])

    def test_status_toont_wat_er_ontbreekt(self) -> None:
        watcher = MailboxWatcher(MailboxConfig(host="imap.example.com"), self.dir / "mailbox.json")
        status = watcher.status()
        self.assertFalse(status["configured"])
        self.assertIn("e-mailadres", status["problem"])


if __name__ == "__main__":
    unittest.main()
