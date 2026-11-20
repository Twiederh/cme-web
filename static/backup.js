// Sicherung und Wiederherstellung (Tab "Sicherung"): CME-Konfiguration samt Dateien sichern und auf einen
// anderen Router uebertragen. Nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let card = document.getElementById("backup-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "backup-card";
    document.querySelector("main").appendChild(card);
  }
  let R = null, poll = null;
  const mb = (b) => b == null ? "?" : `${(b / 1048576).toFixed(b < 10485760 ? 1 : 0)} MB`;

  card.innerHTML = `
    <h2>Sicherung &amp; Umzug auf einen neuen Router</h2>
    <div class="body">
      <form id="f-bk">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Sicherung erstellen</label>
        <div class="hint">Sichert die CME-Teile der running-config (voice, dial-peer, sip-ua, telephony-service, voice register,
          Übersetzungen, tftp-server …) als ZIP. Mit „Dateien“ kommen alle Dateien aus dem Flash dazu, auf die diese Konfiguration
          verweist (Firmware, Klingeltöne, Hintergründe, Sprachpakete) – das kann einige hundert MB groß werden und dauert.
          Daten von cme-web selbst (Voicemail, Fax, Benutzer, Telefonbuch) liegen im Docker-Volume und sind hier nicht enthalten.</div>
        <label class="check"><input type="checkbox" name="files" checked> Dateien aus dem Flash mitnehmen</label>
        <div class="actions"><button class="primary small" type="submit">Sicherung erstellen</button></div>
      </form>
    </div>
    <div class="body" style="border-top:1px solid var(--line)">
      <form id="f-rs">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Sicherung auf diesen Router übertragen</label>
        <div class="hint">Erst hochladen – danach zeigt cme-web, was übertragen wird, und welche IP-Adressen, Speicherorte und
          Schnittstellen auf dem neuen Router anders sein müssen.</div>
        <div class="row">
          <div><input type="file" name="file" accept=".zip" required></div>
          <div class="actions" style="margin:0"><button class="small" type="submit">Hochladen und prüfen</button></div>
        </div>
        <div class="msg" id="m-rs"></div>
      </form>
      <div id="rs-preview"></div>
    </div>
    <div class="body" style="border-top:1px solid var(--line)"><div id="bk-job"></div></div>`;

  function renderJob(j) {
    const box = $("#bk-job");
    if (!j || !j.state) { box.innerHTML = '<span class="muted">Keine laufende Sicherung.</span>'; return; }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    const head = j.state === "running" ? badge("warn", "läuft") : j.state === "done" ? badge("ok", "fertig")
      : j.state === "warn" ? badge("warn", "mit Fehlern") : badge("bad", "Fehler");
    box.innerHTML = `<div>${head} <b>${j.kind === "backup" ? "Sicherung" : "Wiederherstellung"}</b> – ${esc(j.step || "")}</div>
      ${j.state === "running" && j.total ? `<div style="height:6px;background:var(--line);border-radius:3px;margin-top:6px">
        <div style="height:6px;width:${pct}%;background:var(--accent);border-radius:3px"></div></div>
        <div class="muted" style="font-size:12px">${mb(j.done)} von ${mb(j.total)} (${pct} %)</div>` : ""}
      ${j.download ? `<div class="actions" style="margin-top:8px"><a class="btn primary small" href="/api/backup/download/${esc(j.download)}">
        ${esc(j.name || "Sicherung")} herunterladen (${mb(j.size)})</a></div>` : ""}
      ${j.missing?.length ? `<div class="hint">Nicht im Flash gefunden (nicht gesichert): ${j.missing.map(esc).join(", ")}</div>` : ""}
      ${j.saved ? `<div class="hint">Konfiguration gespeichert (write memory).</div>` : ""}
      ${j.errors?.length ? `<div class="msg bad" style="display:block;white-space:pre-wrap">${j.errors.map(esc).join("\n\n")}</div>` : ""}
      ${j.error ? `<div class="msg bad" style="display:block">${esc(j.error)}</div>` : ""}`;
  }

  async function loadJob() {
    const j = await api("/api/backup/job");
    if (!j.ok) return;
    renderJob(j.job);
    if (j.job.state === "running") startPoll();
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      const j = await api("/api/backup/job");
      if (!j.ok) return;
      renderJob(j.job);
      if (j.job.state !== "running") { clearInterval(poll); poll = null; }
    }, 1500);
  }

  function renderPreview() {
    const m = R.manifest;
    const label = { ip: "IP-Adresse", fs: "Speicherort", if: "Schnittstelle" };
    $("#rs-preview").innerHTML = `
      <div class="hint" style="margin-top:12px">Sicherung von <b>${esc(m.hostname || "?")}</b> (${esc(m.version || "")}) vom ${esc(m.created)}:
        ${R.lines} Konfigurationszeilen, ${m.files.length} Dateien (${mb(m.files.reduce((a, f) => a + f.size, 0))}).
        Dieser Router: <b>${esc(R.hostname_here || "?")}</b>.</div>
      <details><summary class="muted">Inhalt</summary>
        <table><tbody>${R.summary.map(([k, n]) => `<tr><td class="mono">${esc(k)}</td><td>${n}</td></tr>`).join("")}</tbody></table>
        <pre class="mono" style="max-height:300px;overflow:auto;font-size:12px">${esc(R.preview)}</pre></details>
      ${R.type6 ? `<div class="msg bad" style="display:block">${R.type6} Passwort(e) sind mit „password 6“ verschlüsselt (z. B. SIP-Zugang
        beim Provider, Autoregistrierung). Die funktionieren auf dem neuen Router nur mit demselben Hauptschlüssel
        (<span class="mono">key config-key password-encrypt …</span> und <span class="mono">password encryption aes</span>) –
        sonst nach dem Übertragen dort neu eingeben.</div>` : ""}
      <form id="f-rsrun">
        <table style="margin-top:8px"><thead><tr><th>Was</th><th>Alt</th><th>Neu</th><th>Vorkommen</th></tr></thead><tbody>
          ${R.replace.map((r, i) => `<tr><td>${label[r.kind]}</td><td class="mono">${esc(r.old)}</td>
            <td><input class="mono" data-rep="${i}" value="${esc(r.new)}" style="width:180px"></td>
            <td class="muted mono" style="font-size:11px">${r.where.map(esc).join("<br>")}</td></tr>`).join("")
            || '<tr><td colspan="4" class="empty">Nichts zu ersetzen.</td></tr>'}
        </tbody></table>
        <label class="check"><input type="checkbox" name="files" ${m.files.length ? "checked" : "disabled"}> Dateien in den Flash übertragen</label>
        <label class="check"><input type="checkbox" name="config" checked> Konfiguration einspielen</label>
        <label class="check"><input type="checkbox" name="save"> danach speichern (write memory), wenn alles ohne Fehler durchlief</label>
        <div class="actions"><button class="primary" type="submit">Auf diesen Router übertragen</button></div>
        <div class="msg" id="m-rsrun"></div>
      </form>`;
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-bk") {
      const j = await api("/api/backup", { method: "POST", body: JSON.stringify({ files: f.files.checked }) });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      renderJob(j.job);
      startPoll();
    } else if (f.id === "f-rs") {
      const file = f.file.files[0];
      if (!file) return;
      const m = $("#m-rs"), btn = f.querySelector("button[type=submit]");
      btn.disabled = true;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/restore/upload");
      xhr.setRequestHeader("X-CME-Request", "1");
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) showMsg(m, true, `Übertrage … ${Math.round(100 * ev.loaded / ev.total)} %`);
      };
      xhr.onload = () => {
        btn.disabled = false;
        let j; try { j = JSON.parse(xhr.responseText); } catch { j = { ok: false, error: `HTTP ${xhr.status}` }; }
        if (xhr.status === 413) j = { ok: false, error: "Datei zu groß." };
        if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
        showMsg(m, true, "Sicherung geprüft.");
        R = j;
        renderPreview();
      };
      xhr.onerror = () => { btn.disabled = false; showMsg(m, false, "Übertragung abgebrochen."); };
      const fd = new FormData();
      fd.append("file", file);
      xhr.send(fd);
    } else if (f.id === "f-rsrun") {
      const replace = R.replace.map((r, i) => ({ ...r, new: f.querySelector(`[data-rep="${i}"]`).value.trim() }));
      if (!confirm(`Konfiguration${f.files.checked ? " und Dateien" : ""} jetzt auf diesen Router (${R.hostname_here}) übertragen?\n\n` +
                   "Vorhandene Einträge mit gleicher Nummer werden überschrieben.")) return;
      const j = await api("/api/restore/run", { method: "POST", body: JSON.stringify({ id: R.id, replace,
        files: f.files.checked, config: f.config.checked, save: f.save.checked }) });
      if (!j.ok) { showMsg($("#m-rsrun"), false, j.error || "Fehler"); return; }
      renderJob(j.job);
      startPoll();
    }
  });

  loadJob();
})();
