// Konfigurationsdateien der Telefone (SEP<MAC>.cnf.xml) im Browser anpassen.
// Die Aenderungen werden als Regeln gespeichert und nach jedem "create profile" wieder angewendet.
// Eigene Karte im Tab "Telefone & Rufnummern"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let P = { files: [] };
  let open = null;

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
        <textarea name="text" spellcheck="false" rows="28" style="width:100%;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;padding:8px;
          border:1px solid var(--line);border-radius:7px;background:var(--bg2, transparent);color:inherit;white-space:pre;overflow:auto">${esc(j.text)}</textarea>
        ${j.rules.length ? `<div class="hint">Aktive Anpassungen:<br><span class="mono">${j.rules.map(esc).join("<br>")}</span></div>` : ""}
        ${f.pool ? `<label class="check"><input type="checkbox" name="restart" checked> Telefon (Pool ${esc(f.pool)}) danach neu starten</label>` : ""}
        <div class="actions"><button class="primary" type="submit">Speichern</button>
          <button type="button" id="pcfg-orig">Fassung von CME einsetzen</button>
          <button type="button" id="pcfg-close">Schließen</button></div>
        <div class="msg" id="m-pcfg"></div>
      </form>`;
    ed.scrollIntoView({ behavior: "smooth", block: "start" });
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
    } else if (e.target.id === "pcfg-orig" && open) {
      $("#f-pcfg").text.value = open.original;
    } else if (e.target.id === "pcfg-close") {
      $("#pcfg-editor").hidden = true;
      open = null;
    }
  });

  window.loadPhoneCfg = load;
  load();
})();
