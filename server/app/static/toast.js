// Kleine Toast-Benachrichtigungen (auch für serverseitige Flash-Meldungen).
(function () {
  "use strict";
  const box = document.getElementById("toasts");
  const ICON = { ok: "i-check", error: "i-alert" };

  function dismiss(t) {
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 220);
  }
  function arm(t) {
    const ms = t.classList.contains("error") ? 9000 : 5000;
    let h = setTimeout(() => dismiss(t), ms);
    t.addEventListener("mouseenter", () => clearTimeout(h));
    t.addEventListener("mouseleave", () => { h = setTimeout(() => dismiss(t), 2500); });
    t.addEventListener("click", () => dismiss(t));
  }
  window.showToast = function (msg, kind) {
    kind = kind === "error" ? "error" : "ok";
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.setAttribute("role", kind === "error" ? "alert" : "status");
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "ic");
    const use = document.createElementNS(svgNS, "use");
    use.setAttribute("href", "#" + ICON[kind]);
    svg.append(use);
    const span = document.createElement("span");
    span.textContent = msg;
    t.append(svg, span);
    while (box.children.length >= 3) box.firstElementChild.remove();
    box.append(t);
    arm(t);
  };
  if (box) Array.from(box.children).forEach(arm);
})();
