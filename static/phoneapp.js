// Tab "Softphone": CME Phone (eigene App fuer iPhone, iPad, Mac) - APNs-Schluessel, Local Push, App-Schluessel.
// Eigene Karte; nutzt die Helfer aus index.html ($, esc, api, badge, showMsg). Server: phoneapp.py
(() => {
  let card = document.getElementById("phoneapp-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "phoneapp-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>CME Phone (Softphone)</h2><div class="body" id="pa-body"><div class="empty">Lädt …</div></div>`;
  let reveal = false;

  const ago = (t) => {
    if (!t) return "–";
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 90 ? `vor ${s} s` : s < 5400 ? `vor ${Math.round(s / 60)} min` : new Date(t * 1000).toLocaleString("de-DE");
  };
  const secretTxt = (v) => reveal ? esc(v) : (v ? "•".repeat(12) : '<span class="muted">–</span>');

  async function load() {
    const [j, d] = await Promise.all([api("/api/phoneapp"), api("/api/phoneapp/devices")]);
    const b = $("#pa-body");
    if (!j.ok) { b.innerHTML = `<div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div>`; return; }
    const r = j.relay || {}, a = j.apns || {}, devs = (r.localpush || {}).devices || [];
    const relay = r.online ? badge("ok", "läuft") : badge("bad", "keine Verbindung");
    const apns = a.configured ? badge("ok", `eingerichtet (${a.key_id})`) : badge("bad", "fehlt");
    const lp = r.last_push && r.last_push.time
      ? `${esc(r.last_push.from || "?")} – ${esc(r.last_push.via || "…")} (${ago(r.last_push.time)})` : '<span class="muted">noch keiner</span>';
    b.innerHTML = `
      <div class="hint">Die App meldet sich über das Push-Gateway (Flexisip) an CME an. Schläft die App, weckt sie der push-relay:
        im Heim-WLAN direkt über Local Push, unterwegs über Apple (APNs). Am Router wird jede App als Telefon vom Typ
        <b>CME Phone</b> angelegt (Gerätename <span class="mono">${esc(j.prefix)}&lt;Nebenstelle&gt;</span>, Tab Telefone).</div>

      <h3>Status</h3>
      <div class="kv"><span class="muted">push-relay</span><span>${relay}</span></div>
      <div class="kv"><span class="muted">APNs-Schlüssel</span><span>${apns}</span></div>
      <div class="kv"><span class="muted">Im WLAN verbunden (Local Push)</span><span>${devs.length
        ? devs.map(d => `<span class="mono">${esc(d.peer)}</span> <span class="muted">seit ${ago(d.since)}</span>`).join("<br>")
        : '<span class="muted">kein Gerät</span>'}</span></div>
      <div class="kv"><span class="muted">Letzter Anruf-Push</span><span>${lp}</span></div>
      <div class="kv"><span class="muted">Letzter APNs-Versand</span><span class="mono">${esc((r.apns || {}).last_result || "–")}</span></div>

      <h3>Geräte (Pools vom Typ CME Phone)</h3>
      <div class="hint">Alle Apps derselben Rufnummer (iPhone, iPad, Mac) nutzen einen Pool. CME verlangt von ihnen
        Benutzer und Passwort; der Router speichert das Passwort verschlüsselt, deshalb merkt cme-web es sich und
        gibt es an die Apps weiter. Fehlt es, hier <b>Zugangsdaten setzen</b> – das setzt ein neues Passwort im Pool.</div>
      ${(d && d.devices && d.devices.length) ? d.devices.map(x => `
        <div class="kv"><span><span class="mono">${esc(x.device)}</span> <span class="muted">Pool ${esc(x.tag)}</span></span>
          <span>${x.creds ? badge("ok", "Zugangsdaten hinterlegt") : badge("bad", "Zugangsdaten fehlen")}
            <button class="small" data-act="creds" data-tag="${esc(x.tag)}">${x.creds ? "Neu setzen" : "Zugangsdaten setzen"}</button></span></div>`).join("")
        : '<div class="empty">Noch kein Telefon vom Typ CME Phone (Tab Telefone).</div>'}

      <h3>Einrichtung in der App</h3>
      <div class="hint">iPhone/iPad: <b>Einstellungen → Apps → CME Phone</b>. Mac: Menüleiste → <b>Einstellungen</b>.
        Mehr braucht die App nicht – den Rest (Gateway, CME, Local Push, Voicemail-Nummer) holt sie sich hier ab.</div>
      <div class="kv"><span class="muted">cme-web-Adresse</span><span class="mono">${esc(j.url)}</span></div>
      <div class="kv"><span class="muted">App-Schlüssel</span><span><span class="mono">${secretTxt(j.token)}</span>
        <button class="small" data-act="reveal">${reveal ? "Verbergen" : "Anzeigen"}</button>
        <button class="small" data-act="copy" data-v="${esc(j.token)}">Kopieren</button>
        <button class="small" data-act="renew" data-what="token">Neu erzeugen</button></span></div>
      <div class="kv"><span class="muted">Nebenstelle</span><span>Rufnummer der ersten Leitung des Telefons</span></div>

      <h3>Push-Gateway und Local Push</h3>
      <form id="f-pa-set">
        <div class="row">
          <div><label>Gateway (DNS-Name oder IP von docker01)</label><input name="gateway_host" value="${esc(j.gateway_host)}" placeholder="sip-gw.example.de"></div>
          <div><label>SIP-Port (Flexisip, TCP/UDP)</label><input name="gateway_port" type="number" value="${esc(j.gateway_port)}"></div>
        </div>
        <div class="row">
          <div><label>Local-Push-Port (TCP)</label><input name="localpush_port" type="number" value="${esc(j.localpush_port)}"></div>
          <div><label>WLAN-Namen (SSIDs) für Local Push, kommagetrennt</label><input name="ssids" value="${esc(j.ssids)}" placeholder="Zuhause, Zuhause-5G"></div>
        </div>
        <div class="kv"><span class="muted">Local-Push-Secret</span><span><span class="mono">${secretTxt(j.secret)}</span>
          <button class="small" type="button" data-act="renew" data-what="secret">Neu erzeugen</button></span></div>
        <div class="hint">Die Ports müssen zum Stack passen (<span class="mono">SIP_GW_PORT</span>, <span class="mono">RELAY_LOCALPUSH_PORT</span>).
          Nach einem neuen Secret melden sich die Apps beim nächsten Abruf der Einstellungen neu an.</div>
        <div class="actions"><button class="primary" type="submit">Speichern</button></div>
        <div class="msg" id="m-pa-set"></div>
      </form>

      <h3>APNs (Push über Apple, unterwegs)</h3>
      <form id="f-pa-apns">
        <div class="hint">Apple Developer → <b>Certificates, Identifiers &amp; Profiles → Keys → +</b> → „Apple Push Notifications service (APNs)“,
          Environment <b>Sandbox &amp; Production</b>, Key Restriction <b>Team Scoped</b>. Die <span class="mono">.p8</span>-Datei lässt sich nur einmal herunterladen.
          Topic: <span class="mono">${esc(j.bundle_id)}.voip</span>.</div>
        <div class="row">
          <div><label>Schlüsseldatei (.p8)${a.configured ? " – leer lassen, um nur die Werte zu ändern" : ""}</label><input name="keyfile" type="file" accept=".p8"></div>
          <div><label>Key-ID (aus dem Dateinamen AuthKey_&lt;Key-ID&gt;.p8)</label><input name="key_id" value="${esc(a.key_id)}" maxlength="10" class="mono"></div>
        </div>
        <div class="row">
          <div><label>Team-ID</label><input name="team_id" value="${esc(a.team_id)}" maxlength="10" class="mono"></div>
          <div><label>Umgebung</label><select name="env">
            ${["auto", "sandbox", "production"].map(e => `<option value="${e}" ${a.env === e ? "selected" : ""}>${
              {auto: "automatisch (aus dem Token der App)", sandbox: "Sandbox (aus Xcode installiert)", production: "Production (TestFlight / App Store)"}[e]}</option>`).join("")}
          </select></div>
        </div>
        ${a.uploaded ? `<div class="hint">Gespeichert am ${esc(a.uploaded)}. Der push-relay übernimmt Änderungen sofort.</div>` : ""}
        <div class="actions"><button class="primary" type="submit">Speichern</button>
          ${a.configured ? '<button type="button" class="danger" data-act="del-apns">Schlüssel löschen</button>' : ""}</div>
        <div class="msg" id="m-pa-apns"></div>
      </form>`;
    const f = $("#f-pa-apns");
    f.keyfile.addEventListener("change", () => {
      const m = (f.keyfile.files[0]?.name || "").toUpperCase().match(/([A-Z0-9]{10})\.P8$/);
      if (m) f.key_id.value = m[1];
    });
  }

  card.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    e.preventDefault();
    const act = b.dataset.act;
    if (act === "reveal") { reveal = !reveal; return load(); }
    if (act === "copy") { await navigator.clipboard?.writeText(b.dataset.v); b.textContent = "Kopiert"; return; }
    if (act === "renew") {
      const what = b.dataset.what === "token" ? "einen neuen App-Schlüssel (alle Apps müssen ihn neu eintragen)"
                                             : "ein neues Local-Push-Secret";
      if (!confirm(`Wirklich ${what} erzeugen?`)) return;
      await api("/api/phoneapp/renew", { method: "POST", body: JSON.stringify({ what: b.dataset.what }) });
      reveal = true;
      return load();
    }
    if (act === "creds") {
      if (!confirm(`Neues SIP-Passwort für Pool ${b.dataset.tag} setzen? Die Apps holen es sich beim nächsten Abruf.`)) return;
      b.disabled = true;
      const r = await api(`/api/phoneapp/devices/${b.dataset.tag}/credentials`, { method: "POST", body: "{}" });
      if (!r.ok) { alert(r.error || "Fehler"); b.disabled = false; return; }
      return load();
    }
    if (act === "del-apns") {
      if (!confirm("APNs-Schlüssel löschen? Unterwegs klingeln die Apps dann nicht mehr.")) return;
      await api("/api/phoneapp/apns", { method: "DELETE" });
      return load();
    }
  });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    let j;
    if (f.id === "f-pa-set") {
      j = await api("/api/phoneapp/settings", { method: "POST", body: JSON.stringify({
        gateway_host: f.gateway_host.value, gateway_port: f.gateway_port.value,
        localpush_port: f.localpush_port.value, ssids: f.ssids.value }) });
    } else if (f.id === "f-pa-apns") {
      const fd = new FormData(f);
      if (!f.keyfile.files.length) fd.delete("keyfile");
      const r = await fetch("/api/phoneapp/apns", { method: "POST", body: fd, headers: { "X-CME-Request": "1" } });
      try { j = await r.json(); } catch { j = { ok: false, error: `HTTP ${r.status}` }; }
    }
    btn.disabled = false;
    const m = f.querySelector(".msg");
    if (!j || !j.ok) { showMsg(m, false, (j && j.error) || "Fehler"); return; }
    await load();
    showMsg(document.querySelector(`#${f.id} .msg`), true, "Gespeichert.");
  });

  load();
  // Status auffrischen, solange niemand im Formular arbeitet
  setInterval(() => {
    const busy = card.contains(document.activeElement) && document.activeElement.matches("input, select")
      || [...card.querySelectorAll("input[type=file]")].some(i => i.files.length);
    if (!document.getElementById("tab-softphone")?.hidden && !busy) load();
  }, 15000);
})();
