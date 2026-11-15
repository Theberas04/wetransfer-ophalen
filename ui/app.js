/* WeTransfer Ophalen — de pagina werkt in het venster (window.pywebview.api)
   én in de browser (via /api/call op de ingebouwde webversie). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let state = null;
  let pollTimer = null;
  let settingsTouched = false;
  let inFlight = false;

  const bridge = () => (window.pywebview && window.pywebview.api) || null;
  const isWeb = () => location.protocol === "http:" || location.protocol === "https:";

  /* Welke namen de argumenten hebben als de webversie ze doorgeeft. */
  const ARG_NAMES = {
    add_links: ["text", "password"],
    test_slack: ["webhook"],
    save_settings: ["values"],
    retry_item: ["item_id"],
    remove_item: ["item_id"],
    open_folder: ["path"],
    choose_folder: ["kind"],
    import_file: ["path"],
  };

  async function call(method, ...args) {
    const python = bridge();
    if (python) {
      try {
        return await python[method](...args);
      } catch (error) {
        return { ok: false, error: String(error) };
      }
    }
    if (!isWeb()) return null;
    const names = ARG_NAMES[method] || [];
    const argumenten = {};
    args.forEach((value, index) => {
      argumenten[names[index] || `arg${index}`] = value;
    });
    try {
      const response = await fetch("/api/call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method, arguments: argumenten }),
      });
      if (response.status === 401) {
        location.href = "/login";
        return null;
      }
      return await response.json();
    } catch (error) {
      return { ok: false, error: "Geen verbinding met de app." };
    }
  }

  /* ---------- opmaak ---------- */
  function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return "";
    const units = ["B", "kB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
  }

  function formatSpeed(bytesPerSecond) {
    if (!bytesPerSecond) return "";
    return `${formatBytes(bytesPerSecond)}/s`;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds || 0));
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes} min ${total % 60} s`;
    const hours = Math.floor(minutes / 60);
    return `${hours} u ${minutes % 60} min`;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
  }

  /* ---------- de wachtrij ---------- */
  function itemDetails(item) {
    if (item.state === "klaar") {
      const parts = [];
      if (item.size) parts.push(formatBytes(item.size));
      if (item.seconds) parts.push(`in ${formatDuration(item.seconds)}`);
      if (item.skipped) parts.push("was al aanwezig");
      return parts.join(" · ") || "klaar";
    }
    if (item.state === "mislukt") return item.error || "mislukt";
    if (item.state === "ophalen") {
      const parts = [];
      if (item.size) parts.push(`${formatBytes(item.received)} van ${formatBytes(item.size)}`);
      else parts.push(formatBytes(item.received));
      if (item.speed) parts.push(formatSpeed(item.speed));
      if (item.size && item.speed) {
        const remaining = (item.size - item.received) / item.speed;
        if (isFinite(remaining) && remaining > 0) parts.push(`nog ${formatDuration(remaining)}`);
      }
      return parts.join(" · ");
    }
    if (item.state === "gestopt") return "gestopt — gaat verder bij de volgende start";
    return "wacht op zijn beurt";
  }

  function itemHtml(item) {
    const percent = item.percent == null ? 0 : Math.min(100, item.percent);
    const indeterminate = item.state === "ophalen" && !item.size;
    const actions = [];
    if (item.state === "mislukt" || item.state === "gestopt") {
      actions.push(`<button class="btn small" data-action="retry" data-id="${item.id}">Opnieuw</button>`);
    }
    if (item.state !== "ophalen") {
      actions.push(`<button class="btn small ghost" data-action="remove" data-id="${item.id}">Verwijderen</button>`);
    }
    const path = item.path ? `<div class="item-url" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</div>` : "";
    const hint = item.state === "mislukt" && item.hint ? `<div class="item-error">${escapeHtml(item.hint)}</div>` : "";
    const bron = item.source && item.source !== "handmatig" ? `<span class="chip">${escapeHtml(item.source)}</span>` : "";
    return `
      <div class="item ${item.state}">
        <div class="item-head">
          <span class="item-name" title="${escapeHtml(item.name || item.url)}">${escapeHtml(item.name || "onbekende transfer")}</span>
          ${bron}
          <span class="chip ${item.state}">${escapeHtml(item.state)}</span>
        </div>
        <div class="bar ${indeterminate ? "indeterminate" : ""}"><span style="width:${percent}%"></span></div>
        <div class="item-foot">
          <span class="item-url" title="${escapeHtml(item.url)}">${escapeHtml(item.url)}</span>
          <span>${escapeHtml(itemDetails(item))}</span>
          <span class="item-actions">${actions.join("")}</span>
        </div>
        ${hint}${path}
      </div>`;
  }

  function renderQueue() {
    const container = $("queue");
    const items = (state.items || []).slice().sort(byImportance);
    if (!items.length) {
      container.innerHTML = `<div class="empty">Nog geen transfers. Plak een WeTransfer-link links, zet een bestand met een link in de wachtmap, of laat de GitHub-bron of mailbox er een vinden.</div>`;
    } else {
      container.innerHTML = items.map(itemHtml).join("");
    }
    const done = items.filter((item) => item.state === "klaar").length;
    const failed = items.filter((item) => item.state === "mislukt").length;
    $("counts").textContent = items.length
      ? `${items.length} in de lijst · ${done} klaar${failed ? ` · ${failed} mislukt` : ""}`
      : "";
  }

  /* Wat aandacht nodig heeft bovenaan: bezig, dan wachtend, dan fouten, dan de rest. */
  const ORDER = { ophalen: 0, wachtend: 1, mislukt: 2, gestopt: 2, klaar: 3 };
  function byImportance(a, b) {
    const difference = (ORDER[a.state] ?? 4) - (ORDER[b.state] ?? 4);
    if (difference !== 0) return difference;
    return (b.added_at || 0) - (a.added_at || 0);
  }

  function renderStatus() {
    const pill = $("status-pill");
    const running = state.items.find((item) => item.state === "ophalen");
    pill.className = "pill";
    if (state.running && running) {
      pill.classList.add("running");
      pill.textContent = `Bezig met ${running.name || "een transfer"}`;
    } else if (state.pending) {
      pill.textContent = `${state.pending} in de wachtrij`;
    } else if (state.items.some((item) => item.state === "mislukt")) {
      pill.classList.add("failed");
      pill.textContent = "Klaar — met fouten";
    } else if (state.items.some((item) => item.state === "klaar")) {
      pill.classList.add("done");
      pill.textContent = "Alles opgehaald";
    } else {
      pill.textContent = "Klaar voor gebruik";
    }
    $("btn-start").disabled = !state.pending || state.running;
    $("btn-stop").disabled = !state.running;
  }

  function setValue(id, value) {
    const element = $(id);
    if (!element || document.activeElement === element) return;
    if (element.type === "checkbox") element.checked = !!value;
    else element.value = value == null ? "" : value;
  }

  function renderSettings() {
    const settings = state.settings || {};
    if (!settingsTouched) {
      setValue("watch_enabled", settings.watch_enabled);
      setValue("watch_interval", settings.watch_interval);
      setValue("auto_start", settings.auto_start);
      setValue("autostart_with_windows", settings.autostart_with_windows);
      setValue("sound_on_finish", settings.sound_on_finish);
      setValue("open_folder_after", settings.open_folder_after);
      setValue("notify_mode", settings.notify_mode || "altijd");
      setValue("slack_webhook", settings.slack_webhook || "");
      setValue("web_enabled", settings.web_enabled);
      setValue("web_port", settings.web_port);
      setValue("web_code", settings.web_code);
      setValue("github_enabled", settings.github_enabled);
      setValue("github_repos", settings.github_repos);
      setValue("github_interval", settings.github_interval);
      setValue("github_watch_releases", settings.github_watch_releases);
      setValue("github_watch_links", settings.github_watch_links);
      setValue("github_download_releases", settings.github_download_releases);
      setValue("github_token", settings.github_token || "");
      setValue("mailbox_enabled", settings.mailbox_enabled);
      setValue("mailbox_host", settings.mailbox_host || "");
      setValue("mailbox_port", settings.mailbox_port);
      setValue("mailbox_user", settings.mailbox_user || "");
      setValue("mailbox_password", settings.mailbox_password || "");
      setValue("mailbox_folder", settings.mailbox_folder || "INBOX");
      setValue("mailbox_sender", settings.mailbox_sender || "wetransfer");
      setValue("mailbox_days", settings.mailbox_days);
      setValue("mailbox_interval", settings.mailbox_interval);
    }
    $("download-path").textContent = settings.download_dir || "";
    $("watch-path").textContent = state.watch_dir_effective || "";
  }

  function renderWeb() {
    const web = state.web || {};
    $("web-address").textContent = web.address || "—";
    $("web-code").textContent = web.code || "—";
    $("web-state").textContent = web.enabled ? "actief" : "uit";
    $("web-state").className = web.enabled ? "chip klaar" : "chip";
  }

  function renderSources() {
    const github = state.github || {};
    const states = github.states || {};
    const lines = (github.repos || []).map((repo) => {
      const info = states[repo] || {};
      if (info.last_error) return `${repo}: ${info.last_error}`;
      if (!info.last_check_iso) return `${repo}: nog niet gecontroleerd`;
      return `${repo}: laatst ${info.last_check_iso} · ${info.last_new || 0} nieuw`;
    });
    $("github-state").textContent = lines.join(" · ") || "Nog geen repo ingesteld.";
    $("github-state").className = (github.repos || []).some((repo) => (states[repo] || {}).last_error)
      ? "muted small error"
      : "muted small";

    const mailbox = state.mailbox || {};
    const laatste = mailbox.last_result || {};
    const stukken = [];
    if (!mailbox.configured) stukken.push(mailbox.problem || "nog niet ingesteld");
    else stukken.push(`${mailbox.user} · ${mailbox.seen || 0} bericht(en) bekeken`);
    if (laatste.checked_at) stukken.push(`laatst gelezen: ${(laatste.messages || 0)} bericht(en)`);
    $("mailbox-state").textContent = stukken.join(" · ");
    $("mailbox-state").className = mailbox.configured ? "muted small" : "muted small error";
  }

  function renderLog() {
    const log = $("log");
    const lines = state.log || [];
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    log.textContent = lines.join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function render() {
    if (!state) return;
    $("version").textContent = state.version;
    $("config-dir").textContent = state.config_dir || "";
    renderSettings();
    renderQueue();
    renderStatus();
    renderWeb();
    renderSources();
    renderLog();
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const fresh = await call("get_state");
      if (fresh && fresh.settings) {
        state = fresh;
        render();
      }
    } finally {
      inFlight = false;
    }
  }

  function feedback(elementId, result, fallback) {
    const element = $(elementId);
    if (!result) {
      element.textContent = "Geen verbinding met de app.";
      element.className = "hint error";
      return;
    }
    if (result.ok) {
      element.textContent = result.message || fallback || "Klaar.";
      element.className = "hint ok";
    } else if (result.cancelled) {
      element.textContent = "";
      element.className = "hint";
    } else {
      element.textContent = result.error || fallback || "Er ging iets mis.";
      element.className = "hint error";
    }
    refresh();
  }

  function currentSettings() {
    return {
      slack_webhook: $("slack_webhook").value.trim(),
      notify_mode: $("notify_mode").value,
      watch_enabled: $("watch_enabled").checked,
      watch_interval: parseInt($("watch_interval").value, 10) || 20,
      auto_start: $("auto_start").checked,
      autostart_with_windows: $("autostart_with_windows").checked,
      sound_on_finish: $("sound_on_finish").checked,
      open_folder_after: $("open_folder_after").checked,
      web_enabled: $("web_enabled").checked,
      web_port: parseInt($("web_port").value, 10) || 8756,
      web_code: $("web_code").value.trim(),
      github_enabled: $("github_enabled").checked,
      github_repos: $("github_repos").value.trim(),
      github_interval: parseInt($("github_interval").value, 10) || 15,
      github_watch_releases: $("github_watch_releases").checked,
      github_watch_links: $("github_watch_links").checked,
      github_download_releases: $("github_download_releases").checked,
      github_token: $("github_token").value.trim(),
      mailbox_enabled: $("mailbox_enabled").checked,
      mailbox_host: $("mailbox_host").value.trim(),
      mailbox_port: parseInt($("mailbox_port").value, 10) || 993,
      mailbox_user: $("mailbox_user").value.trim(),
      mailbox_password: $("mailbox_password").value,
      mailbox_folder: $("mailbox_folder").value.trim() || "INBOX",
      mailbox_sender: $("mailbox_sender").value.trim() || "wetransfer",
      mailbox_days: parseInt($("mailbox_days").value, 10) || 30,
      mailbox_interval: parseInt($("mailbox_interval").value, 10) || 10,
    };
  }

  const TOUCH_IDS = [
    "slack_webhook", "notify_mode", "watch_enabled", "watch_interval", "auto_start",
    "autostart_with_windows", "sound_on_finish", "open_folder_after",
    "web_enabled", "web_port", "web_code",
    "github_enabled", "github_repos", "github_interval", "github_watch_releases",
    "github_watch_links", "github_download_releases", "github_token",
    "mailbox_enabled", "mailbox_host", "mailbox_port", "mailbox_user", "mailbox_password",
    "mailbox_folder", "mailbox_sender", "mailbox_days", "mailbox_interval",
  ];

  function wire() {
    $("btn-add").addEventListener("click", async () => {
      const text = $("links").value.trim();
      if (!text) {
        feedback("add-feedback", { ok: false, error: "Plak eerst een WeTransfer-link." });
        return;
      }
      const result = await call("add_links", text, $("password").value);
      if (result && result.ok && result.added) $("links").value = "";
      feedback("add-feedback", result, "Toegevoegd.");
    });

    $("btn-import").addEventListener("click", async () => {
      feedback("add-feedback", await call("choose_link_file"), "Links uit bestand toegevoegd.");
    });

    $("btn-scan").addEventListener("click", async () => {
      const result = await call("scan_watch_folder");
      if (result && result.ok) {
        const count = (result.links || []).length;
        feedback("add-feedback", {
          ok: true,
          message: count ? `${count} link(s) gevonden in de wachtmap.` : "Geen nieuwe links gevonden in de wachtmap.",
        });
      } else {
        feedback("add-feedback", result, "Scannen mislukte.");
      }
    });

    $("btn-start").addEventListener("click", async () => {
      feedback("add-feedback", await call("start_queue"), "Ophalen gestart.");
    });
    $("btn-stop").addEventListener("click", async () => {
      feedback("add-feedback", await call("stop_queue"), "Gestopt.");
    });
    $("btn-clear").addEventListener("click", async () => {
      await call("clear_finished");
      refresh();
    });
    $("btn-clear-log").addEventListener("click", async () => {
      await call("clear_log");
      refresh();
    });

    $("queue").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action === "retry" ? "retry_item" : "remove_item";
      await call(action, button.dataset.id);
      refresh();
    });

    $("btn-choose-download").addEventListener("click", async () => {
      const result = await call("choose_folder", "download");
      if (result && result.ok) {
        $("download-path").textContent = result.path;
      }
      refresh();
    });
    $("btn-choose-watch").addEventListener("click", async () => {
      const result = await call("choose_folder", "watch");
      if (result && result.ok) {
        $("watch-path").textContent = result.path;
        $("watch_enabled").checked = true;
      }
      refresh();
    });
    $("btn-open-download").addEventListener("click", () => call("open_folder", ""));
    $("btn-open-watch").addEventListener("click", () => call("open_folder", state ? state.watch_dir_effective : ""));
    $("btn-open-log").addEventListener("click", () => call("open_log_file"));

    $("btn-test-slack").addEventListener("click", async () => {
      feedback("slack-feedback", await call("test_slack", $("slack_webhook").value.trim()), "Testbericht verstuurd.");
    });

    $("btn-check-github").addEventListener("click", async () => {
      feedback("github-feedback", await call("check_github"), "GitHub gecontroleerd.");
    });
    $("btn-check-mailbox").addEventListener("click", async () => {
      feedback("mailbox-feedback", await call("check_mailbox"), "Mailbox gelezen.");
    });
    $("btn-check-all").addEventListener("click", async () => {
      const result = await call("check_all_sources");
      if (result && result.ok) {
        const stukken = Object.entries(result.results || {}).map(([naam, waarde]) => {
          const tekst = waarde && (waarde.message || waarde.error || "klaar");
          return `${naam}: ${tekst}`;
        });
        feedback("mailbox-feedback", { ok: true, message: stukken.join(" · ") });
      } else {
        feedback("mailbox-feedback", result, "Controleren mislukte.");
      }
    });

    $("btn-save").addEventListener("click", async () => {
      const result = await call("save_settings", currentSettings());
      feedback("settings-feedback", result, "Instellingen opgeslagen.");
      settingsTouched = false;
      refresh();
    });

    TOUCH_IDS.forEach((id) => {
      const element = $(id);
      if (!element) return;
      const mark = () => { settingsTouched = true; };
      element.addEventListener("change", mark);
      element.addEventListener("input", mark);
    });

    $("links").addEventListener("keydown", (event) => {
      if (event.ctrlKey && event.key === "Enter") $("btn-add").click();
    });

    // In de browser horen de vensterknoppen er niet: die openen een venster op de pc.
    if (!bridge() && isWeb()) {
      document.querySelectorAll(".needs-window").forEach((element) => { element.hidden = true; });
      $("web-hint").hidden = false;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      if (!document.hidden) refresh();
    }, 1000);
    refresh();
  }

  window.app = { refresh, render: refresh };

  function boot() {
    wire();
    startPolling();
  }

  if (bridge() || isWeb()) boot();
  else window.addEventListener("pywebviewready", boot);
})();
