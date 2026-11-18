// Voicemail (Asterisk): Einrichtung, Postfaecher und Nachrichten.
// Wird von cme_web in index.html eingehaengt und nutzt dessen Helfer ($, esc, badge, api, showMsg).
(() => {
  let V = { enabled: false, mailboxes: [] };
  let player = null;

  const card = document.createElement("section");
  card.className = "card";
  card.id = "voicemail-card";
  card.innerHTML = `<h2>Voicemail <span class="count" id="c-vm"></span>
      <span class="right muted" id="vm-version" style="font-weight:400;font-size:12px"></span></h2>
    <div id="vm-body"><div class="empty">Lädt …</div></div>`;
  const anchor = document.getElementById("raw-out")?.closest("section.card");
  (anchor ? anchor.parentNode.insertBefore(card, anchor) : document.querySelector("main").appendChild(card));

  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
  const fmtDur = (s) => s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min` : `${s} s`;
  const ok = (b, yes, no) => b ? badge("ok", yes) : badge("bad", no);

  function render() {
    const body = $("#vm-body");
    if (!V.enabled) {
      $("#c-vm").textContent = "";
      body.innerHTML = `<div class="body"><div class="hint">Die Voicemail ist noch nicht eingerichtet. Im Portainer-Stack
        die Variablen <span class="mono">VM_HOST_IP</span> (IP des Docker-Hosts, z. B. docker01) und
        <span class="mono">VM_AMI_SECRET</span> (beliebiges Passwort) setzen und neu deployen. Der Container
        <b>cme-voicemail</b> braucht auf dem Host UDP 5060 und 10000–10049.</div></div>`;
      return;
    }
    const st = V.status, cme = V.cme, s = V.settings;
    const total = V.mailboxes.reduce((a, b) => a + b.new, 0);
    $("#c-vm").textContent = total ? `(${total} neu)` : "";
    $("#vm-version").textContent = st.asterisk ? st.version : "";
    const tabN = document.getElementById("n-voicemail");
    if (tabN) tabN.innerHTML = total ? ` ${badge("warn", `${total}`)}` : "";

    const ready = cme.mwi_server && cme.voicemail && cme.dialpeer;
    const active = V.mailboxes.filter(b => b.enabled);
    body.innerHTML = `
      <div class="body">
        <div class="actions" style="flex-wrap:wrap;gap:6px;margin-bottom:10px">
          ${st.asterisk ? badge("ok", "Asterisk läuft") : badge("bad", "Asterisk nicht erreichbar")}
          ${ok(cme.mwi_server, "MWI-Server am Router", "MWI-Server fehlt")}
          ${ok(cme.voicemail, `Nachrichten-Taste → ${s.pilot}`, "Nachrichten-Taste fehlt")}
          ${ok(cme.dialpeer, `Dial-Peer ${cme.dialpeer || ""} → ${V.host_ip}`, "Dial-Peer fehlt")}
          <button class="small${ready ? "" : " primary"}" id="vm-setup">${ready ? "Router-Einstellungen erneut setzen" : "Am Router einrichten"}</button>
        </div>
        ${st.error ? `<div class="msg bad" style="display:block">${esc(st.error)}</div>` : ""}
        ${cme.error ? `<div class="msg bad" style="display:block">${esc(cme.error)}</div>` : ""}
        <form id="f-vmset">
          <div class="row">
            <div><label>Voicemail-Nummer (Abfrage und Umleitungsziel)</label><input name="pilot" value="${esc(s.pilot)}" required></div>
            <div class="row" style="margin:0">
              <div><label>Max. Länge (s)</label><input name="maxsecs" type="number" min="10" max="600" value="${esc(s.maxsecs)}"></div>
              <div><label>Max. Nachrichten</label><input name="maxmsg" type="number" min="1" max="999" value="${esc(s.maxmsg)}"></div>
            </div>
          </div>
          <label class="check"><input type="checkbox" name="skip_pin" ${s.skip_pin ? "checked" : ""}> Vom eigenen Telefon ohne PIN abhören</label>
          <div class="actions"><button type="submit" class="small">Einstellungen speichern</button></div>
          <div class="msg" id="m-vmset"></div>
        </form>
      </div>
      <table><thead><tr><th>Rufnummer</th><th>Postfach</th><th>Umleitung</th><th>Nachrichten</th><th></th></tr></thead>
        <tbody>${V.mailboxes.map(b => {
          const c = b.cme || {};
          const fwd = !b.enabled ? '<span class="muted">–</span>'
            : (c.mwi && c.noan === s.pilot) ? `nach ${esc(c.timeout || b.timeout)} s${c.busy === s.pilot ? ", bei besetzt sofort" : ""}`
            : badge("warn", "am Router nicht gesetzt");
          return `<tr data-row="${esc(b.tag)}"><td><span class="mono">${esc(b.number)}</span> <span class="muted">${esc(b.name)}</span></td>
            <td>${b.enabled ? `${badge("ok", "aktiv")} <span class="muted mono">PIN ${esc(b.pin)}</span>` : '<span class="muted">aus</span>'}</td>
            <td>${fwd}</td>
            <td>${b.enabled ? (b.new ? badge("warn", `${b.new} neu`) : "") + (b.old ? ` <span class="muted">${b.old} gehört</span>` : "") || '<span class="muted">keine</span>' : ""}</td>
            <td><button class="small" data-vm-edit="${esc(b.tag)}">${b.enabled ? "Bearbeiten" : "Einrichten"}</button></td></tr>
            <tr data-edit-row="${esc(b.tag)}" hidden><td colspan="5">
              <form class="f-vmbox" data-tag="${esc(b.tag)}">
                <div class="row">
                  <div><label class="check" style="margin-top:18px"><input type="checkbox" name="enabled" ${b.enabled ? "checked" : ""}> Postfach für ${esc(b.number)}</label></div>
                  <div class="row" style="margin:0">
                    <div><label>PIN (4–10 Ziffern)</label><input name="pin" inputmode="numeric" value="${esc(b.pin || String(Math.floor(1000 + Math.random() * 9000)))}"></div>
                    <div><label>Klingeln bis Voicemail (s)</label><input name="timeout" type="number" min="5" max="120" value="${esc(b.timeout)}"></div>
                  </div>
                </div>
                <label class="check"><input type="checkbox" name="busy" ${b.busy ? "checked" : ""}> Bei besetzt sofort auf die Voicemail</label>
                <div class="actions"><button class="primary small" type="submit">Speichern</button>
                  <button type="button" class="small" data-vm-cancel="${esc(b.tag)}">Abbrechen</button></div>
                <div class="msg"></div>
              </form></td></tr>`;
        }).join("") || `<tr><td colspan="5" class="empty">Keine Rufnummern.</td></tr>`}</tbody></table>
      <h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">Nachrichten</h2>
      ${active.some(b => b.messages.length) ? `<table><thead><tr><th>Postfach</th><th>Von</th><th>Zeit</th><th>Länge</th><th>Anhören</th><th></th></tr></thead><tbody>
        ${active.flatMap(b => b.messages.map(m => {
          const ref = `${b.id}/${m.folder}/${m.file}`;
          return `<tr><td><span class="mono">${esc(b.number)}</span> ${m.folder === "INBOX" ? badge("warn", "neu") : ""}</td>
            <td>${m.caller_name ? esc(m.caller_name) + " " : ""}<span class="mono">${esc(m.caller) || "unbekannt"}</span></td>
            <td>${fmtTime(m.time)}</td><td>${fmtDur(m.duration)}</td>
            <td>${m.audio ? `<button class="small" data-vm-play="${esc(ref)}">▶</button>
              <a class="btn" title="Herunterladen" style="text-decoration:none;padding:2px 8px;font-size:12px" href="/api/voicemail/audio/${esc(ref)}?download=1">⤓</a>` : '<span class="muted">–</span>'}</td>
            <td><div class="actions"><button class="small" data-vm-move="${esc(ref)}">${m.folder === "INBOX" ? "als gehört" : "als neu"}</button>
              <button class="small danger" data-vm-del="${esc(ref)}">Löschen</button></div></td></tr>`;
        })).join("")}</tbody></table>` : `<div class="empty">Keine Nachrichten.</div>`}`;
  }

  async function loadVM() {
    const j = await api("/api/voicemail");
    if (!j.ok) { $("#vm-body").innerHTML = `<div class="empty bad-text">${esc(j.error || "Fehler beim Laden")}</div>`; return; }
    V = j;
    render();
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-vmset") {
      const m = $("#m-vmset");
      const j = await api("/api/voicemail/settings", { method: "PUT", body: JSON.stringify({
        pilot: f.pilot.value, maxsecs: f.maxsecs.value, maxmsg: f.maxmsg.value, skip_pin: f.skip_pin.checked }) });
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await loadVM();
      showMsg($("#m-vmset"), true, "Gespeichert." + (j.config?.length ? `\n\n${j.config.join("\n")}` : ""));
      load(true);
    } else if (f.classList.contains("f-vmbox")) {
      const m = f.querySelector(".msg"), btn = f.querySelector("button[type=submit]");
      btn.disabled = true;
      const j = await api(`/api/voicemail/mailboxes/${f.dataset.tag}`, { method: "PUT", body: JSON.stringify({
        enabled: f.enabled.checked, pin: f.pin.value, timeout: f.timeout.value, busy: f.busy.checked }) });
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      if (j.asterisk_restart) {
        // Neue MWI-Ziele: Asterisk startet kurz neu
        showMsg(m, true, "Gespeichert. Asterisk startet neu …");
        await new Promise(r => setTimeout(r, 6000));
      }
      await loadVM();
      load(true);
    }
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (e.target.id === "vm-setup") {
      if (!confirm(`Am Router einrichten?\n\n• mwi-server ${V.host_ip} unter sip-ua\n• voicemail ${V.settings.pilot} unter voice register global\n` +
                   `• Dial-Peer ${V.settings.pilot} → ${V.host_ip}\n• Umleitungen der aktiven Postfächer`)) return;
      e.target.disabled = true;
      const j = await api("/api/voicemail/cme", { method: "POST" });
      e.target.disabled = false;
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      await loadVM(); load(true);
    } else if (d.vmEdit) {
      card.querySelectorAll("tr[data-edit-row]").forEach(r => r.hidden = r.dataset.editRow !== d.vmEdit || !r.hidden);
    } else if (d.vmCancel) {
      card.querySelector(`tr[data-edit-row="${d.vmCancel}"]`).hidden = true;
    } else if (d.vmPlay) {
      if (player) player.pause();
      player = new Audio(`/api/voicemail/audio/${d.vmPlay}`);
      player.play().catch(() => alert("Abspielen nicht möglich."));
    } else if (d.vmMove || d.vmDel) {
      if (d.vmDel && !confirm("Nachricht löschen?")) return;
      e.target.disabled = true;
      const j = d.vmDel ? await api(`/api/voicemail/messages/${d.vmDel}`, { method: "DELETE" })
                        : await api(`/api/voicemail/messages/${d.vmMove}/move`, { method: "POST" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      loadVM();
    }
  });

  loadVM();
  setInterval(() => { if (!document.hidden && !card.querySelector("tr[data-edit-row]:not([hidden])")) loadVM(); }, 60000);
})();
