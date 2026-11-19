// Telefon-Firmware: Pakete hochladen, Telefontypen zuweisen, alte Staende loeschen.
// Eigene Karte im Tab "Telefone & Rufnummern"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let F = { firmware: [], types: [], job: {} };
  let poll = null;

  // Die Karte legt tabs.js an (damit sie beim Aufbau der Tabs schon da ist)
  let card = document.getElementById("firmware-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "firmware-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `
    <h2>Telefon-Firmware <span class="count" id="c-fw"></span>
      <span class="right muted" id="fw-free" style="font-weight:400;font-size:12px"></span></h2>
    <table><thead><tr><th>Telefontyp</th><th>Telefone</th><th>Zugewiesene Firmware</th><th>Neue Firmware</th><th></th></tr></thead>
      <tbody id="t-fwtypes"></tbody></table>
    <h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">Am Router vorhanden</h2>
    <table><thead><tr><th>Firmware</th><th>Für</th><th>Ablage</th><th>Zugewiesen</th><th></th></tr></thead>
      <tbody id="t-fw"></tbody></table>
    <div class="body" style="border-top:1px solid var(--line)">
      <form id="f-fwup">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Firmware hochladen</label>
        <div class="hint">Das ZIP-Paket von Cisco für Fremdsysteme/CME, z. B. <span class="mono">cmterm-8845_65-sip.14-3-1-0001-136.zip</span>
          (nicht die <span class="mono">.cop.sha512</span>-Datei für CUCM). Alle Dateien kommen per SCP in den Flash des Routers
          und bekommen einen tftp-server-Eintrag. Das dauert einige Minuten; die Seite kann solange offen bleiben oder neu geladen werden.</div>
        <div class="row">
          <div><input type="file" name="file" accept=".zip" required></div>
          <div class="actions" style="margin:0"><button type="submit" class="primary small">Hochladen</button></div>
        </div>
        <div id="fw-job" style="margin-top:8px"></div>
        <div class="msg" id="m-fwup"></div>
      </form>
    </div>`;

  const mb = (b) => b == null ? "?" : `${(b / 1048576).toFixed(b < 10485760 ? 1 : 0)} MB`;

  function renderJob() {
    const j = F.job || {};
    const box = $("#fw-job");
    if (!j.state) { box.innerHTML = ""; return; }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    const head = j.state === "running" ? badge("warn", "läuft") : j.state === "done" ? badge("ok", "fertig") : badge("bad", "Fehler");
    box.innerHTML = `<div>${head} <span class="mono">${esc(j.file || "")}</span> – ${esc(j.step || "")}</div>
      ${j.state === "running" && j.total ? `<div style="height:6px;background:var(--line);border-radius:3px;margin-top:6px">
        <div style="height:6px;width:${pct}%;background:var(--accent);border-radius:3px"></div></div>
        <div class="muted" style="font-size:12px">${mb(j.done)} von ${mb(j.total)} (${pct} %)</div>` : ""}
      ${j.error ? `<div class="msg bad" style="display:block">${esc(j.error)}</div>` : ""}`;
  }

  function render() {
    $("#c-fw").textContent = F.firmware.length ? `(${F.firmware.length})` : "";
    $("#fw-free").textContent = F.free != null ? `${mb(F.free)} frei im Flash` : "";
    $("#t-fwtypes").innerHTML = F.types.map(t => {
      const phones = Object.entries(t.phone_loads || {});
      const choices = [...new Set([...(t.choices || []), ...(t.load ? [t.load] : [])])];
      const opts = choices.map(l =>
        `<option value="${esc(l)}" ${l === t.load ? "selected" : ""}>${esc(l)}</option>`).join("");
      return `<tr><td><b>${esc(TYPE_LABEL?.[t.type] ?? t.type)}</b></td>
        <td>${t.pools.length ? `${t.pools.length} <span class="muted mono">(Pool ${t.pools.map(esc).join(", ")})</span>` : '<span class="muted">keine</span>'}
          ${phones.length ? `<br><span class="muted" style="font-size:12px">läuft: ${phones.map(([p, v]) => `${esc(p)}: ${esc(v)}`).join(", ")}</span>` : ""}</td>
        <td>${t.load ? `<span class="mono">${esc(t.load)}</span>${t.load_ok ? "" : " " + badge("bad", "Dateien fehlen")}` : '<span class="muted">Standard des Telefons</span>'}</td>
        <td><select data-fw-sel="${esc(t.type)}"><option value="">– keine (Zuweisung entfernen) –</option>${opts}</select>
          <label class="check" style="margin-top:4px"><input type="checkbox" data-fw-reset="${esc(t.type)}" ${t.pools.length ? "checked" : "disabled"}> Telefone neu starten</label></td>
        <td><button class="small" data-fw-assign="${esc(t.type)}">Zuweisen</button></td></tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">Keine Telefone mit Typ angelegt.</td></tr>`;
    $("#t-fw").innerHTML = F.firmware.map(f => `<tr>
        <td class="mono">${esc(f.load)}</td>
        <td>${f.types.length ? f.types.map(t => esc(TYPE_LABEL?.[t] ?? t)).join(", ") : '<span class="muted">?</span>'}</td>
        <td class="mono" style="font-size:12px">${esc(f.dir)}${f.files ? ` <span class="muted">(${f.files} Dateien)</span>` : ""}
          ${f.own ? "" : `<br><span class="muted">manuell installiert</span>`}</td>
        <td>${f.used_by.length ? f.used_by.map(t => badge("ok", TYPE_LABEL?.[t] ?? t)).join(" ") : '<span class="muted">–</span>'}</td>
        <td>${f.own && !f.used_by.length ? `<button class="small danger" data-fw-del="${esc(f.load)}">Löschen</button>` : ""}</td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">Keine Firmware mit tftp-server-Eintrag gefunden.</td></tr>`;
    renderJob();
  }

  async function load() {
    const j = await api("/api/firmware");
    if (!j.ok) { $("#t-fwtypes").innerHTML = `<tr><td colspan="5" class="empty bad-text">${esc(j.error || "Fehler beim Laden")}</td></tr>`; return; }
    F = j;
    render();
    if (F.job?.state === "running") startPoll();
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      const j = await api("/api/firmware/job");
      if (!j.ok) return;
      F.job = j.job;
      renderJob();
      if (j.job.state !== "running") {
        clearInterval(poll); poll = null;
        load();
      }
    }, 2000);
  }

  card.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-fwup") return;
    const m = $("#m-fwup"), btn = f.querySelector("button[type=submit]");
    const file = f.file.files[0];
    if (!file) return;
    btn.disabled = true;
    // XMLHttpRequest statt fetch: zeigt den Fortschritt des Hochladens zum Server
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/firmware/upload");
    xhr.setRequestHeader("X-CME-Request", "1");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        F.job = { state: "running", file: file.name, step: "Übertrage zum Server …", total: ev.total, done: ev.loaded };
        renderJob();
      }
    };
    xhr.onload = () => {
      btn.disabled = false;
      let j; try { j = JSON.parse(xhr.responseText); } catch { j = { ok: false, error: `HTTP ${xhr.status}` }; }
      if (xhr.status === 413) j = { ok: false, error: "Datei zu groß." };
      if (!j.ok) { F.job = {}; renderJob(); showMsg(m, false, j.error || "Fehler"); return; }
      f.reset();
      F.job = j.job;
      renderJob();
      startPoll();
    };
    xhr.onerror = () => { btn.disabled = false; showMsg(m, false, "Übertragung abgebrochen."); };
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.fwAssign) {
      const t = d.fwAssign;
      const loadName = card.querySelector(`select[data-fw-sel="${t}"]`).value;
      const reset = card.querySelector(`input[data-fw-reset="${t}"]`).checked;
      const cur = F.types.find(x => x.type === t);
      const txt = loadName ? `Firmware ${loadName} für ${TYPE_LABEL?.[t] ?? t} zuweisen?` : `Zuweisung für ${TYPE_LABEL?.[t] ?? t} entfernen?`;
      if (!confirm(txt + (reset && cur?.pools.length ? `\n\n${cur.pools.length} Telefon(e) starten neu und laden die Firmware (einige Minuten).` : ""))) return;
      e.target.disabled = true;
      const j = await api("/api/firmware/assign", { method: "POST", body: JSON.stringify({ type: t, load: loadName, reset }) });
      e.target.disabled = false;
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (d.fwDel) {
      if (!confirm(`Firmware ${d.fwDel} mit allen Dateien aus dem Flash löschen?`)) return;
      e.target.disabled = true;
      const j = await api(`/api/firmware/${encodeURIComponent(d.fwDel)}`, { method: "DELETE" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      load();
    }
  });

  window.loadFirmware = load;
  load();
})();
