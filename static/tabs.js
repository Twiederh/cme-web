// Teilt die Seite in Tabs auf. Laeuft nach index.html und translation.js und sortiert
// die vorhandenen Karten um; Inhalte und Logik bleiben unveraendert.
(() => {
  const main = document.querySelector("main");
  const top = (sel) => {
    let el = document.querySelector(sel);
    while (el && el.parentElement !== main) el = el.parentElement;
    return el;
  };

  const style = document.createElement("style");
  style.textContent = `
    nav.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin: 0 0 18px; flex-wrap: wrap; }
    nav.tabs button { border: none; border-bottom: 2px solid transparent; border-radius: 0; background: none;
      padding: 10px 14px; color: var(--muted); font-weight: 500; }
    nav.tabs button:hover { color: var(--text); border-color: var(--line); }
    nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }
    nav.tabs .n { font-weight: 400; font-size: 12px; margin-left: 4px; }
    section.tab-panel[hidden] { display: none !important; }`;
  document.head.appendChild(style);

  // Kompakte Liste der registrierten Geraete fuer die Uebersicht
  const devices = document.createElement("section");
  devices.className = "card";
  devices.innerHTML = `<h2>Geräte <span class="count" id="c-devices"></span></h2>
    <table><thead><tr><th>Gerät</th><th>Typ</th><th>Rufnummern</th><th>IP</th><th>Status</th></tr></thead>
    <tbody id="t-devices"></tbody></table>`;

  // Karten fuer die E.164-Pattern-Maps (Tab Uebersetzungen) und das Absender-Routing (Tab Telefone);
  // static/e164.js fuellt beide
  if (!document.getElementById("e164-card")) {
    for (const id of ["e164-card", "egress-card"]) {
      const c = document.createElement("section");
      c.className = "card";
      c.id = id;
      main.appendChild(c);
    }
    const s = document.createElement("script");
    s.src = "/static/e164.js";
    document.body.appendChild(s);
  }
  // Karten fuer Telefon-Firmware und -Konfiguration (Tab Telefone) und die Warteschlangen (Tab Voicemail);
  // static/firmware.js, static/phonecfg.js und static/queues.js fuellen sie
  for (const [id, src] of [["firmware-card", "/static/firmware.js"], ["phonecfg-card", "/static/phonecfg.js"],
                           ["queue-card", "/static/queues.js"]]) {
    if (document.getElementById(id)) continue;
    const c = document.createElement("section");
    c.className = "card";
    c.id = id;
    main.appendChild(c);
    const s = document.createElement("script");
    s.src = src;
    document.body.appendChild(s);
  }

  const TABS = [
    { id: "overview", label: "Übersicht",
      parts: [top(".kpis"), devices, top("#t-trunks"), top("#raw-out")] },
    { id: "phones", label: "Telefone & Rufnummern",
      parts: [top("#t-phones"), top("#f-phone"), top("#t-numbers"), top("#egress-card"), top("#t-hunts"), top("#t-codecs"), top("#firmware-card"), top("#phonecfg-card")] },
    { id: "translation", label: "Übersetzungen", parts: [top("#translation-card"), top("#e164-card")] },
    { id: "voicemail", label: "Voicemail, IVR & Warteschlangen", parts: [top("#voicemail-card"), top("#queue-card")] },
    { id: "recording", label: "Aufzeichnung", parts: [top("#recording-card")] },
    { id: "user", label: "Benutzereinstellungen", parts: [top("#walls")] },
  ];

  const nav = document.createElement("nav");
  nav.className = "tabs";
  const errors = document.getElementById("errors");
  main.insertBefore(nav, errors ? errors.nextSibling : main.firstChild);

  const panels = {};
  for (const t of TABS) {
    const panel = document.createElement("section");
    panel.className = "tab-panel";
    panel.id = `tab-${t.id}`;
    for (const p of t.parts) if (p) panel.appendChild(p);
    main.appendChild(panel);
    panels[t.id] = panel;
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.tab = t.id;
    b.innerHTML = `${t.label}<span class="n" id="n-${t.id}"></span>`;
    nav.appendChild(b);
  }
  // Was noch lose in main liegt (z. B. spaetere Karten), kommt in die Uebersicht
  [...main.children].forEach(el => {
    if (el !== nav && el !== errors && !el.classList.contains("tab-panel")) panels.overview.appendChild(el);
  });

  function show(id) {
    if (!panels[id]) id = "overview";
    for (const [k, p] of Object.entries(panels)) p.hidden = k !== id;
    nav.querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.tab === id));
    if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  }
  nav.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) { show(b.dataset.tab); window.scrollTo({ top: 0 }); }
  });
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  show(location.hash.slice(1) || "overview");

  // Geraeteliste bei jedem Neuladen der Daten aktualisieren
  const origRender = window.render;
  window.render = function () {
    origRender.apply(this, arguments);
    const d = window.data || data;
    const ph = d.phones || [];
    const reg = ph.filter(p => p.state === "REGISTERED").length;
    document.getElementById("c-devices").textContent = `(${reg} / ${ph.length} registriert)`;
    const order = (p) => (p.state === "REGISTERED" ? 0 : 1);
    document.getElementById("t-devices").innerHTML = ph.length ? [...ph].sort((a, b) => order(a) - order(b)).map(p => {
      const st = p.state === "REGISTERED" ? badge("ok", "registriert")
               : p.state === "UNBEKANNT" ? badge("neutral", "unbekannt") : badge("bad", "offline");
      return `<tr><td>${esc(p.description || `Pool ${p.tag}`)} <span class="muted mono">#${esc(p.tag)}</span></td>
        <td>${p.type ? esc(TYPE_LABEL[p.type] ?? p.type) : '<span class="muted">Fremdgerät</span>'}</td>
        <td class="mono">${p.lines.map(l => esc(l.number)).join(", ")}</td>
        <td class="mono">${esc(p.ip) || '<span class="muted">–</span>'}</td><td>${st}</td></tr>`;
    }).join("") : `<tr><td colspan="5" class="empty">Noch keine Telefone angelegt.</td></tr>`;
    document.getElementById("n-phones").textContent = `${ph.length}`;
    const off = ph.length - reg;
    document.getElementById("n-overview").innerHTML = off ? ` ${badge("bad", `${off} offline`)}` : "";
  };
  if (window.data || (typeof data !== "undefined" && data)) window.render();
})();
