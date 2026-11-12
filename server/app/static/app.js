// Druckdienst – Druckmaske mit Live-Vorschau (pdf.js lokal), Upload, Auftragsstatus.
import * as pdfjs from "/static/vendor/pdf.min.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);
const form = $("upload-form");
const csrf = form.querySelector("input[name=csrf]").value;
const fileInput = $("datei");
const presets = JSON.parse($("presets-data").textContent || "[]");
const duplexFormats = JSON.parse($("duplex-formats").textContent || "[]");
const PAPER_MM = { A3: [297, 420], A4: [210, 297], A5: [148, 210], Letter: [216, 279] };
const MAX_BYTES = Number(fileInput.dataset.maxBytes);
const MAX_PAGES = Number(fileInput.dataset.maxPages);
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  file: null, doc: null, pages: 0, landscape: false,
  sequence: [],          // Seiten in Druckreihenfolge
  rangeError: null,
  sheet: 0, back: false,
  renderGen: 0,
};

// ------------------------------------------------------------------ Formularwerte
const val = (name) => form.querySelector(`input[name="${name}"]:checked`)?.value;
// Beidseitig = immer wie ein Buch umblättern. Bei Hochformat ist das die lange Papierkante,
// bei Querformat die kurze – so stehen die Rückseiten immer richtig herum.
function computeDuplex() {
  if (val("seiten_modus") !== "beidseitig") return "none";
  return isLandscape() ? "short" : "long";
}
// Ausrichtung des Blatts: automatisch aus dem PDF oder fest gewählt.
function isLandscape() {
  const o = val("ausrichtung");
  return o === "auto" ? state.landscape : o === "landscape";
}
const settings = () => ({
  format: val("format"),
  color: val("farbe") === "1",
  duplex: computeDuplex(),
  scaling: val("skalierung") === "actual" ? "actual" : "fit",

  copies: Math.max(1, Math.min(Number($("kopien").value) || 1, Number($("kopien").max))),
  preset: val("preset") || "",
});
function setRadio(name, value) {
  const el = form.querySelector(`input[name="${name}"][value="${value}"]`);
  if (el && !el.disabled) el.checked = true;
}

// ------------------------------------------------------------------ Papiergrößen
const PT_TO_MM = 25.4 / 72;
const KNOWN_SIZES = [["A5", 148, 210], ["A4", 210, 297], ["A3", 297, 420], ["A2", 420, 594],
  ["Letter", 216, 279], ["Legal", 216, 356]];
function sizeName(wMm, hMm) {
  const [a, b] = [Math.min(wMm, hMm), Math.max(wMm, hMm)];
  const hit = KNOWN_SIZES.find(([, x, y]) => Math.abs(a - x) <= 3 && Math.abs(b - y) <= 3);
  return hit ? `${hit[0]} ${wMm > hMm ? "quer" : "hoch"}` : null;
}
const mm = (v) => Math.round(v);

// ------------------------------------------------------------------ Seitenbereiche
function parseRange(str, n) {
  const s = str.replace(/\s+/g, "").replace(/;/g, ",");
  if (!s) return { pages: Array.from({ length: n }, (_, i) => i + 1) };
  const pages = [];
  for (const part of s.split(",")) {
    if (!part) continue;
    const m = part.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) return { error: `„${part}“ verstehe ich nicht. Beispiel: 1-3, 5` };
    const a = Number(m[1]); const b = m[2] ? Number(m[2]) : a;
    if (a < 1 || b < a) return { error: `„${part}“ ist kein gültiger Bereich.` };
    if (n && b > n) return { error: `Seite ${b} gibt es nicht, das PDF hat ${n} Seiten.` };
    for (let p = a; p <= b; p++) pages.push(p);
  }
  if (!pages.length) return { error: "Mindestens eine Seite auswählen." };
  return { pages };
}
function compress(sorted) {
  const out = []; let i = 0;
  while (i < sorted.length) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    out.push(i === j ? `${sorted[i]}` : `${sorted[i]}-${sorted[j]}`);
    i = j + 1;
  }
  return out.join(", ");
}

// ------------------------------------------------------------------ Datei laden
async function loadFile(file) {
  if (!file) return;
  if (file.size > MAX_BYTES) return toast(`Die Datei ist ${fmtSize(file.size)} groß, erlaubt sind ${fmtSize(MAX_BYTES)}.`, "error");
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") return toast("Nur PDF-Dateien lassen sich drucken.", "error");

  state.file = file;
  $("file-name").textContent = file.name;
  $("file-meta").textContent = `${fmtSize(file.size)}, wird gelesen …`;
  $("dropzone").hidden = true;
  $("preview").hidden = false;
  $("seiten").value = "";

  try {
    const data = new Uint8Array(await file.arrayBuffer());
    if (state.doc) state.doc.destroy();
    state.doc = await pdfjs.getDocument({ data, isEvalSupported: false, enableXfa: false }).promise;
    state.pages = state.doc.numPages;
    const first = await state.doc.getPage(1);
    const vp = first.getViewport({ scale: 1 });
    state.landscape = vp.width > vp.height * 1.02;
    const wMm = vp.width * PT_TO_MM, hMm = vp.height * PT_TO_MM;
    const name = sizeName(wMm, hMm);
    $("file-meta").textContent = `${state.pages} Seite${state.pages === 1 ? "" : "n"}, ${name ? name + ", " : ""}${mm(wMm)} × ${mm(hMm)} mm, ${fmtSize(file.size)}`;
    if (state.pages > MAX_PAGES) toast(`Das PDF hat ${state.pages} Seiten, erlaubt sind höchstens ${MAX_PAGES}.`, "error");
  } catch (e) {
    state.doc = null; state.pages = 0;
    const pw = e && e.name === "PasswordException";
    $("file-meta").textContent = pw ? "Passwortgeschützt, bitte ohne Passwort exportieren." : "Keine lesbare PDF-Datei.";
    toast(pw ? "Das PDF ist passwortgeschützt. Exportiere es ohne Passwort und lade es erneut hoch." : "Diese Datei lässt sich nicht als PDF lesen.", "error");
  }
  buildThumbs();
  state.sheet = 0; state.back = false;
  update();
}

function clearFile() {
  state.file = null; state.pages = 0;
  if (state.doc) { state.doc.destroy(); state.doc = null; }
  fileInput.value = "";
  $("seiten").value = "";
  setRadio("ausrichtung", "auto");
  $("scale-info").hidden = true;
  $("preview").hidden = true;
  $("dropzone").hidden = false;
  $("thumbs").replaceChildren();
  update();
}

// ------------------------------------------------------------------ Miniaturen
let thumbObserver = null;
let thumbQueue = Promise.resolve();
function buildThumbs() {
  const wrap = $("thumbs");
  wrap.replaceChildren();
  if (thumbObserver) thumbObserver.disconnect();
  if (!state.doc) return;
  const doc = state.doc;
  thumbObserver = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      thumbObserver.unobserve(en.target);
      const btn = en.target;
      thumbQueue = thumbQueue.then(() => renderThumb(doc, btn)).catch(() => {});
    }
  }, { root: wrap, rootMargin: "200px" });
  const count = Math.min(state.pages, 500);
  for (let p = 1; p <= count; p++) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "thumb"; b.dataset.page = p;
    b.setAttribute("aria-label", `Seite ${p}`);
    b.append(document.createElement("canvas"));
    const n = document.createElement("span"); n.className = "thumb-num"; n.textContent = p;
    b.append(n);
    b.addEventListener("click", () => togglePage(p));
    wrap.append(b);
    thumbObserver.observe(b);
  }
}
async function renderThumb(doc, btn) {
  if (doc !== state.doc) return;
  const page = await doc.getPage(Number(btn.dataset.page));
  const vp1 = page.getViewport({ scale: 1 });
  const h = 92, dpr = Math.min(window.devicePixelRatio || 1, 2);
  const vp = page.getViewport({ scale: (h / vp1.height) * dpr });
  const cv = btn.querySelector("canvas");
  cv.width = vp.width; cv.height = vp.height;
  cv.style.width = `${vp.width / dpr}px`; cv.style.height = `${h}px`;
  await page.render({ canvasContext: cv.getContext("2d"), viewport: vp }).promise;
  btn.classList.add("ready");
}
function togglePage(p) {
  const r = parseRange($("seiten").value, state.pages);
  const set = new Set(r.pages || []);
  if (set.has(p)) set.delete(p); else set.add(p);
  if (!set.size) return toast("Mindestens eine Seite muss ausgewählt bleiben.", "error");
  const sorted = [...set].sort((a, b) => a - b);
  $("seiten").value = sorted.length === state.pages ? "" : compress(sorted);
  state.sheet = 0; state.back = false;
  update();
}

// ------------------------------------------------------------------ Blätter & Vorschau
function sheets() {
  const seq = state.sequence;
  if (settings().duplex === "none") return seq.map((p) => [p, null]);
  const out = [];
  for (let i = 0; i < seq.length; i += 2) out.push([seq[i], seq[i + 1] ?? null]);
  return out;
}

function layoutSheet() {
  const s = settings();
  const vp = $("viewport");
  const [wMm, hMm] = PAPER_MM[s.format] || PAPER_MM.A4;
  const land = isLandscape();
  const sw = land ? hMm : wMm, sh = land ? wMm : hMm;
  // Gemeinsamer Maßstab: A3 füllt die Fläche, kleinere Formate wirken entsprechend kleiner.
  const availW = vp.clientWidth - 48, availH = vp.clientHeight - 56;
  const pxPerMm = Math.max(0.3, Math.min(availW / (land ? 420 : 297), availH / (land ? 297 : 420)));
  const sheet = $("sheet-stack");
  sheet.style.width = `${Math.round(sw * pxPerMm)}px`;
  sheet.style.height = `${Math.round(sh * pxPerMm)}px`;
  $("paper-label").textContent = `Blatt ${s.format} ${land ? "quer" : "hoch"}, ${sw} × ${sh} mm`;
  return { w: sw * pxPerMm, h: sh * pxPerMm, pxPerMm, sheetMm: [sw, sh] };
}

async function renderFace(canvas, pageNum, box, gen) {
  const ctx = canvas.getContext("2d");
  const face = canvas.parentElement;
  if (!pageNum || !state.doc) {
    canvas.width = 1; canvas.height = 1; ctx.clearRect(0, 0, 1, 1);
    face.classList.add("is-empty");
    return;
  }
  face.classList.remove("is-empty");
  const page = await state.doc.getPage(pageNum);
  if (gen !== state.renderGen) return;
  const base = page.getViewport({ scale: 1 });
  const pageLand = base.width > base.height;
  const sheetLand = box.w > box.h;
  const fit = settings().scaling === "fit";
  // Beim Anpassen mit "Automatisch" werden Seiten passend zum Blatt gedreht; sonst bleibt die Seite, wie sie ist.
  const autoRotate = fit && val("ausrichtung") === "auto";
  const rotation = (page.rotate + (autoRotate && pageLand !== sheetLand ? 90 : 0)) % 360;
  const rv = page.getViewport({ scale: 1, rotation });
  const actualScale = box.pxPerMm * PT_TO_MM;            // Bildschirm-px pro PDF-Punkt bei 100 %
  const marginMm = 4;                                     // typischer unbedruckbarer Rand
  const margin = marginMm * box.pxPerMm;
  const scale = fit ? Math.min((box.w - 2 * margin) / rv.width, (box.h - 2 * margin) / rv.height) : actualScale;
  const pageMm = [rv.width * PT_TO_MM, rv.height * PT_TO_MM];
  const info = {
    pageMm, name: sizeName(pageMm[0], pageMm[1]), pct: Math.round((scale / actualScale) * 100),
    clipped: !fit && (pageMm[0] > box.sheetMm[0] - 2 * marginMm + 0.5 || pageMm[1] > box.sheetMm[1] - 2 * marginMm + 0.5),
  };
  let dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const area = rv.width * scale * rv.height * scale * dpr * dpr;
  if (area > 16e6) dpr *= Math.sqrt(16e6 / area);          // Riesenseiten: Canvas begrenzen
  const vp = page.getViewport({ scale: scale * dpr, rotation });
  const off = document.createElement("canvas");
  off.width = Math.ceil(vp.width); off.height = Math.ceil(vp.height);
  await page.render({ canvasContext: off.getContext("2d"), viewport: vp }).promise;
  if (gen !== state.renderGen) return;
  canvas.width = off.width; canvas.height = off.height;
  canvas.style.width = `${vp.width / dpr}px`; canvas.style.height = `${vp.height / dpr}px`;
  ctx.drawImage(off, 0, 0);
  return info;
}

function showScaleInfo(info, pageNum) {
  const el = $("scale-info");
  if (!info) { el.hidden = true; return; }
  const size = `Seite ${pageNum} ist ${mm(info.pageMm[0])} × ${mm(info.pageMm[1])} mm${info.name ? ` (${info.name})` : ""}.`;
  let text;
  if (settings().scaling === "fit") {
    text = info.pct < 100 ? `${size} Sie wird auf ${info.pct} % verkleinert, damit sie aufs Blatt passt.`
      : info.pct > 100 ? `${size} Sie wird auf ${info.pct} % vergrößert, um das Blatt zu füllen.`
      : `${size} Sie passt ohne Änderung aufs Blatt.`;
  } else if (info.clipped) {
    text = `${size} Sie ist größer als der bedruckbare Bereich des Blatts, die Ränder werden abgeschnitten.`;
  } else {
    text = `${size} Sie wird in Originalgröße gedruckt, 1 mm im PDF ist 1 mm auf Papier.`;
  }
  el.textContent = text;
  el.classList.toggle("is-warn", !!info.clipped);
  el.hidden = false;
  $("paper-label").textContent += `, ${info.pct} %`;
}

async function renderPreview() {
  if (!state.file) return;
  const gen = ++state.renderGen;
  const box = layoutSheet();
  const list = sheets();
  const s = settings();
  state.sheet = Math.min(state.sheet, Math.max(0, list.length - 1));
  const [front, back] = list[state.sheet] || [null, null];
  const sheetEl = $("sheet");
  const duplex = s.duplex !== "none";
  // Seitlich umblättern = Drehung um die senkrechte Achse, nach oben = um die waagerechte.
  const axisY = true;
  sheetEl.classList.toggle("axis-x", duplex && !axisY);
  sheetEl.classList.toggle("flipped", duplex && state.back);
  sheetEl.classList.toggle("duplex", duplex);

  const stack = $("sheet-stack");
  stack.dataset.copies = Math.min(s.copies, 3);
  $("copies-badge").hidden = s.copies < 2;
  $("copies-badge").textContent = `${s.copies} Kopien`;
  $("stage-mono")?.remove();
  document.querySelector(".stage").classList.toggle("mono", !s.color);

  const n = list.length;
  const shown = duplex && state.back ? back : front;
  $("nav-text").textContent = n
    ? `Blatt ${state.sheet + 1} von ${n}${duplex ? (state.back ? ", Rückseite" : ", Vorderseite") : ""}${shown ? ` (Seite ${shown})` : ", leer"}`
    : "";
  $("nav-prev").disabled = state.sheet === 0 && !(duplex && state.back);
  $("nav-next").disabled = state.sheet >= n - 1 && !(duplex && !state.back);
  $("nav-flip").hidden = !duplex;

  const shownPage = duplex && state.back && back ? back : front;
  const [fi, bi] = await Promise.all([
    renderFace($("cv-front"), front, box, gen),
    duplex ? renderFace($("cv-back"), back, box, gen) : Promise.resolve(),
  ]).catch(() => [null, null]);
  if (gen === state.renderGen) showScaleInfo(shownPage === back && bi ? bi : fi, shownPage);
}

function step(dir) {
  const duplex = settings().duplex !== "none";
  const n = sheets().length;
  if (duplex) {
    if (dir > 0 && !state.back) { state.back = true; return renderPreview(); }
    if (dir < 0 && state.back) { state.back = false; return renderPreview(); }
  }
  const next = state.sheet + dir;
  if (next < 0 || next >= n) return;
  state.sheet = next;
  state.back = duplex && dir < 0;
  renderPreview();
}

// ------------------------------------------------------------------ Zustand → Oberfläche
function updateDuplexAvailability() {
  const fmt = val("format");
  const ok = duplexFormats.includes(fmt);
  const duplexRadio = form.querySelector('input[name="seiten_modus"][value="beidseitig"]');
  duplexRadio.disabled = !ok;
  if (!ok && duplexRadio.checked) setRadio("seiten_modus", "einseitig");
  const both = val("seiten_modus") === "beidseitig";
  $("flip-explain").hidden = !both;
  const hint = $("duplex-hint");
  hint.textContent = ok ? "" : `${fmt} kann nur einseitig gedruckt werden.`;
  hint.hidden = ok;
  $("duplex").value = computeDuplex();
}

// Beim Umschalten einmal vorführen, wie das Blatt gewendet wird.
let demoTimer = null;
function demoFlip() {
  if (!state.file || reduceMotion || settings().duplex === "none") return;
  clearTimeout(demoTimer);
  state.back = false;
  renderPreview();
  demoTimer = setTimeout(() => {
    state.back = true; renderPreview();
    demoTimer = setTimeout(() => { state.back = false; renderPreview(); }, 1400);
  }, 250);
}

function update() {
  $("orient-detected").textContent = state.doc ? (state.landscape ? "Querformat erkannt" : "Hochformat erkannt") : "wie im PDF";
  updateDuplexAvailability();
  const s = settings();
  const r = parseRange($("seiten").value, state.pages);
  state.rangeError = r.error || null;
  state.sequence = r.pages || [];
  const help = $("seiten-help");
  help.textContent = r.error || (state.pages ? `${state.sequence.length} von ${state.pages} Seiten` : "z. B. 1-3, 5");
  help.classList.toggle("is-error", !!r.error);
  $("seiten").setAttribute("aria-invalid", r.error ? "true" : "false");
  $("seiten-reset").hidden = !$("seiten").value;

  // Miniaturen markieren
  const sel = new Set(state.sequence);
  $("thumbs").querySelectorAll(".thumb").forEach((t) => {
    const on = sel.has(Number(t.dataset.page));
    t.classList.toggle("off", !on);
    t.setAttribute("aria-pressed", on ? "true" : "false");
  });
  document.querySelector(".stage").classList.toggle("mono", !s.color);

  // Zusammenfassung
  const btn = $("submit");
  const main = $("sum-main"), sub = $("sum-sub");
  if (!state.file) {
    main.textContent = "Noch keine Datei gewählt";
    sub.textContent = "Die Vorschau zeigt, wie der Druck aussieht.";
    btn.disabled = true;
  } else if (!state.doc) {
    main.textContent = "Datei kann nicht gedruckt werden";
    sub.textContent = "Wähle ein anderes PDF.";
    btn.disabled = true;
  } else if (r.error) {
    main.textContent = "Seitenbereich prüfen";
    sub.textContent = r.error;
    btn.disabled = true;
  } else {
    const pages = state.sequence.length * s.copies;
    const perCopy = s.duplex === "none" ? state.sequence.length : Math.ceil(state.sequence.length / 2);
    const sheetsTotal = perCopy * s.copies;
    main.textContent = `${pages} Druckseite${pages === 1 ? "" : "n"} auf ${sheetsTotal} Blatt ${s.format}`;
    const parts = [s.color ? "Farbe" : "Schwarzweiß",
      s.duplex === "none" ? "einseitig" : "beidseitig"];
    if (s.copies > 1) parts.push(`${s.copies} Kopien`);
    sub.textContent = parts.join(", ");
    btn.disabled = state.pages > MAX_PAGES;
  }
  renderPreview();
}

// ------------------------------------------------------------------ Vorlagen
function applyPreset(id) {
  const p = presets.find((x) => x.id === id);
  const hint = $("preset-hint");
  if (!p) { if (hint) hint.hidden = true; return; }
  setRadio("format", p.format);
  updateDuplexAvailability();
  setRadio("farbe", p.farbe ? "1" : "0");
  setRadio("seiten_modus", p.duplex === "none" ? "einseitig" : "beidseitig");
  $("kopien").value = p.kopien;
  if (hint) { hint.textContent = p.hinweis || ""; hint.hidden = !p.hinweis; }
}
function dropPreset() {
  const custom = form.querySelector('input[name="preset"][value=""]');
  if (custom && !custom.checked) { custom.checked = true; const h = $("preset-hint"); if (h) h.hidden = true; }
}

// ------------------------------------------------------------------ Ereignisse
form.addEventListener("change", (e) => {
  const name = e.target.name;
  if (name === "preset") applyPreset(e.target.value);
  else if (["format", "farbe", "seiten_modus", "kopien"].includes(name)) dropPreset();
  if (name === "seiten_modus" || name === "ausrichtung") state.back = false;
  if (name !== "datei") update();
  if (name === "seiten_modus") demoFlip();
});
$("seiten").addEventListener("input", () => { state.sheet = 0; state.back = false; update(); });
$("seiten-reset").addEventListener("click", () => { $("seiten").value = ""; update(); });
$("kopien").addEventListener("input", () => { dropPreset(); update(); });
form.querySelectorAll(".step").forEach((b) => b.addEventListener("click", () => {
  const k = $("kopien");
  k.value = Math.max(1, Math.min(Number(k.max), (Number(k.value) || 1) + Number(b.dataset.step)));
  dropPreset(); update();
}));
fileInput.addEventListener("change", () => loadFile(fileInput.files[0]));
$("file-clear").addEventListener("click", clearFile);
$("nav-prev").addEventListener("click", () => step(-1));
$("nav-next").addEventListener("click", () => step(1));
$("nav-flip").addEventListener("click", () => { state.back = !state.back; renderPreview(); });
$("sheet").addEventListener("click", () => { if (settings().duplex !== "none") { state.back = !state.back; renderPreview(); } });
document.addEventListener("keydown", (e) => {
  if (!state.file || e.target.matches("input, textarea, select")) return;
  if (e.key === "ArrowRight") step(1);
  if (e.key === "ArrowLeft") step(-1);
});

// Drag & Drop auf die ganze Vorschaufläche
const stage = document.querySelector(".stage");
let dragDepth = 0;
stage.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; stage.classList.add("dragging"); });
stage.addEventListener("dragover", (e) => e.preventDefault());
stage.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; stage.classList.remove("dragging"); } });
stage.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; stage.classList.remove("dragging");
  const f = e.dataTransfer.files[0];
  if (!f) return;
  const dt = new DataTransfer(); dt.items.add(f); fileInput.files = dt.files;
  loadFile(f);
});

let resizeTimer;
new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderPreview, 120); }).observe($("viewport"));

// ------------------------------------------------------------------ Absenden
form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  if ($("submit").disabled || !state.file) return;
  $("duplex").value = computeDuplex();
  const fd = new FormData(form);
  fd.delete("seiten_modus");
  fd.set("kopien", settings().copies);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
  xhr.responseType = "json";
  const bar = $("upload-bar"), prog = $("upload-progress"), btn = $("submit"), label = $("submit-label");
  prog.hidden = false; btn.disabled = true; btn.classList.add("busy"); label.textContent = "Wird hochgeladen …";
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = (e.loaded / e.total) * 100;
    bar.style.width = `${pct}%`;
    if (pct >= 100) label.textContent = "Wird geprüft …";
  };
  xhr.onloadend = () => {
    prog.hidden = true; bar.style.width = "0"; btn.classList.remove("busy"); label.textContent = "Drucken";
    const res = xhr.response || {};
    if (xhr.status === 200 && res.ok) {
      toast(res.message, "ok");
      clearFile();
      refreshJobs(true);
    } else if (xhr.status === 401) {
      window.location = "/login";
    } else {
      toast(res.error || "Hochladen fehlgeschlagen. Prüfe die Verbindung und versuche es erneut.", "error");
      update();
    }
  };
  xhr.send(fd);
});

// ------------------------------------------------------------------ Aufträge
const STEPS = ["queued", "printing", "done"];
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function jobItem(j) {
  const li = el("li", "job"); li.dataset.status = j.status;
  const g = el("div", `job-glyph fmt-${j.format}${j.farbe ? " color" : ""}`);
  g.setAttribute("aria-hidden", "true"); g.append(el("span"), el("span"), el("span"));
  const body = el("div", "job-body");
  body.append(el("div", "job-title", j.datei));
  const orient = j.ausrichtung === "portrait" ? " hoch" : j.ausrichtung === "landscape" ? " quer" : "";
  let meta = `${j.format}${orient}, ${j.farbe ? "Farbe" : "Schwarzweiß"}, ${j.duplex === "none" ? "einseitig" : "beidseitig"}, ${j.druckseiten} Seite${j.druckseiten === 1 ? "" : "n"}`;
  if (j.kopien > 1) meta += ` (${j.kopien} Kopien)`;
  body.append(el("div", "job-meta", meta));
  if (j.fehler && j.status === "error") body.append(el("div", "job-error", j.fehler));
  const side = el("div", "job-side");
  side.append(el("span", `status s-${j.status}`, j.status_text + (j.position ? `, Platz ${j.position}` : "")));
  side.append(el("span", "job-time", j.erstellt_zeit));
  if (j.status === "queued") {
    const f = el("form"); f.method = "post"; f.action = `/jobs/${encodeURIComponent(j.id)}/cancel`;
    const h = el("input"); h.type = "hidden"; h.name = "csrf"; h.value = csrf;
    const b = el("button", "btn-text danger", "Abbrechen"); b.type = "submit";
    f.append(h, b); side.append(f);
  }
  li.append(g, body, side);
  return li;
}
let lastStatuses = {};
function renderJobs(jobs, announce) {
  const list = $("jobs");
  list.replaceChildren();
  if (!jobs.length) {
    list.append(el("li", "jobs-empty", "Hier erscheinen deine Druckaufträge, sobald du ein PDF abschickst."));
    return;
  }
  for (const j of jobs) {
    const li = jobItem(j);
    if (announce && lastStatuses[j.id] && lastStatuses[j.id] !== j.status) {
      li.classList.add("changed");
      if (j.status === "done") toast(`„${j.datei}“ ist gedruckt und liegt am Drucker.`, "ok");
      if (j.status === "error") toast(`„${j.datei}“: ${j.fehler || "Druckfehler"}`, "error");
    }
    list.append(li);
  }
  lastStatuses = Object.fromEntries(jobs.map((j) => [j.id, j.status]));
}
function renderAgent(online) {
  $("agent-pill").className = `pill ${online ? "ok" : "warn"}`;
  $("agent-text").textContent = online ? "Drucker bereit" : "Drucker offline";
  $("agent-pill").title = online ? "Der Druck-PC ist verbunden." : "Der Druck-PC ist gerade nicht verbunden. Aufträge warten und werden gedruckt, sobald er wieder online ist.";
}
let timer = null, firstLoad = true;
async function refreshJobs() {
  clearTimeout(timer);
  try {
    const r = await fetch("/api/my-jobs", { credentials: "same-origin" });
    if (r.status === 401) { window.location = "/login"; return; }
    const data = await r.json();
    renderJobs(data.jobs, !firstLoad);
    firstLoad = false;
    renderAgent(data.agent_online);
    const active = data.jobs.some((j) => j.status === "queued" || j.status === "printing");
    timer = setTimeout(refreshJobs, active ? 3000 : 20000);
  } catch {
    timer = setTimeout(refreshJobs, 15000);
  }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshJobs(); });

// ------------------------------------------------------------------ Helfer
function fmtSize(b) {
  return b >= 1048576 ? `${(b / 1048576).toFixed(1).replace(".", ",")} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
}
function toast(msg, kind) { window.showToast ? window.showToast(msg, kind) : alert(msg); }

if (reduceMotion) document.documentElement.classList.add("reduce-motion");
update();
refreshJobs();
