"""Zelftest van de gebouwde app: bewijst dat de .exe echt kan ophalen en melden.

De test draait dezelfde code als de app, maar tegen een lokale nabootser van
WeTransfer (``wtdl.mock``) en van Slack. Zo is de uitslag niet afhankelijk van
het internet of van een bestaande transfer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

DEADLINE_SECONDS = 240.0


class Report:
    """Schrijft de uitslag stap voor stap weg, zodat een crash ook iets achterlaat."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "ok": False,
            "steps": [],
            "error": "",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "frozen": bool(getattr(sys, "frozen", False)),
            "python": sys.version,
            "executable": sys.executable,
        }
        self.write()

    def mark(self, step: str) -> None:
        """Zet de stap *voor* hij begint, zodat de laatste regel de schuldige aanwijst."""

        self.data["steps"].append(step)
        self.data["current"] = step
        self.write()

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.write()

    def write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


def run_selftest(
    report_path: str, ui_path: Optional[Path] = None, core_only: bool = False
) -> int:
    """Draai de zelftest. ``core_only`` slaat de vensterstappen over (voor ontwikkelmachines)."""

    report = Report(report_path)
    watchdog = threading.Timer(DEADLINE_SECONDS, _timeout, args=(report,))
    watchdog.daemon = True
    watchdog.start()
    try:
        _run(report, ui_path, core_only=core_only)
    except BaseException as exc:  # noqa: BLE001 - alles moet in het rapport belanden
        report.data["ok"] = False
        report.data["error"] = f"{type(exc).__name__}: {exc}"
        report.data["traceback"] = traceback.format_exc()
        report.write()
        watchdog.cancel()
        os._exit(1)
    report.data["ok"] = True
    report.data["error"] = ""
    report.data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report.write()
    watchdog.cancel()
    os._exit(0)


def _timeout(report: Report) -> None:
    report.data["ok"] = False
    report.data["error"] = f"Zelftest liep vast in stap '{report.data.get('current')}'."
    ident = threading.main_thread().ident
    frames = sys._current_frames()
    frame = frames.get(ident) if ident is not None else None
    report.data["traceback"] = "".join(traceback.format_stack(frame))
    report.write()
    os._exit(2)


def _run(report: Report, ui_path: Optional[Path], core_only: bool = False) -> None:
    from wtdl import notify
    from wtdl.downloader import download, PART_SUFFIX
    from wtdl.mock import MockWetransferServer, MockTransfer, make_payload, sha256_of
    from wtdl.queue import DONE, FAILED, DownloadQueue
    from wtdl.store import AppLog, QueueStore, Settings, SettingsStore
    from wtdl.version import __version__
    from wtdl.watcher import scan_folder
    from wtdl.wetransfer import TransferError, WetransferClient

    report.mark("imports")
    import requests

    report.set(
        "versions",
        {
            "app": __version__,
            "requests": requests.__version__,
            "python": sys.version.split()[0],
        },
    )

    with tempfile.TemporaryDirectory(prefix="wto-selftest-") as tmp:
        tmp_path = Path(tmp)

        # 1. Instellingen, logboek en wachtrij op schijf
        report.mark("config")
        settings_path = tmp_path / "config" / "settings.json"
        store = SettingsStore(settings_path)
        settings = Settings(download_dir=str(tmp_path / "downloads"), slack_webhook="https://hooks.slack.com/services/T000/B000/XXXX")
        assert store.save(settings), "instellingen opslaan mislukte"
        loaded = store.load()
        assert loaded.download_dir == settings.download_dir, "instellingen komen er anders uit"
        log = AppLog(tmp_path / "config" / "app.log")
        log.write("zelftest")
        assert "zelftest" in log.tail(1)[0], "logboek schrijft niet"
        queue_store = QueueStore(tmp_path / "config" / "queue.json")
        queue_store.save([{"url": "https://we.tl/t-x", "state": "ophalen"}])
        assert queue_store.load()[0]["state"] == "ophalen", "wachtrij opslaan mislukte"

        # 2. Nabootser van WeTransfer en Slack
        report.mark("mock")
        server = MockWetransferServer()
        server.start()
        host = server.base_url.split("//")[1]
        plain = MockTransfer(
            transfer_id="1" * 24,
            security_hash="hash1",
            filename="wetransfer_fotos_2026-11-15_1200.zip",
            payload=make_payload("fotos", 1_500_000),
        )
        mailed = MockTransfer(
            transfer_id="2" * 24,
            security_hash="hash2",
            recipient_id="3" * 64,
            filename="Archiv.zip",
            payload=make_payload("archiv", 400_000),
        )
        expired = MockTransfer(
            transfer_id="4" * 24,
            security_hash="hash4",
            filename="wetransfer_oud.zip",
            payload=make_payload("oud", 2000),
            state="expired",
        )
        for transfer in (plain, mailed, expired):
            server.transfers[transfer.transfer_id] = transfer
        client = WetransferClient(api_base=server.api_base, allow_hosts=(host,))
        download_dir = tmp_path / "downloads"
        report.set("mock", {"base_url": server.base_url})

        # 3. De kern: link lezen, downloaden en hervatten
        report.mark("core")
        info, direct_link = client.resolve(mailed.share_url(server.base_url))
        assert info.recommended_filename == "Archiv.zip", f"naam onverwacht: {info.recommended_filename}"
        assert info.size == mailed.size, "grootte onverwacht"
        result = download(info, direct_link, download_dir, session=client.session)
        digest = hashlib.sha256(result.path.read_bytes()).hexdigest()
        assert digest == sha256_of(mailed.payload), "gedownloade inhoud wijkt af"
        assert result.path.name == "Archiv.zip", f"bestandsnaam onverwacht: {result.path.name}"

        big_info, big_link = client.resolve(plain.share_url(server.base_url))
        part = download_dir / f"{plain.filename}{PART_SUFFIX}"
        part.write_bytes(plain.payload[:500_000])
        resumed = download(big_info, big_link, download_dir, session=client.session)
        assert resumed.resumed_from == 500_000, f"niet hervat: {resumed.resumed_from}"
        assert (
            hashlib.sha256(resumed.path.read_bytes()).hexdigest() == sha256_of(plain.payload)
        ), "hervatte download wijkt af"
        assert any("bytes=500000-" in r["range"] for r in server.file_requests), "geen Range-verzoek gezien"

        expired_error = ""
        try:
            client.resolve(expired.share_url(server.base_url))
        except TransferError as exc:
            expired_error = exc.code
        assert expired_error == "verlopen", f"verlopen transfer gaf code '{expired_error}'"

        report.set(
            "download",
            {
                "file": result.path.name,
                "size": result.size,
                "sha256": digest,
                "resumed_from": resumed.resumed_from,
                "expired_code": expired_error,
                "requests": len(server.file_requests),
            },
        )

        # 4. Links uit een bestand in de wachtmap
        report.mark("watcher")
        watch_dir = tmp_path / "links"
        watch_dir.mkdir(parents=True, exist_ok=True)
        (watch_dir / "mail.txt").write_text(
            f"Hier de bestanden: {mailed.share_url(server.base_url)}\nGroet", encoding="utf-8"
        )
        scan = scan_folder(watch_dir, allow_hosts=(host,))
        assert scan.links == [mailed.share_url(server.base_url)], f"links onverwacht: {scan.links}"
        assert (watch_dir / "verwerkt").is_dir(), "verwerkte map ontbreekt"
        report.set("watcher", {"links": len(scan.links), "files": scan.files})

        # 5. De wachtrij met Slack-meldingen
        report.mark("queue")
        queue_settings = Settings(
            download_dir=str(tmp_path / "queue-downloads"),
            slack_webhook=f"{server.base_url}/slack",
        )
        queue = DownloadQueue(
            settings_provider=lambda: queue_settings,
            client_factory=lambda: WetransferClient(api_base=server.api_base, allow_hosts=(host,)),
            log=log,
            store=QueueStore(tmp_path / "config" / "queue2.json"),
            allow_hosts=(host,),
            skip_webhook_host_check=True,
        )
        queue.add_links(f"{plain.share_url(server.base_url)}\n{expired.share_url(server.base_url)}")
        queue.start()
        deadline = time.monotonic() + 120
        while queue.running and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not queue.running, "wachtrij liep niet af"
        states = [item.state for item in queue.items]
        assert DONE in states and FAILED in states, f"onverwachte statussen: {states}"
        messages = [message.get("text", "") for message in server.slack_messages]
        assert any("opgehaald" in text for text in messages), f"geen succesmelding: {messages}"
        assert any("mislukt" in text for text in messages), f"geen foutmelding: {messages}"
        report.set(
            "queue",
            {
                "states": states,
                "slack_messages": len(messages),
                "sample": messages[0][:120] if messages else "",
            },
        )

        # 6. Slack-bericht via dezelfde weg als in de app
        report.mark("slack")
        ok, error = notify.send_slack(queue_settings.slack_webhook, notify.test_text(), skip_host_check=True)
        assert ok, f"Slack-bericht mislukte: {error}"
        assert notify.validate_webhook("https://hooks.slack.com/services/T/B/X") is None, "webhook-controle klopt niet"
        assert notify.validate_webhook("https://example.com/x"), "webhook-controle had moeten klagen"
        server.stop()

        # 7. Het venster: hier komt de .NET-kant van pywebview in beeld
        if core_only:
            report.set("window", {"skipped": "alleen de kern getest (--core-only)"})
            return
        if ui_path is None or not Path(ui_path).exists():
            report.set("window", {"skipped": "geen ui/index.html gevonden"})
            return
        report.mark("clr")
        clr_version = ""
        try:
            import clr  # type: ignore[import-not-found]  # noqa: PLC0415

            clr_version = str(getattr(clr, "__version__", "aanwezig"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"pythonnet kon niet worden geladen: {type(exc).__name__}: {exc}") from exc
        report.set("clr", {"version": clr_version})

        report.mark("window")
        window_result = _run_window_step(report, tmp_path, ui_path)
        report.set("window", window_result)
        assert window_result.get("title") == "WeTransfer Ophalen", f"titel onverwacht: {window_result.get('title')}"
        assert window_result.get("app_ready"), "de pagina heeft de app-code niet geladen"
        dialog = window_result.get("dialog", {})
        assert dialog, "geen mapvenster-proef uitgevoerd"
        assert not dialog.get("error"), f"mapvenster mislukte: {dialog.get('error')}"
        assert not dialog.get("invoke_required"), (
            "het mapvenster wordt buiten de GUI-thread geopend; WinForms weigert dat"
        )


def _run_window_step(report: Report, tmp_path: Path, ui_path: Path) -> dict[str, Any]:
    """Open het echte venster, controleer de pagina en de mapkiezer, sluit weer."""

    import webview  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from main import DesktopApi  # noqa: PLC0415

    api = DesktopApi(config_dir=tmp_path / "window-config")
    window = webview.create_window(
        "WeTransfer Ophalen",
        str(ui_path),
        js_api=api,
        width=1100,
        height=760,
        background_color="#f4f7fb",
    )
    api.window = window
    outcome: dict[str, Any] = {}
    shown = threading.Event()
    window.events.shown += lambda *args: shown.set()

    def probe() -> None:
        try:
            if not shown.wait(90):
                outcome["error"] = "het venster kwam niet in beeld binnen 90 seconden"
                return
            time.sleep(1.5)
            try:
                outcome["title"] = window.evaluate_js("document.title")
                outcome["app_ready"] = bool(
                    window.evaluate_js("!!(window.app && typeof window.app.refresh === 'function')")
                )
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"pagina uitlezen mislukte: {type(exc).__name__}: {exc}"
            _probe_file_dialog(api, window, outcome)
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=probe, name="zelftest-venster", daemon=True).start()
    webview.start()
    return outcome


def _probe_file_dialog(api: Any, window: Any, outcome: dict[str, Any]) -> None:
    """Roep de mapkiezer aan zoals de pagina dat doet en kijk op welke thread hij landt."""

    dialog: dict[str, Any] = {}
    form = getattr(window, "native", None)
    original = window.create_file_dialog

    def stub(*args: Any, **kwargs: Any) -> None:
        dialog["thread"] = threading.current_thread().name
        dialog["invoke_required"] = bool(getattr(form, "InvokeRequired", False))
        return None

    try:
        window.create_file_dialog = stub
        dialog["result"] = api.choose_folder("download")
    except Exception as exc:  # noqa: BLE001
        dialog["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        window.create_file_dialog = original
    outcome["dialog"] = dialog
