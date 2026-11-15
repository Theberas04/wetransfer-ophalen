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
from urllib.parse import urlparse

DEADLINE_SECONDS = 300.0


class MockGithub:
    """Nabootser van de GitHub-API, zodat de zelftest geen internet nodig heeft.

    Dezelfde nabootser serveert ook het releasebestand, zodat de keten
    "nieuwe release -> wachtrij -> bestand op schijf" echt te bewijzen is.
    """

    def __init__(self, payload: bytes, asset_name: str = "WeTransferOphalen-Windows.zip") -> None:
        self.payload = payload
        self.asset_name = asset_name
        self.repo = "Theberas04/wetransfer-ophalen"
        self.release_ready = False
        self.issue_link = ""
        self.base = ""
        self.httpd: Any = None

    def start(self) -> str:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - API van de basisklasse
                path = urlparse(self.path).path
                if path.endswith(f"/repos/{outer.repo}/releases"):
                    releases: list[dict[str, Any]] = []
                    if outer.release_ready:
                        releases.append(
                            {
                                "tag_name": "v1.1.0",
                                "draft": False,
                                "html_url": f"https://github.com/{outer.repo}/releases/tag/v1.1.0",
                                "assets": [
                                    {
                                        "id": 7,
                                        "name": outer.asset_name,
                                        "size": len(outer.payload),
                                        "browser_download_url": f"{outer.base}/download/{outer.asset_name}",
                                    }
                                ],
                            }
                        )
                    releases.append({"tag_name": "v1.0.0", "draft": False, "assets": []})
                    self._json(releases)
                elif path.endswith("/issues/comments"):
                    self._json([])
                elif path.endswith(f"/repos/{outer.repo}/issues"):
                    items: list[dict[str, Any]] = []
                    if outer.issue_link:
                        items.append(
                            {
                                "title": "Opnames",
                                "body": f"Hier de opnames: {outer.issue_link}",
                                "html_url": f"https://github.com/{outer.repo}/issues/1",
                            }
                        )
                    self._json(items)
                elif path.startswith("/download/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(outer.payload)))
                    self.send_header("Content-Disposition", f'attachment; filename="{outer.asset_name}"')
                    self.end_headers()
                    self.wfile.write(outer.payload)
                else:
                    self._json({"message": "niet gevonden"}, 404)

            def _json(self, data: Any, status: int = 200) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API van de basisklasse
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="github-nabootser", daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self.base

    @property
    def api_root(self) -> str:
        return self.base

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class MockMailbox:
    """Nabootser van een IMAP-mailbox met één WeTransfer-mail erin."""

    def __init__(self, link: str) -> None:
        self.link = link
        self.uids = ["7"]
        self.readonly = False

    def _bericht(self) -> bytes:
        from email.message import EmailMessage  # noqa: PLC0415

        message = EmailMessage()
        message["From"] = "WeTransfer <noreply@wetransfer.com>"
        message["Subject"] = "Je aangevraagde opnames staan klaar"
        message["Date"] = "Sun, 15 Nov 2026 12:00:00 +0100"
        message.set_content(f"Je aangevraagde opnames zijn binnen.\nDownload hier: {self.link}\nGroet, WeTransfer")
        return message.as_bytes()

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"LOGIN completed"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, commando: str, *args: Any) -> tuple[str, list[Any]]:
        if commando == "search":
            return "OK", [" ".join(self.uids).encode()]
        if commando == "fetch":
            return "OK", [(b"7 (RFC822 {1}", self._bericht()), b")"]
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]


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

        from main import DesktopApi  # noqa: PLC0415 - hier pas nodig

        # 7. De webversie: dezelfde pagina op een IP-adres in het netwerk
        report.mark("web")
        web_api = DesktopApi(config_dir=tmp_path / "web-config", allow_hosts=(host,))
        web_api.save_settings(
            {"web_enabled": True, "web_host": "127.0.0.1", "web_port": 0, "web_code": "zelftestcode"}
        )
        assert web_api.server is not None, "de webversie startte niet"
        web_basis = f"http://127.0.0.1:{web_api.server.port}"
        dicht = requests.get(f"{web_basis}/api/state", timeout=15)
        assert dicht.status_code == 401, f"zonder code hoort de webversie dicht te zijn (kreeg {dicht.status_code})"
        inlogpagina = requests.get(f"{web_basis}/", timeout=15)
        assert "Toegangscode" in inlogpagina.text, "de inlogpagina komt niet in beeld"
        web_sessie = requests.Session()
        inloggen = web_sessie.post(f"{web_basis}/api/login", json={"code": "zelftestcode"}, timeout=15)
        assert inloggen.status_code == 200, f"inloggen op de webversie mislukte ({inloggen.status_code})"
        web_toestand = web_sessie.get(f"{web_basis}/api/state", timeout=15).json()
        assert web_toestand.get("version") == __version__, "de webversie meldt een andere versie"
        web_add = web_sessie.post(
            f"{web_basis}/api/call",
            json={
                "method": "add_links",
                "arguments": {"text": plain.share_url(server.base_url), "password": ""},
            },
            timeout=15,
        ).json()
        assert web_add.get("ok") and web_add.get("added") == 1, f"link toevoegen vanaf het web mislukte: {web_add}"
        web_info = web_api.web_info()
        report.set(
            "web",
            {
                "address": web_info["address"],
                "needs_code": web_info["needs_code"],
                "items": len(web_api.get_state()["items"]),
            },
        )
        web_api.shutdown()

        # 8. De GitHub-bron: nieuwe release ophalen en links uit issues
        report.mark("github")
        from wtdl.github import GithubClient  # noqa: PLC0415

        github_mock = MockGithub(make_payload("release-bestand", 300_000))
        github_mock.start()
        github_api = DesktopApi(
            config_dir=tmp_path / "github-config",
            allow_hosts=(host,),
            skip_webhook_host_check=True,
            client_factory=lambda: WetransferClient(api_base=server.api_base, allow_hosts=(host,)),
        )
        github_api.github_client_factory = lambda: GithubClient(api_root=github_mock.api_root)
        github_api.save_settings(
            {
                "github_repos": github_mock.repo,
                "github_enabled": True,
                "download_dir": str(tmp_path / "github-downloads"),
                "slack_webhook": f"{server.base_url}/slack",
            }
        )
        eerste_controle = github_api.check_github()
        assert eerste_controle["ok"], f"eerste GitHub-controle mislukte: {eerste_controle}"
        assert github_api.get_state()["items"] == [], "bij het ijkpunt mag nog niets in de wachtrij staan"

        github_mock.release_ready = True
        github_mock.issue_link = plain.share_url(server.base_url)
        tweede_controle = github_api.check_github()
        assert tweede_controle["ok"], f"tweede GitHub-controle mislukte: {tweede_controle}"
        github_api.queue.start()
        deadline = time.monotonic() + 120
        while github_api.queue.running and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not github_api.queue.running, "de GitHub-wachtrij liep niet af"
        github_items = github_api.get_state()["items"]
        assert len(github_items) == 2, f"verwachtte 2 dingen uit GitHub, kreeg {len(github_items)}"
        staten = [item["state"] for item in github_items]
        assert all(state == "klaar" for state in staten), f"niet alles opgehaald: {staten}"
        releasebestand = next(item for item in github_items if item["direct"])
        inhoud = Path(releasebestand["path"]).read_bytes()
        digest = hashlib.sha256(inhoud).hexdigest()
        assert digest == sha256_of(github_mock.payload), "het releasebestand klopt niet"
        assert releasebestand["name"] == github_mock.asset_name, f"naam onverwacht: {releasebestand['name']}"
        github_meldingen = [message.get("text", "") for message in server.slack_messages]
        assert any("Bestand opgehaald" in text for text in github_meldingen), (
            f"geen Slack-melding over het releasebestand: {github_meldingen}"
        )
        report.set(
            "github",
            {
                "message": tweede_controle.get("message", ""),
                "items": [{"source": item["source"], "state": item["state"], "name": item["name"]} for item in github_items],
                "asset_sha256": digest,
            },
        )
        github_api.shutdown()
        github_mock.stop()

        # 9. De mailbox: een WeTransfer-mail met een aangevraagde opname erin
        report.mark("mailbox")
        mailbox_mock = MockMailbox(plain.share_url(server.base_url))
        mailbox_api = DesktopApi(
            config_dir=tmp_path / "mailbox-config",
            allow_hosts=(host,),
            skip_webhook_host_check=True,
            client_factory=lambda: WetransferClient(api_base=server.api_base, allow_hosts=(host,)),
        )
        mailbox_api.mailbox_client_factory = lambda config: mailbox_mock
        mailbox_api.save_settings(
            {
                # Geen mailserver invullen: die hoort uit het Gmail-adres te komen.
                "mailbox_host": "",
                "mailbox_user": "tom@gmail.com",
                "mailbox_password": "zelftest",
                "mailbox_enabled": True,
                "download_dir": str(tmp_path / "mailbox-downloads"),
                "slack_webhook": f"{server.base_url}/slack",
            }
        )
        assert mailbox_api._mailbox_config().effective_host() == "imap.gmail.com", (
            "de mailserver wordt niet uit het Gmail-adres gehaald"
        )
        mailbox_result = mailbox_api.check_mailbox()
        assert mailbox_result["ok"], f"mailbox lezen mislukte: {mailbox_result}"
        assert mailbox_mock.readonly, "de mailbox hoort alleen gelezen te worden"
        mailbox_items = mailbox_api.get_state()["items"]
        assert len(mailbox_items) == 1, f"verwachtte 1 link uit de mail, kreeg {len(mailbox_items)}"
        assert mailbox_items[0]["source"] == "mailbox", f"bron onverwacht: {mailbox_items[0]['source']}"
        mailbox_api.check_mailbox()
        assert len(mailbox_api.get_state()["items"]) == 1, "dezelfde mail is twee keer opgepakt"
        report.set(
            "mailbox",
            {
                "messages": mailbox_result.get("messages"),
                "links": mailbox_result.get("links", 0),
                "source": mailbox_items[0]["source"],
                "host": mailbox_api._mailbox_config().effective_host(),
            },
        )
        mailbox_api.shutdown()

        server.stop()

        # 10. Het venster: hier komt de .NET-kant van pywebview in beeld
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
