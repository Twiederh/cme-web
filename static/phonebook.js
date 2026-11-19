// Telefonbuch (phonebook.py): Namen zu Rufnummern, Anzeige beim Klingeln, Verzeichnis am Telefon, Kurzwahltasten.
// Eigene Karte im Tab "Telefonbuch"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let B = { entries: [], settings: {}, peers: [] };
  let SD = { pools: [] };
  let filter = "", onlyEmpty = false;

  let card = document.getElementById("phonebook-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "phonebook-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Telefonbuch <span class="count" id="c-pb"></span></h2>
    <div class="body">
      <div class="hint">Rufnummern mit Namen. Eingehende Anrufe sammelt die Seite jede Minute aus der Anrufliste des Routers;
        neue Nummern erscheinen hier ohne Namen – Name eintragen, fertig. Die Namen erscheinen in der Voicemail (Web und Telefon),
        im Verzeichnis am Telefon und – wenn eingeschaltet – beim Klingeln.</div>
      <form id="f-pb-add" class="row" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:10px 0">
        <div><label>Rufnummer</label><input name="number" placeholder="z. B. 006187123456" required></div>
        <div style="flex:1;min-width:200px"><label>Name</label><input name="name" placeholder="Name" style="width:100%"></div>
        <div class="actions" style="margin:0"><button class="primary" type="submit">Hinzufügen</button></div>
      </form>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <input type="search" id="pb-q" placeholder="Suchen …" style="max-width:260px">
        <label class="check" style="margin:0"><input type="checkbox" id="pb-empty"> nur ohne Namen</label>
        <button class="small" type="button" id="pb-collect">Anrufliste jetzt abfragen</button>
        <span class="muted" id="pb-status" style="font-size:12px"></span>
      </div>
      <div class="msg" id="m-pb"></div>
    </div>
    <table><thead><tr><th>Name</th><th>Rufnummer</th><th>Herkunft</th><th>Anrufe</th><th>Zuletzt</th><th></th></tr></thead>
      <tbody id="t-pb"><tr><td colspan="6" class="empty">Lädt …</td></tr></tbody></table>
    <div class="body" style="border-top:1px solid var(--line)" id="pb-settings"></div>
    <div class="body" style="border-top:1px solid var(--line)" id="pb-sd"></div>`;

  const SRC = { manual: ["neutral", "von Hand"], router: ["neutral", "Anrufliste"], lookup: ["ok", "Telefonbuch"] };
  const when = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";

  function renderTable() {
    const q = filter.toLowerCase();
    const rows = B.entries.filter(e => (!onlyEmpty || !e.name) &&
      (!q || (e.name || "").toLowerCase().includes(q) || e.number.includes(q.replace(/\s/g, ""))));
    const unnamed = B.entries.filter(e => !e.name).length;
    $("#c-pb").textContent = `(${B.entries.length}${unnamed ? `, ${unnamed} ohne Namen` : ""})`;
    $("#t-pb").innerHTML = rows.map(e => {
      const s = SRC[e.source] || SRC.manual;
      return `<tr>
        <td><input data-pb-name="${esc(e.id)}" value="${esc(e.name || "")}" placeholder="Name eintragen …" style="width:100%;min-width:180px"></td>
        <td class="mono">${esc(e.number)}</td>
        <td>${badge(s[0], s[1])}</td>
        <td>${e.calls ? `${e.calls}${e.answered ? ` <span class="muted">(${e.answered} angenommen)</span>` : ""}` : '<span class="muted">–</span>'}</td>
        <td class="muted" style="font-size:12px">${when(e.last)}</td>
        <td><div class="actions">${!e.name ? `<button class="small" data-pb-lookup="${esc(e.id)}" title="Im öffentlichen Telefonbuch suchen">Suchen</button>` : ""}
          <button class="small danger" data-pb-del="${esc(e.id)}">Löschen</button></div></td></tr>`;
    }).join("") || `<tr><td colspan="6" class="empty">${B.entries.length ? "Keine Treffer." : "Noch keine Einträge."}</td></tr>`;
    const st = B.status || {};
    $("#pb-status").textContent = B.demo ? "Demo: keine Anrufliste" :
      (st.error ? `Anrufliste: ${st.error}` : st.poll ? `Anrufliste zuletzt abgefragt ${when(st.poll)}` : "");
    $("#pb-status").classList.toggle("bad-text", !!st.error);
    dlist();
  }

  function dlist() {
    let dl = document.getElementById("pb-dl");
    if (!dl) { dl = document.createElement("datalist"); dl.id = "pb-dl"; document.body.appendChild(dl); }
    dl.innerHTML = B.entries.filter(e => e.name).map(e => `<option value="${esc(e.number)}">${esc(e.name)}</option>`).join("");
  }

  function renderSettings() {
    const s = B.settings;
    const peers = B.peers.filter(p => p.target || p.incoming);
    $("#pb-settings").innerHTML = `<form id="f-pb-set">
      <h3 style="margin:0 0 8px;font-size:14px">Einstellungen</h3>
      <label class="check"><input type="checkbox" name="collect" ${s.collect ? "checked" : ""}> Eingehende Anrufe aus der Anrufliste des Routers sammeln</label>
      <div class="hint">Der Router hebt standardmäßig nur wenige Anrufe für kurze Zeit auf. Mehr Puffer:
        <span class="mono">dial-control-mib retain-timer 60</span> und <span class="mono">dial-control-mib max-size 500</span>.</div>
      <div style="margin:10px 0 4px"><b>Dial-Peers vom Provider (eingehend)</b> <span class="muted">– nur Anrufe über diese Dial-Peers sammeln; hier wirkt auch die Anzeige beim Klingeln</span></div>
      <div style="display:flex;flex-wrap:wrap;gap:4px 16px">${peers.map(p => `<label class="check" style="margin:0">
        <input type="checkbox" name="peer" value="${esc(p.tag)}" ${s.peers.includes(p.tag) ? "checked" : ""}>
        <span class="mono">${esc(p.tag)}</span> ${esc(p.desc || p.pattern || p.target)}</label>`).join("") || '<span class="muted">Keine VoIP-Dial-Peers gefunden.</span>'}</div>
      <label class="check" style="margin-top:10px"><input type="checkbox" name="ring_names" ${s.ring_names ? "checked" : ""}>
        Namen beim Klingeln anzeigen</label>
      <div class="hint">Legt am Router das eingehende SIP-Profil <span class="mono">voice class sip-profiles</span>
        <input name="profile" type="number" min="2" max="10000" value="${esc(s.profile)}" style="width:80px"> an
        (je Name eine Regel, die den Anzeigenamen im <span class="mono">From</span> setzt), schaltet
        <span class="mono">sip-profiles inbound</span> ein und hängt es an die gewählten Dial-Peers
        (<span class="mono">voice-class sip profiles … inbound</span>). Umlaute werden umgeschrieben (ä → ae).
        Nach jeder Namensänderung wird das Profil neu geschrieben.</div>
      <label class="check" style="margin-top:10px"><input type="checkbox" name="lookup" ${s.lookup ? "checked" : ""}>
        Neue Nummern im öffentlichen Telefonbuch suchen (Rückwärtssuche)</label>
      <div class="hint">Fragt für jede neue deutsche Nummer einmal die Rückwärtssuche ab. Adresse
        (<span class="mono">{number}</span> = Nummer national mit 0):<br>
        <input name="lookup_url" value="${esc(s.lookup_url)}" style="width:100%;max-width:560px" class="mono"></div>
      <div class="actions"><button class="primary" type="submit">Einstellungen speichern</button></div>
      <div class="msg" id="m-pb-set"></div>
    </form>
    <form id="f-pb-dir" style="margin-top:14px">
      <h3 style="margin:0 0 8px;font-size:14px">Verzeichnis am Telefon</h3>
      <div class="hint">Die Taste <b>Verzeichnisse</b> zeigt das Telefonbuch (Einträge mit Namen und die internen Rufnummern)
        mit Suche; Eintrag wählen und <b>Wählen</b> drücken. Adresse: <span class="mono">${esc(B.dir_url)}</span>
        – ${B.dir_phones}/${B.phones} Telefone eingerichtet.</div>
      <label class="check"><input type="checkbox" name="enabled" ${B.dir_phones ? "checked" : ""}> Taste Verzeichnisse öffnet das Telefonbuch</label>
      <label class="check"><input type="checkbox" name="restart" checked> Geänderte Telefone neu starten</label>
      <div class="actions"><button class="primary" type="submit">Übernehmen</button></div>
      <div class="msg" id="m-pb-dir"></div>
    </form>`;
  }

  function renderSD() {
    const pools = SD.pools.filter(p => p.type || p.lines.length);
    $("#pb-sd").innerHTML = `<h3 style="margin:0 0 8px;font-size:14px">Kurzwahltasten</h3>
      <div class="hint">Freie Tasten am Telefon (nach den Leitungen) mit einer Nummer belegen. Die Nummer so eintragen, wie das Telefon
        sie wählen würde (mit Amtsholung); aus dem Telefonbuch auswählbar. Danach <span class="mono">create profile</span>
        und Neustart des Telefons.</div>
      ${pools.map(p => `<details data-sd-pool="${esc(p.tag)}" style="margin:8px 0">
        <summary><b>${esc(p.desc || "Pool " + p.tag)}</b> <span class="muted mono">#${esc(p.tag)}${p.type ? " · " + esc(p.type) : ""}</span>
          – ${p.speed.length ? `${p.speed.length} Kurzwahl${p.speed.length > 1 ? "en" : ""}` : '<span class="muted">keine Kurzwahlen</span>'}</summary>
        <form data-sd-form="${esc(p.tag)}" style="margin:8px 0 0 16px">
          <table style="width:auto"><thead><tr><th>#</th><th>Nummer</th><th>Beschriftung</th><th></th></tr></thead>
            <tbody>${[...p.speed, { number: "", label: "" }].map((s, i) => sdRow(i + 1, s)).join("")}</tbody></table>
          <div class="actions"><button type="button" class="small" data-sd-addrow>Zeile hinzufügen</button>
            <label class="check" style="margin:0 8px"><input type="checkbox" name="restart" checked> Telefon neu starten</label>
            <button class="primary" type="submit">Speichern</button></div>
          <div class="msg"></div>
        </form></details>`).join("") || '<div class="empty">Keine Telefone.</div>'}`;
  }

  const sdRow = (i, s) => `<tr><td class="muted">${i}</td>
    <td><input name="sd-number" list="pb-dl" value="${esc(s.number)}" placeholder="Nummer" class="mono" style="width:180px"></td>
    <td><input name="sd-label" value="${esc(s.label)}" placeholder="Beschriftung" maxlength="30" style="width:200px"></td>
    <td><button type="button" class="small" data-sd-del title="Zeile entfernen">✕</button></td></tr>`;

  async function load() {
    const j = await api("/api/phonebook");
    if (!j.ok) { $("#t-pb").innerHTML = `<tr><td colspan="6" class="empty bad-text">${esc(j.error || "Fehler")}</td></tr>`; return; }
    B = j;
    renderTable();
    renderSettings();
  }

  async function loadSD() {
    const j = await api("/api/phonebook/speeddials");
    if (j.ok) { SD = j; renderSD(); }
    else $("#pb-sd").innerHTML = `<div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div>`;
  }

  const routerNote = (r) => r ? (r.ok ? ` Router: ${r.rules} Name(n) im SIP-Profil.` : ` Router: ${r.error || "Fehler"}`) : "";

  card.addEventListener("input", (e) => {
    if (e.target.id === "pb-q") { filter = e.target.value; renderTable(); }
    if (e.target.name === "sd-number") {
      const ent = B.entries.find(x => x.number === e.target.value && x.name);
      const lab = e.target.closest("tr").querySelector("input[name=sd-label]");
      if (ent && !lab.value) lab.value = ent.name.slice(0, 30);
    }
  });

  card.addEventListener("change", async (e) => {
    if (e.target.id === "pb-empty") { onlyEmpty = e.target.checked; renderTable(); return; }
    const id = e.target.dataset?.pbName;
    if (id) {
      const j = await api(`/api/phonebook/entries/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ name: e.target.value }) });
      const ent = B.entries.find(x => x.id === id);
      if (j.ok && ent) ent.name = e.target.value.trim();
      showMsg($("#m-pb"), j.ok && (!j.router || j.router.ok), j.ok ? `Gespeichert: ${e.target.value || "(ohne Namen)"}.` + routerNote(j.router) : (j.error || "Fehler"));
      dlist();
    }
  });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-pb-add") {
      const j = await api("/api/phonebook/entries", { method: "POST", body: JSON.stringify({ number: f.number.value, name: f.name.value }) });
      if (!j.ok) { showMsg($("#m-pb"), false, j.error || "Fehler"); return; }
      f.reset();
      await load();
      showMsg($("#m-pb"), !j.router || j.router.ok, "Hinzugefügt." + routerNote(j.router));
    } else if (f.id === "f-pb-set") {
      const body = { collect: f.collect.checked, ring_names: f.ring_names.checked, profile: f.profile.value,
        lookup: f.lookup.checked, lookup_url: f.lookup_url.value,
        peers: [...f.querySelectorAll("input[name=peer]:checked")].map(i => i.value) };
      if (body.ring_names && !body.peers.length) { showMsg($("#m-pb-set"), false, "Für die Anzeige beim Klingeln mindestens einen Dial-Peer wählen."); return; }
      const btn = f.querySelector("button[type=submit]"); btn.disabled = true;
      const j = await api("/api/phonebook/settings", { method: "PUT", body: JSON.stringify(body) });
      btn.disabled = false;
      await load();
      showMsg($("#m-pb-set"), j.ok, j.ok ? "Gespeichert." + (j.rules !== undefined ? ` ${j.rules} Name(n) im SIP-Profil.` : "") : (j.error || "Fehler"));
    } else if (f.id === "f-pb-dir") {
      const btn = f.querySelector("button[type=submit]"); btn.disabled = true;
      const j = await api("/api/phonebook/directory", { method: "POST", body: JSON.stringify({ enabled: f.enabled.checked, restart: f.restart.checked }) });
      btn.disabled = false;
      if (!j.ok) { showMsg($("#m-pb-dir"), false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-pb-dir"), !j.errors.length, `${j.files.length} Telefon-Konfiguration(en) aktualisiert.` +
        (j.restarted.length ? ` Neu gestartet: Pool ${j.restarted.join(", ")}.` : "") + (j.errors.length ? "\n" + j.errors.join("\n") : ""));
      window.loadPhoneCfg?.();
    } else if (f.dataset.sdForm) {
      const pool = f.dataset.sdForm;
      const speed = [...f.querySelectorAll("tbody tr")].map(tr => ({
        number: tr.querySelector("input[name=sd-number]").value.trim(), label: tr.querySelector("input[name=sd-label]").value.trim() }))
        .filter(s => s.number);
      const btn = f.querySelector("button[type=submit]"); btn.disabled = true;
      const j = await api(`/api/phonebook/speeddials/${pool}`, { method: "PUT", body: JSON.stringify({ speed, restart: f.restart.checked }) });
      btn.disabled = false;
      if (!j.ok) { showMsg(f.querySelector(".msg"), false, j.error || "Fehler"); return; }
      await loadSD();
      const d = card.querySelector(`details[data-sd-pool="${pool}"]`);
      if (d) { d.open = true; showMsg(d.querySelector(".msg"), true, `${speed.length} Kurzwahl(en) gespeichert.`); }
    }
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.pbDel) {
      const ent = B.entries.find(x => x.id === d.pbDel);
      if (!confirm(`${ent?.name || ent?.number} aus dem Telefonbuch löschen?`)) return;
      const j = await api(`/api/phonebook/entries/${encodeURIComponent(d.pbDel)}`, { method: "DELETE" });
      if (!j.ok) { showMsg($("#m-pb"), false, j.error || "Fehler"); return; }
      load();
    } else if (d.pbLookup) {
      const ent = B.entries.find(x => x.id === d.pbLookup);
      e.target.disabled = true;
      const j = await api("/api/phonebook/lookup", { method: "POST", body: JSON.stringify({ id: ent.id, number: ent.number }) });
      e.target.disabled = false;
      showMsg($("#m-pb"), j.ok && !!j.name, j.ok ? (j.name ? `Gefunden: ${j.name}` : `Nichts gefunden (${j.info}${j.national ? ", gesucht: " + j.national : ""}).`) : (j.error || "Fehler"));
      if (j.name) load();
    } else if (e.target.id === "pb-collect") {
      e.target.disabled = true;
      const j = await api("/api/phonebook/collect", { method: "POST", body: "{}" });
      e.target.disabled = false;
      showMsg($("#m-pb"), j.ok, j.ok ? `${j.added} neue Nummer(n).` : (j.error || "Fehler"));
      load();
    } else if (d.sdAddrow !== undefined) {
      const tb = e.target.closest("form").querySelector("tbody");
      tb.insertAdjacentHTML("beforeend", sdRow(tb.children.length + 1, { number: "", label: "" }));
    } else if (d.sdDel !== undefined) {
      const tb = e.target.closest("tbody");
      e.target.closest("tr").remove();
      [...tb.children].forEach((tr, i) => tr.firstElementChild.textContent = i + 1);
    }
  });

  window.loadPhonebook = load;
  load();
  loadSD();
})();
