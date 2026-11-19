// Visual Voicemail am Telefon (XML-Dienst aus vvm.py): Adresse anzeigen und in die Telefone eintragen.
// Eigene Karte im Tab "Voicemail"; nutzt die Helfer aus index.html ($, esc, api, showMsg).
(() => {
  let card = document.getElementById("vvm-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "vvm-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Visual Voicemail am Telefon</h2><div class="body" id="vvm-body"><div class="empty">Lädt …</div></div>`;

  async function load() {
    const j = await api("/api/vvm");
    const b = $("#vvm-body");
    if (!j.ok) { b.innerHTML = `<div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div>`; return; }
    if (!j.enabled) { b.innerHTML = `<div class="hint">Erst die Voicemail einrichten (VM_HOST_IP und Asterisk).</div>`; return; }
    const files = Object.entries(j.state || {});
    const svc = files.filter(([, s]) => s["device/servicesURL[1]"] === j.url).length;
    const msg = files.filter(([, s]) => s["device/messagesURL[1]"] === j.url).length;
    b.innerHTML = `
      <div class="hint">Ein Telefondienst zeigt die Nachrichten der eigenen Mailboxen als Liste: Anrufer, Zeit, Dauer und ob sie neu ist.
        Am Telefon kannst du sie abspielen (über Hörer oder Lautsprecher), zurückrufen, löschen und als gehört oder neu markieren.
        Das Telefon wird an seiner IP-Adresse erkannt und sieht nur die Mailboxen seiner eigenen Leitungen.</div>
      <div class="kv" style="margin:10px 0"><span class="muted">Adresse des Dienstes</span>
        <span class="mono">${esc(j.url)}</span></div>
      <div class="hint">Die Telefone erreichen den Dienst über Port <b>${esc(j.port)}</b> des Docker-Hosts (unverschlüsselt, ohne Anmeldung, nur für registrierte Telefone).
        Das Abspielen schickt den Ton per RTP an Port ${esc(j.rtp_port)} des Telefons.</div>
      <form id="f-vvm">
        <label class="check"><input type="checkbox" name="services" ${svc ? "checked" : ""}> Taste <b>Dienste/Anwendungen</b> öffnet die Visual Voicemail
          <span class="muted">(${svc}/${files.length} Telefone)</span></label>
        <label class="check"><input type="checkbox" name="messages" ${msg ? "checked" : ""}> Taste <b>Nachrichten</b> öffnet die Visual Voicemail statt die Mailbox anzurufen
          <span class="muted">(${msg}/${files.length} Telefone)</span></label>
        <label class="check"><input type="checkbox" name="restart" checked> Geänderte Telefone danach neu starten</label>
        <div class="hint">Wird als Anpassung in der Telefon-Konfiguration gespeichert (<span class="mono">servicesURL</span> bzw.
          <span class="mono">messagesURL</span>) und bleibt nach „create profile“ erhalten.
          Die Taste <b>Dienste</b> zeigt danach nur noch die Visual Voicemail statt anderer Dienste.</div>
        <div class="actions"><button class="primary" type="submit">Übernehmen</button></div>
        <div class="msg" id="m-vvm"></div>
      </form>`;
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-vvm") return;
    const btn = f.querySelector("button[type=submit]"), m = $("#m-vvm");
    btn.disabled = true;
    const j = await api("/api/vvm/install", { method: "POST", body: JSON.stringify({
      services: f.services.checked, messages: f.messages.checked, restart: f.restart.checked }) });
    btn.disabled = false;
    if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
    await load();
    showMsg($("#m-vvm"), !j.errors.length,
      `${j.files.length} Telefon-Konfiguration(en) aktualisiert.` +
      (j.restarted.length ? ` Neu gestartet: Pool ${j.restarted.join(", ")}.` : "") +
      (j.errors.length ? `\n${j.errors.join("\n")}` : ""));
    window.loadPhoneCfg?.();
  });

  window.loadVvm = load;
  load();
})();
