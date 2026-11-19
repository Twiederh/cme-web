// Konfigurationsdateien der Telefone (SEP<MAC>.cnf.xml) im Browser anpassen.
// Die Aenderungen werden als Regeln gespeichert und nach jedem "create profile" wieder angewendet.
// Eigene Karte im Tab "Telefone & Rufnummern"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let P = { files: [] };
  let open = null;
  let hits = [], cur = -1;
  const EDSTYLE = "font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;padding:8px;border:1px solid var(--line);border-radius:7px;" +
                  "white-space:pre;overflow-wrap:normal;tab-size:8;margin:0;";

  let card = document.getElementById("phonecfg-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "phonecfg-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Telefon-Konfiguration <span class="count" id="c-pcfg"></span></h2>
    <div class="body"><div class="hint">Die Konfigurationsdateien der Telefone erzeugt CME bei jedem „create profile“ neu.
      Was du hier änderst, merkt sich die Seite als Anpassung (Wert setzen, Element entfernen, Attribut ändern)
      und schreibt sie danach jedes Mal wieder hinein. Neue Einstellungen von CME bleiben dabei erhalten.
      Das Telefon übernimmt eine geänderte Datei erst nach einem Neustart.</div></div>
    <table><thead><tr><th>Datei</th><th>Telefon</th><th>Anpassungen</th><th></th></tr></thead>
      <tbody id="t-pcfg"><tr><td colspan="4" class="empty">Lädt …</td></tr></tbody></table>
    <div class="body" id="pcfg-editor" style="border-top:1px solid var(--line)" hidden></div>`;

  function render() {
    $("#c-pcfg").textContent = P.files.length ? `(${P.files.length})` : "";
    $("#t-pcfg").innerHTML = P.files.map(f => `<tr>
        <td class="mono" style="font-size:12px">${esc(f.name)}</td>
        <td>${f.pool ? `${esc(f.desc || "Pool " + f.pool)} <span class="muted mono">#${esc(f.pool)}${f.type ? " · " + esc(TYPE_LABEL?.[f.type] ?? f.type) : ""}</span>` : '<span class="muted">–</span>'}</td>
        <td class="mono" style="font-size:12px">${f.rules.length ? f.rules.slice(0, 6).map(esc).join("<br>") + (f.rules.length > 6 ? `<br><span class="muted">… und ${f.rules.length - 6} weitere</span>` : "")
          : '<span class="muted">keine</span>'}</td>
        <td><div class="actions"><button class="small" data-pcfg-edit="${esc(f.name)}">Bearbeiten</button>
          ${f.rules.length ? `<button class="small danger" data-pcfg-reset="${esc(f.name)}">Anpassungen entfernen</button>` : ""}</div></td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Keine Konfigurationsdateien gefunden (show voice register tftp-bind).</td></tr>`;
  }

  async function load() {
    const j = await api("/api/phonecfg");
    if (!j.ok) { $("#t-pcfg").innerHTML = `<tr><td colspan="4" class="empty bad-text">${esc(j.error || "Fehler beim Laden")}</td></tr>`; return; }
    P = j;
    render();
  }

  async function edit(name) {
    const ed = $("#pcfg-editor");
    ed.hidden = false;
    ed.innerHTML = `<div class="empty">Lädt ${esc(name)} …</div>`;
    const j = await api(`/api/phonecfg/${encodeURIComponent(name)}`);
    if (!j.ok) { ed.innerHTML = `<div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div>`; return; }
    open = j;
    const f = P.files.find(x => x.name === name) || {};
    ed.innerHTML = `
      <form id="f-pcfg">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">${esc(name)}
          <span class="muted mono" style="font-weight:400">${esc(j.path)}</span></label>
        <div class="pcfg-search" style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <input type="search" id="pcfg-q" placeholder="Suchen … (Enter: nächster, Umschalt+Enter: vorheriger)" autocomplete="off" style="flex:1;max-width:420px">
          <button type="button" class="small" id="pcfg-prev" title="Vorheriger Treffer">▲</button>
          <button type="button" class="small" id="pcfg-next" title="Nächster Treffer">▼</button>
          <span class="muted mono" id="pcfg-hits" style="font-size:12px;min-width:70px"></span>
        </div>
        <div id="pcfg-wrap" style="position:relative">
          <div id="pcfg-hl" aria-hidden="true" style="position:absolute;inset:0;overflow:hidden;pointer-events:none;box-sizing:border-box;${EDSTYLE}color:transparent;
            background:var(--bg2, transparent)"></div>
          <textarea name="text" spellcheck="false" rows="28" style="position:relative;display:block;width:100%;box-sizing:border-box;${EDSTYLE}
            background:transparent;color:inherit;resize:vertical">${esc(j.text)}</textarea>
        </div>
        ${j.rules.length ? `<div class="hint">Aktive Anpassungen:<br><span class="mono">${j.rules.map(esc).join("<br>")}</span></div>` : ""}
        ${f.pool ? `<label class="check"><input type="checkbox" name="restart" checked> Telefon (Pool ${esc(f.pool)}) danach neu starten</label>` : ""}
        <div class="actions"><button class="primary" type="submit">Speichern</button>
          <button type="button" id="pcfg-orig">Fassung von CME einsetzen</button>
          <button type="button" id="pcfg-close">Schließen</button></div>
        <div class="msg" id="m-pcfg"></div>
      </form>`;
    hits = []; cur = -1;
    const ta = $("#f-pcfg").text;
    ta.addEventListener("scroll", syncScroll);
    ta.addEventListener("input", () => search(false));
    new ResizeObserver(syncSize).observe(ta);
    syncSize();
    $("#pcfg-q").addEventListener("input", () => search(true));
    $("#pcfg-q").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); step(ev.shiftKey ? -1 : 1); }
      else if (ev.key === "Escape" && hits.length) {
        ev.preventDefault();
        const h = hits[cur];
        ta.focus(); ta.setSelectionRange(h, h + $("#pcfg-q").value.length); scrollTo(h);
      }
    });
    ta.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "f") {
        ev.preventDefault();
        const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
        const q = $("#pcfg-q");
        if (sel && !sel.includes("\n")) { q.value = sel; search(true); }
        q.focus(); q.select();
      }
    });
    paint();
    ed.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Suche: Treffer werden in einer Ebene hinter dem (transparenten) Textfeld markiert.
  function search(jump) {
    const ta = $("#f-pcfg")?.text; if (!ta) return;
    const q = $("#pcfg-q").value.toLowerCase();
    const t = ta.value.toLowerCase();
    const old = hits[cur];
    hits = [];
    if (q) for (let i = t.indexOf(q); i >= 0; i = t.indexOf(q, i + q.length)) hits.push(i);
    if (!hits.length) cur = -1;
    else if (jump) {
      // ersten Treffer ab der Cursorposition nehmen
      const from = ta.selectionStart || 0;
      cur = hits.findIndex(h => h >= from); if (cur < 0) cur = 0;
    } else {
      cur = Math.max(0, hits.findIndex(h => h >= (old ?? 0))); if (cur < 0) cur = 0;
    }
    paint();
    if (jump && cur >= 0) scrollTo(hits[cur]);
  }

  function step(d) {
    if (!hits.length) { search(true); return; }
    cur = (cur + d + hits.length) % hits.length;
    paint(); scrollTo(hits[cur]);
  }

  function paint() {
    const ta = $("#f-pcfg")?.text, hl = $("#pcfg-hl"); if (!ta || !hl) return;
    const q = $("#pcfg-q").value, v = ta.value;
    let html = "", pos = 0;
    hits.forEach((h, i) => {
      html += esc(v.slice(pos, h)) +
        `<mark style="color:transparent;border-radius:2px;background:${i === cur ? "rgba(255,140,0,.75)" : "rgba(255,215,0,.45)"}">${esc(v.slice(h, h + q.length))}</mark>`;
      pos = h + q.length;
    });
    hl.innerHTML = html + esc(v.slice(pos)) + "\n ";
    $("#pcfg-hits").textContent = q ? (hits.length ? `${cur + 1} / ${hits.length}` : "kein Treffer") : "";
    $("#pcfg-hits").classList.toggle("bad-text", !!q && !hits.length);
    syncScroll();
  }

  function syncScroll() {
    const ta = $("#f-pcfg")?.text, hl = $("#pcfg-hl"); if (!ta || !hl) return;
    hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft;
  }

  function syncSize() {
    const ta = $("#f-pcfg")?.text, hl = $("#pcfg-hl"); if (!ta || !hl) return;
    // Bildlaufleisten des Textfelds aussparen, damit die Markierungen deckungsgleich bleiben
    hl.style.height = ta.offsetHeight + "px";
    hl.style.width = ta.offsetWidth + "px";
    hl.style.borderRightWidth = (ta.offsetWidth - ta.clientWidth - 1) + "px";
    hl.style.borderBottomWidth = (ta.offsetHeight - ta.clientHeight - 1) + "px";
    syncScroll();
  }

  function scrollTo(idx) {
    const ta = $("#f-pcfg").text, v = ta.value;
    const line = v.slice(0, idx).split("\n").length - 1;
    const col = idx - (v.lastIndexOf("\n", idx - 1) + 1);
    const cs = getComputedStyle(ta);
    const lh = parseFloat(cs.lineHeight) || 17;
    const cw = charWidth(cs.font);
    ta.scrollTop = Math.max(0, line * lh - ta.clientHeight / 3);
    const x = col * cw;
    ta.scrollLeft = x < ta.clientWidth - 60 ? 0 : x - ta.clientWidth / 3;
    syncScroll();
  }

  let _cw = {};
  function charWidth(font) {
    if (!_cw[font]) { const c = document.createElement("canvas").getContext("2d"); c.font = font; _cw[font] = c.measureText("0000000000").width / 10; }
    return _cw[font];
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-pcfg" || !open) return;
    const m = $("#m-pcfg"), btn = f.querySelector("button[type=submit]");
    const pf = P.files.find(x => x.name === open.name) || {};
    btn.disabled = true;
    const j = await api(`/api/phonecfg/${encodeURIComponent(open.name)}`, { method: "PUT",
      body: JSON.stringify({ text: f.text.value, restart_pool: f.restart?.checked ? pf.pool : "" }) });
    btn.disabled = false;
    if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
    await load();
    showMsg(m, true, j.rules.length ? `Gespeichert. ${j.rules.length} Anpassung(en):\n${j.rules.join("\n")}`
                                    : "Gespeichert – keine Abweichung von der Fassung von CME.");
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.pcfgEdit) {
      edit(d.pcfgEdit);
    } else if (d.pcfgReset) {
      if (!confirm(`Alle Anpassungen an ${d.pcfgReset} entfernen und die Fassung von CME zurückschreiben?`)) return;
      const j = await api(`/api/phonecfg/${encodeURIComponent(d.pcfgReset)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      $("#pcfg-editor").hidden = true;
      load();
    } else if (e.target.id === "pcfg-prev" || e.target.id === "pcfg-next") {
      step(e.target.id === "pcfg-next" ? 1 : -1);
    } else if (e.target.id === "pcfg-orig" && open) {
      $("#f-pcfg").text.value = open.original;
      search(false);
    } else if (e.target.id === "pcfg-close") {
      $("#pcfg-editor").hidden = true;
      open = null;
    }
  });

  window.loadPhoneCfg = load;
  load();
})();
