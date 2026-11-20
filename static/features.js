// Rufuebernahme-Gruppen, Berechtigungen (COR) und Konferenzraeume (Asterisk ConfBridge).
// Drei Karten, angelegt von tabs.js (pickup-card, cor-card im Tab Telefone; conf-card im Tab Voicemail).
// Nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  const card = (id) => {
    let c = document.getElementById(id);
    if (!c) {
      c = document.createElement("section");
      c.className = "card";
      c.id = id;
      document.querySelector("main").appendChild(c);
    }
    return c;
  };
  const cfg = (j) => (j.config?.length ? `\n\n${j.config.join("\n")}` : "");

  // ======================= Rufuebernahme =======================
  const pc = card("pickup-card");
  let P = null;

  function renderPickup() {
    const groups = [...new Set(P.dns.map(d => d.group).filter(Boolean))].sort();
    pc.innerHTML = `<h2>Rufübernahme <span class="count">${groups.length ? `(${groups.length} Gruppe${groups.length > 1 ? "n" : ""})` : ""}</span>
        <span class="right muted" style="font-weight:400;font-size:12px">pickup-group</span></h2>
      <div class="body"><div class="hint">Rufnummern in derselben Gruppe können Anrufe füreinander annehmen:
        am Telefon die Taste <b>PickUp</b> (Übernahme) drücken, solange es bei einem Kollegen klingelt.
        Die Telefone der Mitglieder bekommen dafür die Softkeys PickUp und GPickUp (Template) und werden neu gestartet.
        Gruppennummer frei wählbar (Ziffern), z. B. 1 für „Büro“.
        ${P.directed ? "" : "<br>Eingestellt: <span class=\"mono\">no service directed-pickup</span> – PickUp übernimmt direkt in der eigenen Gruppe."}</div></div>
      <form id="f-pickup">
        <table><thead><tr><th>Rufnummer</th><th>Telefon (Pool)</th><th>Gruppe</th></tr></thead><tbody>
          ${P.dns.map(d => `<tr><td><span class="mono">${esc(d.number)}</span> <span class="muted">${esc(d.name)}</span></td>
            <td class="mono">${d.pools.map(esc).join(", ") || '<span class="muted">–</span>'}</td>
            <td><input name="g:${esc(d.tag)}" value="${esc(d.group)}" class="mono" style="width:90px" placeholder="keine" inputmode="numeric"></td></tr>`).join("")
            || '<tr><td colspan="3" class="empty">Keine Rufnummern.</td></tr>'}
        </tbody></table>
        <div class="body"><label class="check"><input type="checkbox" name="restart" checked> Betroffene Telefone neu starten</label>
          <div class="actions"><button class="primary" type="submit">Am Router übernehmen</button></div>
          <div class="msg" id="m-pickup"></div></div>
      </form>`;
  }

  async function loadPickup() {
    const j = await api("/api/pickup");
    if (!j.ok) { pc.innerHTML = `<h2>Rufübernahme</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    P = j;
    renderPickup();
  }

  pc.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    const assign = {};
    f.querySelectorAll("input[name^='g:']").forEach(i => { assign[i.name.slice(2)] = i.value.trim(); });
    btn.disabled = true;
    const j = await api("/api/pickup", { method: "PUT", body: JSON.stringify({ assign, restart: f.restart.checked }) });
    btn.disabled = false;
    if (!j.ok) { showMsg($("#m-pickup"), false, j.error || "Fehler"); return; }
    await loadPickup();
    showMsg($("#m-pickup"), true, j.unchanged ? "Keine Änderung." :
      `Übernommen.${j.restarted?.length ? ` Neustart: Pool ${j.restarted.join(", ")}.` : ""}${cfg(j)}`);
  });

  // ======================= Berechtigungen (COR) =======================
  const cc = card("cor-card");
  let C = null;

  function renderCor() {
    const opts = (sel) => Object.entries(C.stages).map(([k, l]) =>
      `<option value="${esc(k)}" ${k === sel ? "selected" : ""}>${esc(l)}</option>`).join("");
    const unsupported = C.providers.filter(p => !p.supported);
    cc.innerHTML = `<h2>Berechtigungen <span class="count"></span>
        <span class="right muted" style="font-weight:400;font-size:12px">Class of Restriction</span></h2>
      <div class="body"><div class="hint">Je Telefon festlegen, wohin es nach draußen telefonieren darf.
        <b>Notrufe (110, 112) und 116xxx gehen immer.</b> „Unbeschränkt“ erlaubt auch Sonderrufnummern
        (0900, 0137, 0180, 0700, Call-by-Call …) – alles, was nicht eindeutig Festnetz, Mobilfunk oder Ausland ist.
        Dafür legt cme-web Kopien der Provider-Dial-Peers mit engeren Mustern an und hält sie automatisch aktuell.
        ${unsupported.length ? `<br><b>Hinweis:</b> Dial-Peer ${unsupported.map(p => `${esc(p.tag)} (${esc(p.pattern || "ohne Muster")})`).join(", ")}
          hat ein Muster, das cme-web nicht aufteilen kann – darüber gehen Anrufe ohne Prüfung.` : ""}</div></div>
      <form id="f-cor">
        <table><thead><tr><th>Telefon</th><th>Rufnummern</th><th>Darf anrufen</th></tr></thead><tbody>
          ${C.pools.map(p => `<tr><td>${esc(p.desc || "Pool " + p.tag)} <span class="muted mono">#${esc(p.tag)}</span></td>
            <td class="mono">${p.numbers.map(esc).join(", ")}</td>
            <td>${p.foreign ? '<span class="muted">eigene COR-Liste am Router</span>'
                 : `<select name="s:${esc(p.tag)}">${opts(p.stage)}</select>`}</td></tr>`).join("")
            || '<tr><td colspan="3" class="empty">Keine Telefone.</td></tr>'}
        </tbody></table>
        <div class="body">
          <div class="actions"><button class="primary" type="submit">Am Router übernehmen</button>
            ${C.active ? badge("ok", `aktiv, ${C.clones.length} Kopie(n) der Provider-Dial-Peers`) : badge("neutral", "keine Einschränkungen")}
            ${C.pending ? badge("warn", "Kopien werden gerade abgeglichen") : ""}
            ${C.sync?.error ? badge("bad", C.sync.error) : ""}</div>
          <div class="msg" id="m-cor"></div>
          ${C.active ? `<details style="margin-top:10px"><summary class="muted">Provider-Dial-Peers und Kopien</summary>
            <table style="margin-top:6px"><tbody>${C.providers.map(p => `<tr><td class="mono">${esc(p.tag)}</td><td>${esc(p.desc)}</td>
              <td class="mono">${esc(p.pattern)}</td><td class="mono" style="font-size:12px">${p.clones.map(esc).join(" ")}</td></tr>`).join("")}
            </tbody></table></details>` : ""}
        </div>
      </form>`;
  }

  async function loadCor() {
    const j = await api("/api/cor");
    if (!j.ok) { cc.innerHTML = `<h2>Berechtigungen</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    C = j;
    renderCor();
  }

  cc.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    const stages = {};
    f.querySelectorAll("select[name^='s:']").forEach(s => { stages[s.name.slice(2)] = s.value; });
    btn.disabled = true;
    const j = await api("/api/cor", { method: "PUT", body: JSON.stringify({ stages }) });
    btn.disabled = false;
    if (!j.ok) { showMsg($("#m-cor"), false, j.error || "Fehler"); return; }
    await loadCor();
    showMsg($("#m-cor"), true, j.unchanged ? "Keine Änderung." : `Übernommen.${cfg(j)}`);
  });

  // ======================= Konferenzraeume =======================
  const fc = card("conf-card");
  let K = null, editing = null;

  function roomForm(r) {
    const v = r || { name: "", number: "", external: "", pin: "", mpin: "", max: "", music: "", announce: true };
    return `<form id="f-conf" data-id="${r ? r.id : ""}">
      <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">${r ? `Raum „${esc(r.name)}“ bearbeiten` : "Neuer Konferenzraum"}</label>
      <div class="row">
        <div><label>Name *</label><input name="name" required maxlength="40" value="${esc(v.name)}" placeholder="Team-Besprechung"></div>
        <div><label>Interne Nummer *</label><input name="number" required class="mono" value="${esc(v.number)}" placeholder="7001"></div>
      </div>
      <div class="row">
        <div><label>Von extern erreichbar unter (optional)</label><input name="external" class="mono" value="${esc(v.external)}"
          placeholder="4961874191285"></div>
        <div><label>Höchstens Teilnehmer (optional)</label><input name="max" type="number" min="2" max="50" value="${esc(v.max || "")}"></div>
      </div>
      <div class="row">
        <div><label>PIN für Teilnehmer (optional)</label><input name="pin" class="mono" inputmode="numeric" maxlength="8" value="${esc(v.pin)}"></div>
        <div><label>Moderator-PIN (optional)</label><input name="mpin" class="mono" inputmode="numeric" maxlength="8" value="${esc(v.mpin)}"></div>
      </div>
      <div class="row">
        <div><label>Wartemusik, solange man allein ist / auf den Moderator wartet</label><select name="music">
          <option value="">keine</option>${K.music.map(m => `<option value="${esc(m.id)}" ${m.id === v.music ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></div>
        <div><label class="check" style="margin-top:18px"><input type="checkbox" name="announce" ${v.announce !== false ? "checked" : ""}>
          Ansagen (Anzahl der Teilnehmer, „Sie sind allein“)</label></div>
      </div>
      <div class="hint">Externe Nummer: so, wie der Router sie für den eingehenden Anruf sieht (wie bei Fax/IVR).
        Mit Moderator-PIN beginnt die Konferenz erst, wenn der Moderator da ist, und endet, wenn er auflegt;
        Teilnehmer ohne eigene PIN drücken bei der Abfrage nur #. Wartemusik lädt man unter Warteschlangen hoch.
        Tasten im Raum: 1 stumm/laut, 4/6 leiser/lauter; Moderator zusätzlich 2 Raum sperren, 3 letzten entfernen, 9 alle stumm.</div>
      <div class="actions"><button class="primary" type="submit">${r ? "Speichern" : "Anlegen"}</button>
        ${r ? '<button type="button" class="small" id="conf-cancel">Abbrechen</button>' : ""}</div>
      <div class="msg" id="m-conf"></div>
    </form>`;
  }

  function renderConf() {
    if (!K.enabled) { fc.hidden = true; return; }
    fc.hidden = false;
    const r = editing ? K.rooms.find(x => x.id === editing) : null;
    fc.innerHTML = `<h2>Konferenzräume <span class="count">${K.rooms.length ? `(${K.rooms.length})` : ""}</span>
        <span class="right muted" style="font-weight:400;font-size:12px">Asterisk ConfBridge</span></h2>
      <table><thead><tr><th>Raum</th><th>Nummer</th><th>Schutz</th><th>Jetzt</th><th>Router</th><th></th></tr></thead><tbody>
        ${K.rooms.map(x => `<tr><td>${esc(x.name)}</td>
          <td class="mono">${esc(x.number)}${x.external ? `<br><span class="muted">extern ${esc(x.external)}</span>` : ""}</td>
          <td>${x.pin ? badge("neutral", "PIN") : ""} ${x.mpin ? badge("neutral", "Moderator") : ""} ${!x.pin && !x.mpin ? '<span class="muted">offen</span>' : ""}</td>
          <td>${x.active ? badge("ok", `${x.active} Teilnehmer`) : '<span class="muted">leer</span>'}</td>
          <td>${x.dialpeer ? `<span class="muted">Dial-Peer ${esc(x.dialpeer)}${x.dialpeer_ext ? ", " + esc(x.dialpeer_ext) : ""}</span>` : badge("warn", "kein Dial-Peer")}</td>
          <td><div class="actions"><button class="small" data-conf-edit="${x.id}">Bearbeiten</button>
            <button class="small danger" data-conf-del="${x.id}">Löschen</button></div></td></tr>`).join("")
          || '<tr><td colspan="6" class="empty">Noch kein Konferenzraum.</td></tr>'}</tbody></table>
      <div class="body" style="border-top:1px solid var(--line)">${roomForm(r)}</div>`;
  }

  async function loadConf() {
    const j = await api("/api/conf");
    if (!j.ok) { fc.innerHTML = `<h2>Konferenzräume</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    K = j;
    renderConf();
  }

  fc.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]"), id = f.dataset.id;
    const body = { name: f.name.value, number: f.number.value, external: f.external.value, pin: f.pin.value,
                   mpin: f.mpin.value, max: f.max.value, music: f.music.value, announce: f.announce.checked };
    btn.disabled = true;
    const j = await api(id ? `/api/conf/${id}` : "/api/conf", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    btn.disabled = false;
    if (!j.ok) { showMsg($("#m-conf"), false, j.error || "Fehler"); return; }
    editing = null;
    await loadConf();
    showMsg($("#m-conf"), true, `Gespeichert.${cfg(j)}`);
  });

  fc.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.confEdit) {
      editing = +d.confEdit;
      renderConf();
      $("#f-conf").scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (e.target.id === "conf-cancel") {
      editing = null;
      renderConf();
    } else if (d.confDel) {
      const r = K.rooms.find(x => x.id === +d.confDel);
      if (!confirm(`Konferenzraum „${r.name}“ löschen? Der Dial-Peer am Router wird entfernt.`)) return;
      const j = await api(`/api/conf/${d.confDel}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadConf();
    }
  });

  // ======================= Autoregistrierung =======================
  let ac = document.getElementById("autoreg-card");
  if (!ac) {
    ac = document.createElement("section");
    ac.className = "card";
    ac.id = "autoreg-card";
    cc.after(ac);
  }
  let A = null;

  function renderAutoreg() {
    ac.innerHTML = `<h2>Autoregistrierung <span class="count"></span>
        <span class="right muted" style="font-weight:400;font-size:12px">voice register global / auto-register</span></h2>
      <div class="body"><div class="hint">Eingeschaltet melden sich neue SIP-Telefone (z. B. ATA 190) ohne vorher angelegten Pool an
        und bekommen die nächste freie Nummer aus dem Bereich. Am Telefon wird dafür das Passwort eingegeben.
        Nach der Einrichtung besser wieder ausschalten, damit sich keine fremden Geräte anmelden.</div>
      <form id="f-autoreg">
        <label class="check"><input type="checkbox" name="enabled" ${A.enabled ? "checked" : ""}> Autoregistrierung eingeschaltet</label>
        <div class="row">
          <div><label>Rufnummern von</label><input name="start" class="mono" inputmode="numeric" value="${esc(A.start)}" placeholder="300"></div>
          <div><label>bis</label><input name="end" class="mono" inputmode="numeric" value="${esc(A.end)}" placeholder="399"></div>
        </div>
        <div class="row">
          <div><label>Passwort ${A.password ? "(gesetzt – leer lassen, um es zu behalten)" : "*"}</label>
            <input name="password" class="mono" autocomplete="new-password" placeholder="${A.password ? "••••••" : ""}"></div>
          <div></div>
        </div>
        <div class="actions"><button class="primary" type="submit">Am Router übernehmen</button>
          ${A.enabled ? badge("warn", "eingeschaltet") : badge("neutral", "ausgeschaltet")}</div>
        <div class="msg" id="m-autoreg"></div>
      </form>
      ${A.pools.length ? `<div class="hint" style="margin-top:12px">Automatisch angelegt: ${A.pools.map(p =>
        `Pool ${esc(p.tag)}${p.type ? " (" + esc(p.type) + ")" : ""} – ${p.numbers.map(esc).join(", ")}`).join("; ")}.
        Über „Bearbeiten“ in der Telefonliste lassen sich Beschreibung und Rufnummern dauerhaft festlegen.</div>` : ""}
      </div>`;
  }

  async function loadAutoreg() {
    const j = await api("/api/autoreg");
    if (!j.ok) { ac.innerHTML = `<h2>Autoregistrierung</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    A = j;
    renderAutoreg();
  }

  ac.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const j = await api("/api/autoreg", { method: "PUT", body: JSON.stringify({
      enabled: f.enabled.checked, start: f.start.value, end: f.end.value, password: f.password.value }) });
    btn.disabled = false;
    if (!j.ok) { showMsg($("#m-autoreg"), false, j.error || "Fehler"); return; }
    await loadAutoreg();
    showMsg($("#m-autoreg"), true, j.unchanged ? "Keine Änderung." : `Übernommen.${cfg(j)}`);
  });

  loadPickup(); loadCor(); loadAutoreg(); loadConf();

  // Sprachpakete (static/locales.js) im selben Tab
  if (!document.getElementById("locale-card")) {
    const s = document.createElement("script");
    s.src = "/static/locales.js";
    document.body.appendChild(s);
  }
})();
