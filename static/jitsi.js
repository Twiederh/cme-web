// Tab "Jitsi": Konferenzen in Jitsi Meet über Jigasi, angebunden an Kamailio. Server: jitsi.py
// Nutzt die Helfer aus index.html ($, esc, api, badge, showMsg).
(() => {
  let card = document.getElementById("jitsi-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "jitsi-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Jitsi-Konferenzen (Jigasi)</h2><div class="body" id="jt-body"><div class="empty">Lädt …</div></div>`;
  const CODECS = [["opus/48000", "Opus"], ["G722/8000", "G.722"], ["PCMA/8000", "G.711 A-law"], ["PCMU/8000", "G.711 µ-law"]];
  let J = null, rooms = [];
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const svc = (v) => v === "active" ? badge("ok", "läuft") : badge("bad", v || "?");

  async function load() {
    J = await api("/api/jitsi");
    rooms = (J.settings && J.settings.rooms || []).map(r => ({ ...r }));
    render();
  }

  function roomRows() {
    const dom = J.public_domain;
    return rooms.map((r, i) => `<tr>
      <td><input data-i="${i}" data-k="number" value="${esc(r.number)}" placeholder="z. B. 8888" class="mono"></td>
      <td><input data-i="${i}" data-k="room" value="${esc(r.room)}" placeholder="raumname" class="mono"></td>
      <td><input data-i="${i}" data-k="name" value="${esc(r.name)}" placeholder="${dom ? "leer = nicht von draußen" : "Zugang von außen ist aus"}"
            class="mono" ${dom ? "" : "disabled"}>${r.name && dom ? `<div class="muted mono">${esc(r.name)}@${esc(dom)}</div>` : ""}</td>
      <td><button class="small danger" data-del="${i}">Entfernen</button></td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Noch keine Räume.</td></tr>`;
  }

  function render() {
    const b = $("#jt-body");
    if (!J.ok) { b.innerHTML = `<div class="msg bad" style="display:block">${esc(J.error || "Fehler")}</div>`; return; }
    const s = J.settings, g = J.jigasi;
    const conn = !s.host ? badge("neutral", "nicht eingerichtet") : g ? badge("ok", "verbunden") : badge("bad", "keine Verbindung");
    let jig = "";
    if (g) {
      const pc = g.prosody_cert || {};
      const le = /let's encrypt/i.test(pc.issuer || "");
      jig = `
      <h3>Dienste auf ${esc(s.host)}</h3>
      <div>Jigasi ${svc(g.services.jigasi)} · Prosody ${svc(g.services.prosody)} · Jicofo ${svc(g.services.jicofo)} ·
        Videobridge ${svc(g.services.videobridge)}</div>
      <div style="margin-top:6px">Zertifikat von Prosody (${esc(g.domain)}):
        ${le ? badge("ok", "Let's Encrypt") : badge("warn", pc.issuer ? `selbst signiert (${pc.issuer})` : "unbekannt")}
        <span class="muted">gültig bis ${esc(pc.until || "?")}</span>
        ${pc.hook ? badge("ok", "wird bei Verlängerung übernommen") : ""}
        ${pc.letsencrypt && !(le && pc.hook) ? `<button class="small" id="jt-le">Let's-Encrypt-Zertifikat für Prosody verwenden</button>` : ""}</div>
      <div class="hint">Jigasi meldet sich per XMPP bei Prosody an und prüft dessen Zertifikat. Ein selbst signiertes scheitert
        („unable to find valid certification path“). Die Übernahme startet Prosody, Jicofo, Videobridge und Jigasi neu,
        laufende Konferenzen brechen dabei ab.</div>
      <div class="msg" id="m-jt-le"></div>

      <h3>Jigasi</h3>
      <div>Weg: ${g.via_kamailio ? badge("ok", `über Kamailio (${J.host_ip}:${J.conf_port})`)
        : badge("warn", `${g.server ? `Registrar ${g.server}` : "ohne Registrar"}, Proxy ${g.proxy || "–"}:${g.proxy_port || "–"}`)}
        ${g.via_kamailio ? "" : `<button class="small primary" id="jt-kam">Mit Kamailio verbinden</button>`}</div>
      <form id="f-jt" style="margin-top:10px">
        <div class="row">
          <div><label>SIP-Benutzer (From bei „Telefon einladen“)</label><input name="user_id" value="${esc(g.user_id)}" class="mono"></div>
          <div><label>Standard-Raum (ohne Kopfzeile)</label><input name="default_room" value="${esc((g.default_room || "").split("@")[0])}" class="mono"></div>
        </div>
        <div class="row">
          <div><label>Proxy</label><input name="proxy" value="${esc(g.proxy)}" class="mono" ${g.via_kamailio ? "readonly" : ""}></div>
          <div><label>Proxy-Port / Transport</label><div style="display:flex;gap:6px">
            <input name="proxy_port" value="${esc(g.proxy_port)}" class="mono" style="max-width:110px" ${g.via_kamailio ? "readonly" : ""}>
            <select name="transport" ${g.via_kamailio ? "disabled" : ""}>${["UDP", "TCP", "TLS"].map(t =>
              `<option ${t === g.transport ? "selected" : ""}>${t}</option>`).join("")}</select></div></div>
        </div>
        <label>Codecs (Priorität, höher = bevorzugt, 0 = aus). Die CME hat keinen Transcoder: G.722 und G.711 sollten an sein.</label>
        <div class="row" style="grid-template-columns: repeat(4, 1fr)">${CODECS.map(([k, n]) =>
          `<div><label>${n}</label><input type="number" min="0" max="1000" name="enc:${k}" value="${g.encodings[k] || 0}"></div>`).join("")}</div>
        <label class="check"><input type="checkbox" name="xmpp_login" ${g.xmpp_login ? "checked" : ""}>
          Mit dem Jigasi-Konto (${esc(g.brewery_user || "acc-xmpp-1")}) in den Raum – nötig, wenn Jitsi Token/Anmeldung verlangt
          ${g.auth && g.auth !== "anonymous" ? badge("warn", `Jitsi: authentication = ${g.auth}`) : ""}</label>
        <label class="check"><input type="checkbox" name="trust_all" ${g.trust_all ? "checked" : ""}>
          Zertifikat von Prosody nicht prüfen (nur als Notlösung)</label>
        <button class="primary" type="submit">Speichern und Jigasi neu starten</button>
        <button type="button" id="jt-log">Protokoll</button>
        <div class="msg" id="m-jt"></div>
      </form>
      <pre id="jt-logout" class="mono" style="display:none;max-height:360px;overflow:auto;white-space:pre-wrap;font-size:11.5px"></pre>`;
    }
    const peers = (J.cme_peers || []).map(p => `<tr><td class="mono">${esc(p.tag)}</td><td>${esc(p.desc)}</td>
      <td class="mono">${esc(p.pattern || "–")}</td><td class="mono">${esc(p.target || "–")}</td></tr>`).join("");
    b.innerHTML = `
      <div class="hint">Ein Anruf an die Rufnummer eines Raums geht von der CME über Kamailio (${esc(J.host_ip)}:${J.conf_port}) zu Jigasi.
        Kamailio sagt Jigasi per Kopfzeile <span class="mono">Jitsi-Conference-Room</span>, in welchen Raum. „Telefon einladen“ in Jitsi
        geht den umgekehrten Weg zur CME. Hat ein Raum einen Namen, ist er von draußen als <span class="mono">name@${esc(J.public_domain || "…")}</span> erreichbar.</div>

      <h3>Verbindung ${conn}</h3>
      <form id="f-jt-conn"><div class="row" style="grid-template-columns: 2fr 1fr 80px">
        <div><label>Jitsi-Server (SSH)</label><input name="host" value="${esc(s.host)}" placeholder="192.168.2.111" class="mono"></div>
        <div><label>SSH-Benutzer (sudo ohne Passwort)</label><input name="user" value="${esc(s.user)}" class="mono"></div>
        <div><label>Port</label><input name="port" value="${esc(s.port)}" class="mono"></div></div>
        <button type="submit">Speichern und verbinden</button><div class="msg" id="m-jt-conn"></div></form>
      ${J.error ? `<div class="msg bad" style="display:block">${esc(J.error)}</div>` : ""}
      <details ${g ? "" : "open"} style="margin-top:8px"><summary>SSH-Schlüssel von cme-web</summary>
        <div class="hint">Auf dem Jitsi-Server in <span class="mono">~${esc(s.user)}/.ssh/authorized_keys</span> eintragen:</div>
        <pre class="mono" style="white-space:pre-wrap;word-break:break-all;font-size:11.5px">${esc(J.pubkey)}</pre></details>
      ${jig}

      <h3>Räume</h3>
      <table><thead><tr><th>Rufnummer (CME)</th><th>Raum in Jitsi</th><th>Name von draußen</th><th></th></tr></thead>
        <tbody id="jt-rooms">${roomRows()}</tbody></table>
      <div style="margin:8px 0"><button class="small" id="jt-add">Raum hinzufügen</button></div>
      <div class="row" style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="check"><input type="checkbox" id="jt-dialout" ${s.dialout ? "checked" : ""}> „Telefon einladen“ aus Jitsi erlauben (Dial-Peer für Jigasi)</label>
        <div><label>voice class codec der Dial-Peers</label><input id="jt-codec" value="${esc(s.codec)}" class="mono" style="max-width:90px"></div>
      </div>
      <button class="primary" id="jt-save-rooms">Räume übernehmen (Kamailio und CME)</button>
      <div class="msg" id="m-jt-rooms"></div>
      <pre id="jt-cfg" class="mono" style="display:none;white-space:pre-wrap;font-size:11.5px"></pre>

      <h3>Dial-Peers der CME</h3>
      <table><thead><tr><th>Tag</th><th>Beschreibung</th><th>Muster</th><th>Ziel</th></tr></thead>
        <tbody>${peers || `<tr><td colspan="4" class="empty">Keine.</td></tr>`}</tbody></table>`;
    bind();
  }

  function bind() {
    const b = $("#jt-body");
    $("#f-jt-conn").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const r = await post("/api/jitsi/settings", Object.fromEntries(f));
      showMsg($("#m-jt-conn"), r.ok, r.ok ? "Gespeichert, verbinde …" : r.error);
      if (r.ok) load();
    };
    b.querySelectorAll("#jt-rooms input").forEach(inp => inp.oninput = () => { rooms[+inp.dataset.i][inp.dataset.k] = inp.value.trim(); });
    b.querySelectorAll("[data-del]").forEach(x => x.onclick = () => { rooms.splice(+x.dataset.del, 1); $("#jt-rooms").innerHTML = roomRows(); bind(); });
    $("#jt-add").onclick = () => { rooms.push({ number: "", room: "", name: "" }); $("#jt-rooms").innerHTML = roomRows(); bind(); };
    $("#jt-save-rooms").onclick = async () => {
      const btn = $("#jt-save-rooms"); btn.disabled = true;
      const r = await post("/api/jitsi/rooms", { rooms, dialout: $("#jt-dialout").checked, codec: $("#jt-codec").value });
      btn.disabled = false;
      showMsg($("#m-jt-rooms"), r.ok, r.ok ? "Übernommen. Kamailio lädt die Räume in spätestens 20 s." : r.error);
      const pre = $("#jt-cfg");
      pre.style.display = r.config && r.config.length ? "block" : "none";
      pre.textContent = (r.config || []).join("\n");
      if (r.ok) setTimeout(load, 800);
    };
    const kam = $("#jt-kam");
    if (kam) kam.onclick = async () => {
      if (!confirm("Jigasi auf Kamailio umstellen und neu starten?")) return;
      kam.disabled = true;
      const r = await post("/api/jitsi/jigasi", { kamailio: true });
      showMsg($("#m-jt"), r.ok, r.ok ? "Jigasi läuft jetzt über Kamailio." : r.error);
      if (r.ok) setTimeout(load, 1500); else kam.disabled = false;
    };
    const le = $("#jt-le");
    if (le) le.onclick = async () => {
      if (!confirm("Prosody, Jicofo, Videobridge und Jigasi werden neu gestartet, laufende Konferenzen brechen ab. Weiter?")) return;
      le.disabled = true;
      showMsg($("#m-jt-le"), true, "Läuft …");
      const r = await post("/api/jitsi/prosody-cert", {});
      showMsg($("#m-jt-le"), r.ok, r.ok ? "Prosody nutzt jetzt das Let's-Encrypt-Zertifikat." : r.error);
      if (r.ok) setTimeout(load, 4000); else le.disabled = false;
    };
    const f = $("#f-jt");
    if (f) {
      f.onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(f), body = { encodings: {} };
        for (const [k, v] of fd.entries()) {
          if (k.startsWith("enc:")) body.encodings[k.slice(4)] = +v || 0;
          else if (k !== "xmpp_login" && k !== "trust_all") body[k] = v;
        }
        if (J.jigasi.via_kamailio) { delete body.proxy; delete body.proxy_port; delete body.transport; }
        body.xmpp_login = f.xmpp_login.checked;
        body.trust_all = f.trust_all.checked;
        const r = await post("/api/jitsi/jigasi", body);
        showMsg($("#m-jt"), r.ok, r.ok ? (r.changed ? "Gespeichert, Jigasi startet neu." : "Keine Änderung.") : r.error);
        if (r.ok && r.changed) setTimeout(load, 1500);
      };
      $("#jt-log").onclick = async () => {
        const pre = $("#jt-logout");
        if (pre.style.display === "block") { pre.style.display = "none"; return; }
        const r = await api("/api/jitsi/log?n=120");
        pre.style.display = "block";
        pre.textContent = r.ok ? r.log : r.error;
        pre.scrollTop = pre.scrollHeight;
      };
    }
  }

  // erst laden, wenn der Tab geöffnet wird (SSH dauert)
  let loaded = false;
  const maybe = () => { if (!loaded && location.hash === "#jitsi") { loaded = true; load(); } };
  window.addEventListener("hashchange", maybe);
  document.addEventListener("click", (e) => { if (e.target.closest("button[data-tab='jitsi']")) setTimeout(maybe, 0); });
  maybe();
})();
