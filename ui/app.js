/* WeTransfer Ophalen — de pagina praat met de app via window.pywebview.api. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let state = null;
  let pollTimer = null;
  let settingsTouched = false;
  let inFlight = false;

  const api = () => (window.pywebview && window.pywebview.api) || null;

  async function call(method, ...args) {
    const bridge = api();
    if (!bridge) return null;
    try {
      return await bridge[method](...args);
    } catch (error) {
      return { ok: false, error: String(error) };
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
    return `
      <div class="item ${item.state}">
        <div class="item-head">
          <span class="item-name" title="${escapeHtml(item.name || item.url)}">${escapeHtml(item.name || "onbekende transfer")}</span>
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
      container.innerHTML = `<div class="empty">Nog geen transfers. Plak een WeTransfer-link links of zet een bestand met een link in de wachtmap.</div>`;
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

  function renderSettings() {
    const settings = state.settings || {};
    if (!settingsTouched) {
      $("watch_enabled").checked = !!settings.watch_enabled;
      $("watch_interval").value = settings.watch_interval;
      $("auto_start").checked = !!settings.auto_start;
      $("autostart_with_windows").checked = !!settings.autostart_with_windows;
      $("sound_on_finish").checked = !!settings.sound_on_finish;
      $("open_folder_after").checked = !!settings.open_folder_after;
      $("notify_mode").value = settings.notify_mode || "altijd";
      if (document.activeElement !== $("slack_webhook")) {
        $("slack_webhook").value = settings.slack_webhook || "";
      }
    }
    $("download-path").textContent = settings.download_dir || "";
    $("watch-path").textContent = state.watch_dir_effective || "";
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
    renderLog();
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const fresh = await call("get_state");
      if (fresh) {
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
    };
  }

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

    $("btn-save").addEventListener("click", async () => {
      const result = await call("save_settings", currentSettings());
      feedback("settings-feedback", result, "Instellingen opgeslagen.");
      settingsTouched = false;
      refresh();
    });

    ["slack_webhook", "notify_mode", "watch_enabled", "watch_interval", "auto_start",
      "autostart_with_windows", "sound_on_finish", "open_folder_after"].forEach((id) => {
      const element = $(id);
      const mark = () => { settingsTouched = true; };
      element.addEventListener("change", mark);
      element.addEventListener("input", mark);
    });

    $("links").addEventListener("keydown", (event) => {
      if (event.ctrlKey && event.key === "Enter") $("btn-add").click();
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      if (!document.hidden) refresh();
    }, 1000);
    refresh();
  }

  window.app = { refresh, render: refresh };

  if (api()) {
    wire();
    startPolling();
  } else {
    window.addEventListener("pywebviewready", () => {
      wire();
      startPolling();
    });
  }
})();
