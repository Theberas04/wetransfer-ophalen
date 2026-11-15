"""Tests voor het lezen van WeTransfer-links en het praten met de API."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl.mock import MockWetransferServer, MockTransfer, make_payload  # noqa: E402
from wtdl.wetransfer import (  # noqa: E402
    TransferError,
    WetransferClient,
    extract_links,
    human_size,
    parse_share_url,
)


class ExtractLinksTest(unittest.TestCase):
    def test_haalt_short_en_lange_links_eruit(self) -> None:
        text = (
            "Hoi, hier de bestanden: https://we.tl/t-abc123XYZ\n"
            "En nog een: https://wetransfer.com/downloads/aaaaaaaaaaaaaaaaaaaaaaaa/bbbbbb\n"
            "Groet!"
        )
        links = extract_links(text)
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("https://we.tl/"))

    def test_strikt_lege_en_andere_links(self) -> None:
        text = "Zie https://drive.google.com/file/d/123 en https://www.youtube.com/watch?v=1"
        self.assertEqual(extract_links(text), [])

    def test_knipt_leestekens_aan_het_eind_eraf(self) -> None:
        text = "Zie (https://we.tl/t-abc123)."
        self.assertEqual(extract_links(text), ["https://we.tl/t-abc123"])

    def test_laat_dubbelen_weg(self) -> None:
        text = "https://we.tl/t-abc https://we.tl/t-abc https://we.tl/t-def"
        self.assertEqual(len(extract_links(text)), 2)

    def test_herkent_directe_downloadlink(self) -> None:
        url = "https://download.wetransfer.com/eugv/x/y/Archiv.zip?token=abc"
        self.assertEqual(extract_links(url), [url])


class ParseShareUrlTest(unittest.TestCase):
    def test_twee_delen(self) -> None:
        link = parse_share_url("https://wetransfer.com/downloads/abc123/hash456")
        self.assertEqual(link.transfer_id, "abc123")
        self.assertEqual(link.security_hash, "hash456")
        self.assertIsNone(link.recipient_id)

    def test_drie_delen_met_ontvanger(self) -> None:
        link = parse_share_url("https://wetransfer.com/downloads/abc123/ontvanger/hash456?t_exp=1")
        self.assertEqual(link.transfer_id, "abc123")
        self.assertEqual(link.recipient_id, "ontvanger")
        self.assertEqual(link.security_hash, "hash456")

    def test_directe_link(self) -> None:
        link = parse_share_url("https://download.wetransfer.com/x/y/Archiv.zip?token=1")
        self.assertTrue(link.direct)

    def test_weigert_andere_links(self) -> None:
        with self.assertRaises(TransferError) as context:
            parse_share_url("https://example.com/downloads/a/b")
        self.assertEqual(context.exception.code, "geen_link")

    def test_weigert_onvolledige_link(self) -> None:
        with self.assertRaises(TransferError):
            parse_share_url("https://wetransfer.com/downloads/abc123")


class HumanSizeTest(unittest.TestCase):
    def test_formaten(self) -> None:
        self.assertEqual(human_size(None), "onbekend")
        self.assertEqual(human_size(900), "900 B")
        self.assertEqual(human_size(2048), "2.0 kB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = MockWetransferServer()
        cls.server.start()
        cls.host = cls.server.base_url.split("//")[1]
        cls.transfers = {t.transfer_id: t for t in cls.server.default_transfers()}
        cls.server.transfers.update(cls.transfers)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def client(self) -> WetransferClient:
        return WetransferClient(api_base=self.server.api_base, allow_hosts=(self.host,))

    def test_volledige_route_met_ontvanger(self) -> None:
        transfer = self.transfers["b" * 24]
        client = self.client()
        info, direct_link = client.resolve(transfer.share_url(self.server.base_url))
        self.assertEqual(info.state, "downloadable")
        self.assertEqual(info.recommended_filename, "Archiv.zip")
        self.assertEqual(info.size, transfer.size)
        self.assertIn("/files/", direct_link)

    def test_korte_link_wordt_gevolgd(self) -> None:
        transfer = self.transfers["a" * 24]
        short = transfer.short_url().replace("https://we.tl", self.server.base_url)
        client = self.client()
        info, direct_link = client.resolve(short)
        self.assertEqual(info.link.transfer_id, transfer.transfer_id)
        self.assertTrue(direct_link)

    def test_verlopen_transfer_geeft_duidelijke_fout(self) -> None:
        transfer = self.transfers["d" * 24]
        client = self.client()
        with self.assertRaises(TransferError) as context:
            client.resolve(transfer.share_url(self.server.base_url))
        self.assertEqual(context.exception.code, "verlopen")
        self.assertIn("verlopen", context.exception.message)

    def test_onbekende_transfer(self) -> None:
        client = self.client()
        url = f"{self.server.base_url}/downloads/ffffffffffffffffffffffff/geenhash"
        with self.assertRaises(TransferError) as context:
            client.resolve(url)
        self.assertEqual(context.exception.code, "niet_gevonden")

    def test_wachtwoord_nodig_en_daarna_goed(self) -> None:
        transfer = self.transfers["e" * 24]
        client = self.client()
        url = transfer.share_url(self.server.base_url)
        with self.assertRaises(TransferError) as context:
            client.resolve(url)
        self.assertEqual(context.exception.code, "wachtwoord_nodig")
        info, direct_link = client.resolve(url, password="geheim123")
        self.assertTrue(info.downloadable)
        self.assertTrue(direct_link)

    def test_verkeerd_wachtwoord(self) -> None:
        transfer = self.transfers["e" * 24]
        client = self.client()
        with self.assertRaises(TransferError) as context:
            client.resolve(transfer.share_url(self.server.base_url), password="fout")
        self.assertEqual(context.exception.code, "wachtwoord_fout")

    def test_netwerkfout_wordt_nette_fout(self) -> None:
        client = WetransferClient(api_base="http://127.0.0.1:9/api/v4", timeout=2)
        link = parse_share_url("https://wetransfer.com/downloads/abc/hash")
        with self.assertRaises(TransferError) as context:
            client.prepare(link)
        self.assertEqual(context.exception.code, "verbinding")


class MockServerTest(unittest.TestCase):
    def test_payload_is_deterministisch(self) -> None:
        self.assertEqual(make_payload("x", 32), make_payload("x", 32))
        self.assertNotEqual(make_payload("x", 32), make_payload("y", 32))

    def test_share_url_vormen(self) -> None:
        plain = MockTransfer("a" * 24, "h", "x.zip", b"data")
        self.assertNotIn("recipient", plain.share_url("http://x"))
        with_recipient = MockTransfer("a" * 24, "h", "x.zip", b"data", recipient_id="r")
        self.assertEqual(
            with_recipient.share_url("http://x"),
            f"http://x/downloads/{'a' * 24}/r/h",
        )


if __name__ == "__main__":
    unittest.main()
