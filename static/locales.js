// Sprachpakete fuer die Telefone: CME-locale-…tar in den Flash laden und mit dem Locale-Installer des Routers
// installieren (voice register global / user-locale <tag> DE load …). Karte im Tab "Telefone & Rufnummern".
// Nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let L = { tars: [], locales: [], job: {} };
  let poll = null;

  let card = document.getElementById("locale-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "locale-card";
    const after = document.getElementById("phonecfg-card") || document.getElementById("firmware-card");
    if (after) after.after(card); else document.querySelector("main").appendChild(card);
  }
  const tagSel = (name) => `<select name="${name}" style="width:auto">${[0, 1, 2, 3, 4].map(t =>
    `<option value="${t}">${t}${t === 0 ? " (Standard für alle)" : ""}</option>`).join("")}</select>`;
  card.innerHTML = `
    <h2>Sprache der Telefone <span class="count" id="c-loc"></span>
      <span class="right muted" style="font-weight:400;font-size:12px">user-locale … load</span></h2>
    <table><thead><tr><th>Eingestellt</th><th>Tag</th><th>Sprache</th><th>Paket</th><th></th></tr></thead>
      <tbody id="t-locset"></tbody></table>
    <h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">Pakete im Flash</h2>
    <table><thead><tr><th>Datei</th><th>Größe</th><th></th></tr></thead><tbody id="t-loctar"></tbody></table>
    <div class="body" style="border-top:1px solid var(--line)">
      <form id="f-locup">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Sprachpaket hochladen und installieren</label>
        <div class="hint">Das TAR-Paket von Cisco, z. B. <span class="mono">CME-locale-de_DE-German-8.6.3.0.tar</span>. Es kommt unverändert
          nach <span class="mono" id="loc-dir"></span> und wird mit <span class="mono">user-locale &lt;Tag&gt; DE load …</span> unter
          <span class="mono">voice register global</span> installiert; der Router packt es selbst aus.</div>
        <div class="row">
          <div><input type="file" name="file" accept=".tar" required></div>
          <div><label>Tag</label>${tagSel("tag")}</div>
        </div>
        <label class="check"><input type="checkbox" name="install" checked> gleich installieren</label>
        <label class="check"><input type="checkbox" name="network" checked> auch die Töne (network-locale) umstellen</label>
        <label class="check"><input type="checkbox" name="reset"> alle Telefone danach neu starten</label>
        <div class="actions"><button type="submit" class="primary small">Hochladen</button></div>
        <div id="loc-job" style="margin-top:8px"></div>
        <div class="msg" id="m-locup"></div>
      </form>
    </div>`;

  const mb = (b) => b == null ? "?" : `${(b / 1048576).toFixed(b < 10485760 ? 1 : 0)} MB`;

  function renderJob() {
    const j = L.job || {};
    const box = $("#loc-job");
    if (!j.state) { box.innerHTML = ""; return; }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    const head = j.state === "running" ? badge("warn", "läuft") : j.state === "done" ? badge("ok", "fertig") : badge("bad", "Fehler");
    box.innerHTML = `<div>${head} <span class="mono">${esc(j.file || "")}</span> – ${esc(j.step || "")}</div>
      ${j.state === "running" && j.total ? `<div style="height:6px;background:var(--line);border-radius:3px;margin-top:6px">
        <div style="height:6px;width:${pct}%;background:var(--accent);border-radius:3px"></div></div>
        <div class="muted" style="font-size:12px">${mb(j.done)} von ${mb(j.total)} (${pct} %)</div>` : ""}
      ${j.note ? `<div class="muted mono" style="font-size:12px;white-space:pre-wrap">${esc(j.note)}</div>` : ""}
      ${j.error ? `<div class="msg bad" style="display:block">${esc(j.error)}</div>` : ""}`;
  }

  function render() {
    $("#loc-dir").textContent = L.dir || "";
    $("#c-loc").textContent = L.locales.length ? `(${L.locales.filter(l => l.kind === "user").map(l => l.code).join(", ")})` : "";
    $("#t-locset").innerHTML = L.locales.map(l => `<tr>
        <td>${l.kind === "user" ? "Menüsprache" : "Töne"}</td><td>${esc(l.tag)}</td><td>${esc(l.code)}</td>
        <td class="mono" style="font-size:12px">${esc(l.load)}</td>
        <td><button class="small" data-loc-rm="${esc(l.kind)}:${esc(l.tag)}">Entfernen</button></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">Standard (Englisch / USA).</td></tr>`;
    $("#t-loctar").innerHTML = L.tars.map(t => `<tr>
        <td class="mono">${esc(t.file)} ${t.used ? badge("ok", "installiert") : ""}</td><td>${mb(t.size)}</td>
        <td><div class="actions">${tagSel("tag-" + esc(t.file))}
          <button class="small" data-loc-inst="${esc(t.file)}">Installieren</button>
          ${t.used ? "" : `<button class="small danger" data-loc-del="${esc(t.file)}">Löschen</button>`}</div></td></tr>`).join("")
      || `<tr><td colspan="3" class="empty">Kein Sprachpaket im Flash.</td></tr>`;
    renderJob();
  }

  async function load() {
    const j = await api("/api/locales");
    if (!j.ok) { $("#t-locset").innerHTML = `<tr><td colspan="5" class="empty">${esc(j.error || "Fehler")}</td></tr>`; return; }
    L = j;
    render();
    if (L.job?.state === "running") startPoll();
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      const j = await api("/api/locales/job");
      if (!j.ok) return;
      L.job = j.job;
      renderJob();
      if (j.job.state !== "running") { clearInterval(poll); poll = null; load(); }
    }, 1500);
  }

  card.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-locup") return;
    const m = $("#m-locup"), btn = f.querySelector("button[type=submit]");
    const file = f.file.files[0];
    if (!file) return;
    btn.disabled = true;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/locales/upload");
    xhr.setRequestHeader("X-CME-Request", "1");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        L.job = { state: "running", file: file.name, step: "Übertrage zum Server …", total: ev.total, done: ev.loaded };
        renderJob();
      }
    };
    xhr.onload = () => {
      btn.disabled = false;
      let j; try { j = JSON.parse(xhr.responseText); } catch { j = { ok: false, error: `HTTP ${xhr.status}` }; }
      if (xhr.status === 413) j = { ok: false, error: "Datei zu groß." };
      if (!j.ok) { L.job = {}; renderJob(); showMsg(m, false, j.error || "Fehler"); return; }
      f.file.value = "";
      L.job = j.job;
      renderJob();
      startPoll();
    };
    xhr.onerror = () => { btn.disabled = false; showMsg(m, false, "Übertragung abgebrochen."); };
    const fd = new FormData();
    fd.append("file", file);
    fd.append("tag", f.tag.value);
    for (const k of ["install", "network", "reset"]) fd.append(k, f[k].checked ? "1" : "0");
    xhr.send(fd);
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    const m = $("#m-locup");
    if (d.locInst) {
      const tag = card.querySelector(`select[name="tag-${CSS.escape(d.locInst)}"]`).value;
      const reset = confirm("Alle Telefone danach neu starten? (OK = ja, Abbrechen = nur installieren)");
      e.target.disabled = true;
      const j = await api("/api/locales/install", { method: "POST",
        body: JSON.stringify({ file: d.locInst, tag, network: true, reset }) });
      e.target.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await load();
      showMsg(m, true, `Installiert.${j.note ? "\n\n" + j.note : ""}${j.config?.length ? "\n\n" + j.config.join("\n") : ""}`);
    } else if (d.locRm) {
      const [kind, tag] = d.locRm.split(":");
      if (!confirm(`${kind === "user" ? "Menüsprache" : "Töne"} für Tag ${tag} auf Standard (Englisch/USA) zurückstellen?`)) return;
      const reset = confirm("Alle Telefone danach neu starten?");
      const j = await api("/api/locales/remove", { method: "POST", body: JSON.stringify({ kind, tag, reset }) });
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      load();
    } else if (d.locDel) {
      if (!confirm(`${d.locDel} aus dem Flash löschen?`)) return;
      const j = await api(`/api/locales/${encodeURIComponent(d.locDel)}`, { method: "DELETE" });
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      load();
    }
  });

  load();
})();
