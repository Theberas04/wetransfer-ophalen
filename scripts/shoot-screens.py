"""Schermafbeeldingen van de app-interface maken (voor README en Slack).

De pagina praat normaal met de app via window.pywebview; dit script zet daar een
nepbrug met voorbeeldtransfers neer, zodat de schermafbeeldingen altijd dezelfde
nette demo tonen in plaats van een lege lijst.

Gebruik (op een machine met Chromium):
    python scripts/shoot-screens.py [--chromium /pad/naar/chrome]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
DOCS = ROOT / "docs"
PORT = int(os.environ.get("WTO_CDP_PORT", "9227"))

DEMO_STATE = {
    "version": "1.1.0",
    "app_name": "WeTransfer Ophalen",
    "config_dir": "C:\\Users\\Tom\\AppData\\Roaming\\WeTransfer Ophalen",
    "settings": {
        "download_dir": "C:\\Users\\Tom\\Downloads\\WeTransfer",
        "slack_webhook": "https://hooks.slack.com/services/T01ABC/B02DEF/xxxxxxxxxxxxxxxxxxxxxxxx",
        "notify_mode": "altijd",
        "watch_enabled": True,
        "watch_dir": "",
        "watch_interval": 20,
        "auto_start": True,
        "autostart_with_windows": True,
        "sound_on_finish": True,
        "open_folder_after": False,
        "web_enabled": True,
        "web_host": "0.0.0.0",
        "web_port": 8756,
        "web_code": "k7Fq2mZp",
        "github_enabled": True,
        "github_repos": "Theberas04/wetransfer-ophalen",
        "github_interval": 15,
        "github_watch_releases": True,
        "github_watch_links": True,
        "github_download_releases": True,
        "github_token": "",
        "mailbox_enabled": True,
        "mailbox_host": "imap.gmail.com",
        "mailbox_port": 993,
        "mailbox_user": "tom@werdensmedia.nl",
        "mailbox_password": "app-wachtwoord",
        "mailbox_folder": "INBOX",
        "mailbox_sender": "wetransfer",
        "mailbox_days": 30,
        "mailbox_interval": 10,
    },
    "watch_dir_effective": "C:\\Users\\Tom\\Downloads\\WeTransfer-links",
    "autostart_registered": True,
    "platform": "nt",
    "web": {
        "enabled": True,
        "address": "http://192.168.1.42:8756",
        "host": "0.0.0.0",
        "port": 8756,
        "code": "k7Fq2mZp",
        "needs_code": True,
        "local_ip": "192.168.1.42",
        "started_at": 1763250000.0,
    },
    "sources": {
        "running": True,
        "sources": [
            {
                "name": "github", "label": "GitHub", "interval_minutes": 15,
                "last_run": 1763250180.0, "last_run_iso": "15-11-2026 21:58",
                "last_error": "",
                "last_summary": "1 nieuwe release(s), 1 releasebestand(en) in de wachtrij",
                "running": False,
            },
            {
                "name": "mailbox", "label": "Mailbox", "interval_minutes": 10,
                "last_run": 1763250120.0, "last_run_iso": "15-11-2026 21:57",
                "last_error": "",
                "last_summary": "2 bericht(en) gelezen, 1 link(s) gevonden",
                "running": False,
            },
        ],
    },
    "github": {
        "repos": ["Theberas04/wetransfer-ophalen"],
        "states": {
            "Theberas04/wetransfer-ophalen": {
                "last_check": 1763250180.0, "last_check_iso": "15-11-2026 21:58",
                "last_error": "", "last_new": 2, "initialized": True,
            }
        },
        "last_result": {},
        "default_repo": "Theberas04/wetransfer-ophalen",
    },
    "mailbox": {
        "configured": True, "problem": "", "host": "imap.gmail.com",
        "user": "tom@werdensmedia.nl", "folder": "INBOX", "seen": 14,
        "last_result": {"checked_at": 1763250120.0, "messages": 2, "links": ["https://we.tl/t-abc"], "errors": []},
    },
    "items": [
        {
            "id": "a1", "url": "https://we.tl/t-kLu8aUbhSy", "state": "ophalen",
            "name": "wetransfer_fotos-shoot_2026-11-15_1200.zip", "size": 1288490188,
            "received": 812345678, "speed": 12400000, "percent": 63.1, "path": "",
            "error": "", "hint": "", "skipped": False, "seconds": 0,
            "source": "wachtmap", "direct": False,
        },
        {
            "id": "a2", "url": "https://wetransfer.com/downloads/…/e69897", "state": "klaar",
            "name": "Archiv.zip", "size": 320082433, "received": 320082433, "speed": 0,
            "percent": 100.0, "path": "C:\\Users\\Tom\\Downloads\\WeTransfer\\Archiv.zip",
            "error": "", "hint": "", "skipped": False, "seconds": 42,
            "source": "mailbox", "direct": False, "added_at": 1763250100.0,
        },
        {
            "id": "a4", "url": "https://github.com/Theberas04/wetransfer-ophalen/releases/download/v1.1.0/WeTransferOphalen-Windows.zip",
            "state": "klaar", "name": "WeTransferOphalen-Windows.zip", "size": 14430007,
            "received": 14430007, "speed": 0, "percent": 100.0,
            "path": "C:\\Users\\Tom\\Downloads\\WeTransfer\\github\\Theberas04-wetransfer-ophalen\\v1.1.0\\WeTransferOphalen-Windows.zip",
            "error": "", "hint": "", "skipped": False, "seconds": 3, "source": "github", "direct": True,
            "added_at": 1763250200.0,
        },
        {
            "id": "a3", "url": "https://we.tl/t-M35wj2Ba19", "state": "mislukt",
            "name": "wetransfer_oude-uitzending.zip", "size": 0, "received": 0, "speed": 0,
            "percent": None, "path": "",
            "error": "Deze transfer is verlopen. (verliep op 2026-11-10T09:14:00Z)",
            "hint": "Vraag de afzender om de bestanden opnieuw te sturen.",
            "skipped": False, "seconds": 0,
            "source": "github", "direct": False,
        },
    ],
    "running": True,
    "pending": 1,
    "log": [
        "2026-11-15 21:52:10 Versie 1.1.0 gestart. Instellingen in C:\\Users\\Tom\\AppData\\Roaming\\WeTransfer Ophalen",
        "2026-11-15 21:52:10 Wachtmap actief: C:\\Users\\Tom\\Downloads\\WeTransfer-links (elke 20 s)",
        "2026-11-15 21:52:11 Webversie actief op http://192.168.1.42:8756",
        "2026-11-15 21:52:11 Bronnen actief: GitHub, Mailbox",
        "2026-11-15 21:52:41 1 link(s) toegevoegd aan de wachtrij.",
        "2026-11-15 21:52:41 Wachtrij gestart.",
        "2026-11-15 21:52:41 Ophalen gestart: https://wetransfer.com/downloads/b207731623184dfc2beb2ee8429836fe20261115060956/e54851a2f0d08a526964ac8fcaa48b6b/e69897",
        "2026-11-15 21:53:23 Klaar: Archiv.zip (320082433 bytes, 42.1 s)",
        "2026-11-15 21:53:23 Slack-melding verstuurd.",
        "2026-11-15 21:57:00 Mailbox: 'Je aangevraagde opnames staan klaar' van WeTransfer <noreply@wetransfer.com> — 1 WeTransfer-link(s).",
        "2026-11-15 21:58:00 GitHub (Theberas04/wetransfer-ophalen): nieuwe release v1.1.0.",
        "2026-11-15 21:58:01 Bestand van github in de wachtrij: WeTransferOphalen-Windows.zip",
        "2026-11-15 21:58:04 Klaar: WeTransferOphalen-Windows.zip (14430007 bytes, 3.2 s)",
        "2026-11-15 21:57:02 Ophalen gestart: https://we.tl/t-kLu8aUbhSy",
        "2026-11-15 21:58:40 Mislukt (verlopen): Deze transfer is verlopen. Vraag de afzender om de bestanden opnieuw te sturen.",
    ],
}

STUB = """
<script>
window.pywebview = { api: {
  get_state: async () => (__STATE__),
  add_links: async () => ({ok:true, added:1, skipped:0, message:'1 link(s) toegevoegd.'}),
  choose_link_file: async () => ({ok:false, cancelled:true}),
  scan_watch_folder: async () => ({ok:true, links:[], files:[], errors:[]}),
  start_queue: async () => ({ok:true, started:true}),
  stop_queue: async () => ({ok:true}),
  clear_finished: async () => ({ok:true, removed:1}),
  retry_item: async () => ({ok:true}),
  remove_item: async () => ({ok:true}),
  choose_folder: async () => ({ok:true, path:'C:\\\\Users\\\\Tom\\\\Downloads\\\\WeTransfer'}),
  open_folder: async () => ({ok:true}),
  open_log_file: async () => ({ok:true}),
  test_slack: async () => ({ok:true}),
  save_settings: async () => ({ok:true}),
  check_github: async () => ({ok:true, message:'1 nieuwe release(s), 1 releasebestand(en) in de wachtrij.'}),
  check_mailbox: async () => ({ok:true, message:'2 bericht(en) gelezen, 1 link(s) gevonden.'}),
  check_all_sources: async () => ({ok:true, results:{github:{ok:true, message:'niets nieuws.'}, mailbox:{ok:true, message:'1 bericht gelezen.'}}}),
  clear_log: async () => ({ok:true}),
}};
</script>
"""


def find_chromium(explicit: str = "") -> str:
    if explicit:
        return explicit
    for pattern in ("chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(pattern)
        if found:
            return found
    candidates = sorted(Path.home().glob(".hermes/tools/chromium-*/chrome-linux64/chrome"))
    if candidates:
        return str(candidates[-1])
    raise SystemExit("Geen Chromium gevonden; geef het pad mee met --chromium.")


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schermafbeeldingen van de interface maken")
    parser.add_argument("--chromium", default="", help="pad naar het chrome-binary")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path.home() / "tools"))
    from cdp import CDP  # noqa: PLC0415 - hulpje van deze machine

    if not port_is_free(args.port):
        raise SystemExit(f"Poort {args.port} is bezet; kies een andere met --port.")

    work = Path(tempfile.mkdtemp(prefix="wto-shots-"))
    shutil.copytree(UI, work / "ui")
    state = json.dumps(DEMO_STATE, ensure_ascii=False)
    html = (UI / "index.html").read_text(encoding="utf-8")
    demo = html.replace(
        '<script src="app.js"></script>',
        STUB.replace("__STATE__", state) + '<script src="app.js"></script>',
    )
    (work / "ui" / "demo.html").write_text(demo, encoding="utf-8")

    chrome = find_chromium(args.chromium)
    profile = work / "profiel"
    process = subprocess.Popen(
        [
            chrome, "--headless=new", f"--remote-debugging-port={args.port}",
            f"--window-size={args.width},{args.height}", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--disable-sync",
            f"--user-data-dir={profile}", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_port(args.port):
            raise SystemExit("Chromium startte niet (geen DevTools-poort).")
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{args.port}/json/new?file://{work}/ui/demo.html", method="PUT"
            ),
            timeout=5,
        ) as answer:
            target = json.load(answer)
        browser = CDP(target["webSocketDebuggerUrl"])
        browser.call("Page.enable")
        DOCS.mkdir(exist_ok=True)
        for _ in range(40):
            browser.wacht(0.25)
            if browser.js("document.querySelectorAll('.item').length") == 4:
                break
        browser.wacht(0.6)
        print("items:", browser.js("document.querySelectorAll('.item').length"))
        print("lege afbeeldingen:", browser.js(
            "Array.from(document.images).filter(i => !i.naturalWidth).length"
        ))
        print("status:", browser.js("document.getElementById('status-pill').textContent"))
        browser.schermafbeelding(str(DOCS / "screenshot-ophalen.png"))
        browser.js("document.querySelector('.column.left').scrollTop = 620")
        browser.wacht(0.6)
        browser.schermafbeelding(str(DOCS / "screenshot-instellingen.png"))
        # De bronnen (GitHub + mailbox) staan verder naar beneden in de linkerkolom.
        browser.js(
            "(() => {"
            "  const kaart = Array.from(document.querySelectorAll('.card'))"
            "    .find((c) => (c.querySelector('h2') || {}).textContent?.startsWith('GitHub'));"
            "  const kolom = document.querySelector('.column.left');"
            "  kolom.scrollTop = kaart ? kaart.offsetTop - 20 : 0;"
            "  return kolom.scrollTop;"
            "})()"
        )
        browser.wacht(0.6)
        browser.schermafbeelding(str(DOCS / "screenshot-bronnen.png"))
        print(
            "geschreven:",
            DOCS / "screenshot-ophalen.png",
            DOCS / "screenshot-instellingen.png",
            DOCS / "screenshot-bronnen.png",
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
