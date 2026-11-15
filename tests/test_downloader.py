"""Tests voor het downloaden: naamgeving, hervatten en fouten."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl.downloader import (  # noqa: E402
    DownloadCancelled,
    cleanup_partials,
    download,
    name_from_direct_link,
    name_from_headers,
    sanitize_filename,
    unique_path,
)
from wtdl.mock import MockWetransferServer, MockTransfer, make_payload, sha256_of  # noqa: E402
from wtdl.wetransfer import TransferError, TransferInfo, WetransferClient, parse_share_url  # noqa: E402


class NameTest(unittest.TestCase):
    def test_verboden_tekens(self) -> None:
        self.assertEqual(sanitize_filename('a<b>c:d"e|f?g*h.zip'), "a_b_c_d_e_f_g_h.zip")

    def test_pad_wordt_afgekapt(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Users\\tom\\Archiv.zip"), "Archiv.zip")

    def test_gereserveerde_namen(self) -> None:
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename("lpt1.log"), "_lpt1.log")

    def test_leeg_geeft_fallback(self) -> None:
        self.assertEqual(sanitize_filename("", "terugval.zip"), "terugval.zip")
        self.assertEqual(sanitize_filename("   ", "terugval.zip"), "terugval.zip")

    def test_lange_naam(self) -> None:
        name = sanitize_filename("x" * 300 + ".zip")
        self.assertLessEqual(len(name), 130)

    def test_naam_uit_link(self) -> None:
        self.assertEqual(
            name_from_direct_link("https://download.wetransfer.com/a/b/Mijn%20Bestand.zip?token=1"),
            "Mijn Bestand.zip",
        )

    def test_naam_uit_header(self) -> None:
        response = requests.Response()
        response.headers["content-disposition"] = 'attachment; filename="Archiv.zip"'
        self.assertEqual(name_from_headers(response), "Archiv.zip")
        response.headers["content-disposition"] = "attachment; filename*=UTF-8''Na%C3%AFef.zip"
        self.assertEqual(name_from_headers(response), "Naïef.zip")
        response.headers["content-disposition"] = ""
        self.assertEqual(name_from_headers(response), "")

    def test_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "Archiv.zip").write_bytes(b"x")
            self.assertEqual(unique_path(directory, "Archiv.zip").name, "Archiv (2).zip")


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.server = MockWetransferServer()
        self.server.start()
        self.host = self.server.base_url.split("//")[1]
        self.client = WetransferClient(api_base=self.server.api_base, allow_hosts=(self.host,))

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def _transfer(self, **kwargs) -> MockTransfer:
        transfer = MockTransfer(
            transfer_id="a" * 24,
            security_hash="hash",
            filename=kwargs.pop("filename", "wetransfer_fotos.zip"),
            payload=kwargs.pop("payload", make_payload("fotos", 500_000)),
            **kwargs,
        )
        self.server.transfers[transfer.transfer_id] = transfer
        return transfer

    def test_volledige_download(self) -> None:
        transfer = self._transfer()
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))
        progress: list[tuple[int, int | None]] = []
        result = download(
            info,
            direct_link,
            self.directory,
            on_progress=lambda received, total, speed: progress.append((received, total)),
            session=self.client.session,
        )
        self.assertEqual(result.path.name, "wetransfer_fotos.zip")
        self.assertEqual(hashlib.sha256(result.path.read_bytes()).hexdigest(), sha256_of(transfer.payload))
        self.assertFalse(result.skipped)
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], transfer.size)
        self.assertFalse(list(self.directory.glob("*.part")))

    def test_hervat_vanaf_een_half_bestand(self) -> None:
        """Een eerder afgebroken download gaat verder met een Range-verzoek."""

        transfer = self._transfer(payload=make_payload("groot", 900_000))
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))
        part = self.directory / "wetransfer_fotos.zip.wetransfer.part"
        part.write_bytes(transfer.payload[:250_000])

        result = download(info, direct_link, self.directory, session=self.client.session)

        self.assertEqual(result.resumed_from, 250_000)
        self.assertEqual(hashlib.sha256(result.path.read_bytes()).hexdigest(), sha256_of(transfer.payload))
        ranges = [request["range"] for request in self.server.file_requests if request["range"]]
        self.assertIn("bytes=250000-", ranges)
        self.assertFalse(part.exists())

    def test_afgebroken_verbinding_wordt_opnieuw_geprobeerd(self) -> None:
        transfer = self._transfer(payload=make_payload("groot2", 900_000), fail_after=250_000)
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))

        def refresh() -> str:
            _, fresh = self.client.resolve(transfer.share_url(self.server.base_url))
            return fresh

        result = download(
            info, direct_link, self.directory, refresh=refresh, session=self.client.session
        )
        self.assertEqual(hashlib.sha256(result.path.read_bytes()).hexdigest(), sha256_of(transfer.payload))
        self.assertGreaterEqual(result.attempts, 2)
        self.assertEqual(result.path.stat().st_size, transfer.size)

    def test_verlopen_link_wordt_vernieuwd(self) -> None:
        transfer = self._transfer(payload=make_payload("verlopen", 300_000))
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))
        calls: list[int] = []

        def refresh() -> str:
            calls.append(1)
            _, fresh = self.client.resolve(transfer.share_url(self.server.base_url))
            return fresh

        kapot = f"{self.server.base_url}/files/bestaat-niet.bin"
        result = download(info, kapot, self.directory, refresh=refresh, session=self.client.session)
        self.assertEqual(hashlib.sha256(result.path.read_bytes()).hexdigest(), sha256_of(transfer.payload))
        self.assertEqual(len(calls), 1)

    def test_stoppen_laat_halve_download_staan(self) -> None:
        transfer = self._transfer(payload=make_payload("stop", 400_000))
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DownloadCancelled):
            download(info, direct_link, self.directory, cancel=cancel, session=self.client.session)
        self.assertTrue(list(self.directory.glob("*.wetransfer.part")))

    def test_bestaand_compleet_bestand_wordt_overgeslagen(self) -> None:
        transfer = self._transfer(payload=make_payload("dubbel", 200_000))
        info, direct_link = self.client.resolve(transfer.share_url(self.server.base_url))
        first = download(info, direct_link, self.directory, session=self.client.session)
        second = download(info, direct_link, self.directory, session=self.client.session)
        self.assertTrue(second.skipped)
        self.assertEqual(second.path, first.path)

    def test_onbekende_transfer_geeft_fout(self) -> None:
        info = TransferInfo(link=parse_share_url("https://wetransfer.com/downloads/abc/hash"))
        with self.assertRaises(TransferError):
            download(info, f"{self.server.base_url}/files/bestaat-niet.bin", self.directory)

    def test_cleanup_partials(self) -> None:
        (self.directory / "leeg.wetransfer.part").write_bytes(b"")
        (self.directory / "half.wetransfer.part").write_bytes(b"data")
        self.assertEqual(cleanup_partials(self.directory), 1)
        self.assertTrue((self.directory / "half.wetransfer.part").exists())


if __name__ == "__main__":
    unittest.main()
