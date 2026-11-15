"""Tests voor de wachtrij, meldingen, instellingen en de wachtmap."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtdl import notify  # noqa: E402
from wtdl.mock import MockWetransferServer, MockTransfer, make_payload, sha256_of  # noqa: E402
from wtdl.queue import DONE, FAILED, PENDING, DownloadQueue, QueueItem  # noqa: E402
from wtdl.store import AppLog, QueueStore, Settings, SettingsStore, open_in_explorer  # noqa: E402
from wtdl.watcher import FolderWatcher, scan_folder  # noqa: E402
from wtdl.wetransfer import WetransferClient  # noqa: E402


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class NotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockWetransferServer()
        self.server.start()
        self.webhook = f"{self.server.base_url}/slack"

    def tearDown(self) -> None:
        self.server.stop()

    def test_webhook_validatie(self) -> None:
        self.assertIn("nog geen", notify.validate_webhook("") or "")
        self.assertIn("https", notify.validate_webhook("http://hooks.slack.com/x") or "")
        self.assertIn("Slack-webhook", notify.validate_webhook("https://example.com/x") or "")
        self.assertIsNone(notify.validate_webhook("https://hooks.slack.com/services/T/B/X"))

    def test_bericht_versturen(self) -> None:
        ok, error = notify.send_slack(self.webhook, "hallo", skip_host_check=True)
        self.assertTrue(ok, error)
        self.assertEqual(self.server.slack_messages[0]["text"], "hallo")

    def test_onbereikbare_slack(self) -> None:
        ok, error = notify.send_slack("http://127.0.0.1:9/x", "hallo", skip_host_check=True)
        self.assertFalse(ok)
        self.assertIn("niet bereikbaar", error)

    def test_berichten_bevatten_de_bestandsnaam(self) -> None:
        item = QueueItem(url="https://we.tl/t-x", name="Archiv.zip", size=320_000_000, seconds=65)
        self.assertIn("Archiv.zip", notify.done_text(item, "D:\\WeTransfer"))
        self.assertIn("305.2 MB", notify.done_text(item, "D:\\WeTransfer"))
        item.state = FAILED
        item.error = "Deze transfer is verlopen."
        self.assertIn("verlopen", notify.failed_text(item))
        item.state = DONE
        self.assertIn("1 van 1", notify.summary_text([item]))


class SettingsTest(unittest.TestCase):
    def test_rondje_opslaan_en_lezen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "settings.json")
            settings = Settings(download_dir=str(Path(tmp) / "downloads"), slack_webhook="https://hooks.slack.com/x")
            self.assertTrue(store.save(settings))
            loaded = store.load()
            self.assertEqual(loaded.slack_webhook, "https://hooks.slack.com/x")
            self.assertEqual(loaded.notify_mode, "altijd")

    def test_onbekende_sleutels_en_ondergrens(self) -> None:
        settings = Settings.from_dict({"onzin": 1, "watch_interval": 1})
        self.assertEqual(settings.watch_interval, 5)

    def test_kapot_bestand_geeft_standaardwaarden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{niet eens json", encoding="utf-8")
            store = SettingsStore(path)
            settings = store.load()
            self.assertTrue(store.load_error)
            self.assertEqual(settings.notify_mode, "altijd")

    def test_wachtmap_standaard(self) -> None:
        settings = Settings()
        self.assertTrue(settings.effective_watch_dir())
        settings.watch_dir = "C:\\Links"
        self.assertEqual(settings.effective_watch_dir(), "C:\\Links")

    def test_log_schrijft_naar_bestand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = AppLog(Path(tmp) / "app.log")
            log.write("eerste regel")
            log.write("tweede regel")
            self.assertIn("tweede regel", log.tail(1)[0])
            self.assertIn("eerste regel", (Path(tmp) / "app.log").read_text(encoding="utf-8"))

    def test_map_openen_mist(self) -> None:
        ok, error = open_in_explorer("/bestaat/niet")
        self.assertFalse(ok)
        self.assertIn("bestaat niet", error)


class QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "downloads"
        self.directory.mkdir(parents=True)
        self.server = MockWetransferServer()
        self.server.start()
        self.host = self.server.base_url.split("//")[1]
        self.webhook = f"{self.server.base_url}/slack"
        self.settings = Settings(download_dir=str(self.directory), slack_webhook=self.webhook)
        self.log = AppLog(Path(self.tmp.name) / "app.log")
        self.transfers = {t.transfer_id: t for t in self.server.default_transfers()}
        self.server.transfers.update(self.transfers)
        self.queue = DownloadQueue(
            settings_provider=lambda: self.settings,
            client_factory=lambda: WetransferClient(
                api_base=self.server.api_base, allow_hosts=(self.host,)
            ),
            log=self.log,
            store=QueueStore(Path(self.tmp.name) / "queue.json"),
            allow_hosts=(self.host,),
            skip_webhook_host_check=True,
        )

    def tearDown(self) -> None:
        self.queue.stop()
        self.server.stop()
        self.tmp.cleanup()

    def url(self, transfer: MockTransfer) -> str:
        return transfer.share_url(self.server.base_url)

    def run_queue(self) -> None:
        self.assertTrue(self.queue.start())
        self.assertTrue(wait_until(lambda: not self.queue.running, timeout=60))

    def test_twee_transfers_ophalen_en_melden(self) -> None:
        first, second = self.transfers["a" * 24], self.transfers["b" * 24]
        added, skipped = self.queue.add_links(f"{self.url(first)}\n{self.url(second)}")
        self.assertEqual(added, 2)
        self.assertEqual(skipped, [])
        self.run_queue()
        states = {item.url: item.state for item in self.queue.items}
        self.assertEqual(set(states.values()), {DONE})
        for transfer in (first, second):
            path = self.directory / transfer.filename
            self.assertTrue(path.exists(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha256_of(transfer.payload))
        texts = [message["text"] for message in self.server.slack_messages]
        self.assertEqual(len(texts), 3)  # twee keer "klaar" en één samenvatting
        self.assertIn("WeTransfer opgehaald", texts[0])
        self.assertIn("2 van 2", texts[-1])

    def test_mislukte_transfer_geeft_slackmelding(self) -> None:
        expired = self.transfers["d" * 24]
        self.queue.add_links(self.url(expired))
        self.run_queue()
        item = self.queue.items[0]
        self.assertEqual(item.state, FAILED)
        self.assertIn("verlopen", item.error)
        self.assertTrue(any("mislukt" in message["text"] for message in self.server.slack_messages))

    def test_dubbele_link_wordt_overgeslagen(self) -> None:
        first = self.transfers["a" * 24]
        self.queue.add_links(self.url(first))
        added, skipped = self.queue.add_links(self.url(first))
        self.assertEqual(added, 0)
        self.assertEqual(len(skipped), 1)

    def test_geen_slackmeldingen_als_uit(self) -> None:
        self.settings.notify_mode = "uit"
        self.queue.add_links(self.url(self.transfers["a" * 24]))
        self.run_queue()
        self.assertEqual(self.server.slack_messages, [])

    def test_opnieuw_en_opruimen(self) -> None:
        self.queue.add_links(self.url(self.transfers["d" * 24]))
        self.run_queue()
        item = self.queue.items[0]
        self.assertTrue(self.queue.retry(item.id))
        self.assertEqual(self.queue.items[0].state, PENDING)
        self.queue.items[0].state = DONE
        self.assertEqual(self.queue.clear_finished(), 1)
        self.assertEqual(self.queue.items, [])

    def test_wachtrij_bewaard_en_herstart(self) -> None:
        store = QueueStore(Path(self.tmp.name) / "queue2.json")
        item = QueueItem(url="https://we.tl/t-abc", state="ophalen", name="x.zip")
        store.save([item.as_dict()])
        loaded = [QueueItem.from_dict(data) for data in store.load()]
        self.assertEqual(loaded[0].state, PENDING)
        self.assertEqual(loaded[0].name, "x.zip")


class WatcherTest(unittest.TestCase):
    def test_links_uit_bestanden_halen_en_verplaatsen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "mail.txt").write_text(
                "Beste Tom, hier de bestanden: https://we.tl/t-abc123\nGroet", encoding="utf-8"
            )
            (folder / "leeg.txt").write_text("geen link hier", encoding="utf-8")
            (folder / "foto.jpg").write_bytes(b"\xff\xd8\xff")
            result = scan_folder(folder)
            self.assertEqual(result.links, ["https://we.tl/t-abc123"])
            self.assertEqual(result.files, ["mail.txt"])
            self.assertTrue((folder / "verwerkt").is_dir())
            self.assertTrue(list((folder / "verwerkt").glob("*mail.txt")))
            self.assertFalse((folder / "mail.txt").exists())
            self.assertTrue((folder / "leeg.txt").exists())

    def test_wachter_meldt_elke_link_een_keer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            seen: list[list[str]] = []
            watcher = FolderWatcher(str(folder), 5, on_links=seen.append)
            (folder / "a.txt").write_text("https://we.tl/t-een", encoding="utf-8")
            watcher.scan_once()
            (folder / "b.txt").write_text("https://we.tl/t-twee", encoding="utf-8")
            watcher.scan_once()
            self.assertEqual(seen, [["https://we.tl/t-een"], ["https://we.tl/t-twee"]])

    def test_ontbrekende_map_wordt_aangemaakt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "nog-niet"
            result = scan_folder(folder)
            self.assertTrue(folder.is_dir())
            self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()
