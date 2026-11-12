// Druckdienst – Formular-Logik, Upload mit Fortschritt, Status-Aktualisierung.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const form = $("upload-form");
  if (!form) return;

  const presets = JSON.parse($("presets-data").textContent || "[]");
  const duplexFormats = JSON.parse($("duplex-formats").textContent || "[]");
  const presetSel = $("preset");
  const fmtSel = $("format");
  const duplexSel = $("duplex");
  const copies = $("kopien");
  const fileInput = $("datei");
  const msg = $("form-msg");
  const progress = $("progress");
  const submit = $("submit");
  const csrf = form.querySelector("input[name=csrf]").value;
  const DUPLEX = { none: "einseitig", long: "beidseitig (lange Kante)", short: "beidseitig (kurze Kante)" };
  let applyingPreset = false;

  function setColor(on) {
    form.querySelector(`input[name=farbe][value="${on ? 1 : 0}"]`).checked = true;
  }

  function updateDuplexAvailability() {
    const allowed = duplexFormats.includes(fmtSel.value);
    for (const opt of duplexSel.options) {
      if (opt.value !== "none") opt.disabled = !allowed;
    }
    if (!allowed) duplexSel.value = "none";
  }

  function applyPreset() {
    const p = presets.find((x) => x.id === presetSel.value);
    const hint = $("preset-hint");
    if (!p) { if (hint) hint.hidden = true; return; }
    applyingPreset = true;
    fmtSel.value = p.format;
    updateDuplexAvailability();
    setColor(p.farbe);
    duplexSel.value = p.duplex;
    copies.value = p.kopien;
    applyingPreset = false;
    if (hint) { hint.textContent = p.hinweis || ""; hint.hidden = !p.hinweis; }
  }

  // Manuelle Änderung => Preset abwählen, damit das Log/Routing nicht falsch ist.
  function manualChange() {
    if (!applyingPreset && presetSel && presetSel.value) {
      presetSel.value = "";
      const hint = $("preset-hint");
      if (hint) hint.hidden = true;
    }
  }

  if (presetSel) presetSel.addEventListener("change", applyPreset);
  fmtSel.addEventListener("change", () => { updateDuplexAvailability(); manualChange(); });
  [duplexSel, copies, ...form.querySelectorAll("input[name=farbe]")].forEach((el) =>
    el.addEventListener("change", manualChange));
  updateDuplexAvailability();

  function showMsg(text, isError) {
    msg.textContent = text;
    msg.className = "flash" + (isError ? " error" : "");
    msg.hidden = false;
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    msg.hidden = true;
    const f = fileInput.files[0];
    if (!f) return showMsg("Bitte eine PDF-Datei auswählen.", true);
    if (f.size > Number(fileInput.dataset.maxBytes)) {
      return showMsg("Datei zu groß (max. " + Math.round(fileInput.dataset.maxBytes / 1048576) + " MB).", true);
    }
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") {
      return showMsg("Nur PDF-Dateien sind erlaubt.", true);
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/upload");
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.responseType = "json";
    const bar = progress.querySelector("progress");
    const label = progress.querySelector("span");
    progress.hidden = false;
    submit.disabled = true;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        bar.value = pct;
        label.textContent = pct < 100 ? pct + " %" : "wird geprüft …";
      }
    };
    xhr.onloadend = () => {
      submit.disabled = false;
      progress.hidden = true;
      const res = xhr.response || {};
      if (xhr.status === 200 && res.ok) {
        showMsg(res.message, false);
        fileInput.value = "";
        $("seiten").value = "";
        refresh();
      } else if (xhr.status === 401) {
        window.location = "/login";
      } else {
        showMsg(res.error || "Upload fehlgeschlagen (Netzwerkfehler?).", true);
      }
    };
    xhr.send(new FormData(form));
  });

  // ---------------------------------------------------------------- Status
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderJobs(jobs) {
    const list = $("jobs");
    list.replaceChildren();
    if (!jobs.length) { list.append(el("li", "empty muted", "Noch keine Aufträge.")); return; }
    for (const j of jobs) {
      const li = el("li", "job s-" + j.status);
      const main = el("div", "job-main");
      main.append(el("strong", "fn", j.datei));
      main.append(el("span", "badge s-" + j.status, j.status_text + (j.position ? " · Platz " + j.position : "")));
      li.append(main);
      li.append(el("div", "job-meta",
        [j.erstellt_lokal, j.format, j.farbe ? "Farbe" : "s/w", DUPLEX[j.duplex], j.kopien + "×", j.druckseiten + " S."].join(" · ")));
      if (j.fehler && j.status === "error") li.append(el("div", "job-err", j.fehler));
      if (j.status === "queued") {
        const f = el("form", "inline");
        f.method = "post";
        f.action = "/jobs/" + encodeURIComponent(j.id) + "/cancel";
        const h = el("input"); h.type = "hidden"; h.name = "csrf"; h.value = csrf;
        const b = el("button", "link danger", "Abbrechen"); b.type = "submit";
        f.append(h, b);
        li.append(f);
      }
      list.append(li);
    }
  }

  function renderAgent(online) {
    const banner = $("agent-banner");
    banner.className = "agent " + (online ? "online" : "offline");
    $("agent-text").textContent = online
      ? "Drucker bereit"
      : "Druck-PC offline – Aufträge werden gedruckt, sobald er wieder erreichbar ist.";
  }

  let timer = null;
  async function refresh() {
    clearTimeout(timer);
    try {
      const r = await fetch("/api/my-jobs", { credentials: "same-origin" });
      if (r.status === 401) { window.location = "/login"; return; }
      const data = await r.json();
      renderJobs(data.jobs);
      renderAgent(data.agent_online);
      const active = data.jobs.some((j) => j.status === "queued" || j.status === "printing");
      timer = setTimeout(refresh, active ? 4000 : 20000);
    } catch (e) {
      timer = setTimeout(refresh, 15000);
    }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  timer = setTimeout(refresh, 4000);
})();
