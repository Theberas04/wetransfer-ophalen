"""Meldingen sturen naar Slack via een Incoming Webhook."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

import requests

from .wetransfer import human_size

TIMEOUT = 15.0


def validate_webhook(url: str, skip_host_check: bool = False) -> Optional[str]:
    """Geef een Nederlandse foutmelding terug, of None als de URL bruikbaar lijkt."""

    value = (url or "").strip()
    if not value:
        return "Er is nog geen Slack-webhook ingevuld."
    parsed = urlparse(value)
    if parsed.scheme != "https" and not skip_host_check:
        return "De webhook moet met https:// beginnen."
    if not parsed.netloc:
        return "De webhook is geen geldige URL."
    if not skip_host_check and "slack" not in parsed.netloc:
        return "Dit lijkt geen Slack-webhook (verwacht een adres op hooks.slack.com)."
    return None


def send_slack(
    webhook_url: str,
    text: str,
    blocks: Optional[list[dict[str, Any]]] = None,
    skip_host_check: bool = False,
) -> tuple[bool, str]:
    """Stuur één bericht. Geeft (gelukt, foutmelding-in-het-Nederlands)."""

    problem = validate_webhook(webhook_url, skip_host_check=skip_host_check)
    if problem:
        return False, problem
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        response = requests.post(webhook_url.strip(), json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"Slack was niet bereikbaar: {exc.__class__.__name__}."
    if response.status_code >= 400:
        detail = response.text.strip()[:200] or f"status {response.status_code}"
        if response.status_code == 404:
            return False, "Slack kent deze webhook niet (404). Maak een nieuwe webhook aan."
        return False, f"Slack weigerde het bericht ({response.status_code}): {detail}"
    if response.text.strip() not in ("", "ok"):
        return False, f"Slack antwoordde onverwacht: {response.text.strip()[:120]}"
    return True, ""


def done_text(item: Any, directory: str) -> str:
    size = human_size(getattr(item, "size", 0) or getattr(item, "received", 0))
    seconds = float(getattr(item, "seconds", 0) or 0)
    name = getattr(item, "name", "") or "transfer"
    kop = (
        ":inbox_tray: *Bestand opgehaald*"
        if getattr(item, "direct", False)
        else ":inbox_tray: *WeTransfer opgehaald*"
    )
    lines = [
        f"{kop} — `{name}`",
        f"Grootte: {size} · duur: {_duration(seconds)}",
    ]
    source = getattr(item, "source", "")
    if source and source not in ("handmatig",):
        lines.append(f"Bron: {source}")
    path = getattr(item, "path", "")
    if path:
        lines.append(f"Opgeslagen als: `{path}`")
    elif directory:
        lines.append(f"Opgeslagen in: `{directory}`")
    return "\n".join(lines)


def failed_text(item: Any) -> str:
    name = getattr(item, "name", "") or "transfer"
    url = getattr(item, "url", "")
    error = getattr(item, "error", "") or "onbekende fout"
    hint = getattr(item, "hint", "") or ""
    lines = [
        f":warning: *WeTransfer ophalen mislukt* — `{name}`",
        f"Reden: {error}",
    ]
    if hint:
        lines.append(hint)
    if url:
        lines.append(f"Link: {url}")
    return "\n".join(lines)


def summary_text(items: list[Any]) -> str:
    done = [item for item in items if getattr(item, "state", "") == "klaar"]
    failed = [item for item in items if getattr(item, "state", "") == "mislukt"]
    total = sum(int(getattr(item, "size", 0) or getattr(item, "received", 0) or 0) for item in done)
    seconds = sum(float(getattr(item, "seconds", 0) or 0) for item in done)
    lines = [
        f":white_check_mark: *{len(done)} van {len(items)} transfers opgehaald*"
        if not failed
        else f":warning: *{len(done)} van {len(items)} transfers opgehaald, {len(failed)} mislukt*"
    ]
    if done:
        lines.append(f"Samen {human_size(total)} in {_duration(seconds)}")
    for item in done[:10]:
        lines.append(f"• `{getattr(item, 'name', '')}`")
    for item in failed[:10]:
        lines.append(f"• :x: `{getattr(item, 'name', '')}` — {getattr(item, 'error', '')}")
    return "\n".join(lines)


def test_text() -> str:
    return (
        ":wave: *Testbericht van WeTransfer Ophalen*\n"
        "Als je dit leest, komen de meldingen over opgehaalde transfers goed aan."
    )


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} u {minutes} min"
