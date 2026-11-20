// Sprachpakete fuer die Telefone: Locale-Paket (CME-locale-…tar) hochladen, Sprache und Toene einschalten.
// Karte im Tab "Telefone & Rufnummern" hinter der Firmware; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let L = { locales: [], current: {}, job: {} };
  let poll = null;

  let card = document.getElementById("locale-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "locale-card";
    const after = document.getElementById("phonecfg-card") || document.getElementById("firmware-card");
    if (after) after.after(card); else document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `
    <h2>Sprache der Telefone <span class="count" id="c-loc"></span>
      <span class="right muted" style="font-weight:400;font-size:12px">user-locale / network-locale</span></h2>
    <div class="body">
      <form id="f-locset">
        <div class="row">
          <div><label>Menüsprache (user-locale)</label><select name="user" id="loc-user"></select></div>
          <div><label>Töne (network-locale: Wählton, Freiton, Besetztton)</label><select name="network" id="loc-net"></select></div>
        </div>
        <label class="check"><input type="checkbox" name="reset" checked> Alle Telefone danach neu starten (nötig, damit die Sprache wechselt)</label>
        <div class="actions"><button type="submit" class="primary small">Am Router übernehmen</button></div>
        <div class="msg" id="m-locset"></div>
      </form>
    </div>
    <table><thead><tr><th>Paket am Router</th><th>Ordner</th><th>Dateien</th><th></th></tr></thead>
      <tbody id="t-loc"></tbody></table>
    <div class="body" style="border-top:1px solid var(--line)">
      <form id="f-locup">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Sprachpaket hochladen</label>
        <div class="hint">Das TAR-Paket von Cisco, z. B. <span class="mono">CME-locale-de_DE-German_Germany-14.1.14.1.tar</span>.
          Die Dateien kommen per SCP in den Flash des Routers und bekommen tftp-server-Einträge
          (z. B. <span class="mono">German_Germany/…</span> und <span class="mono">Germany/…-tones.xml</span>).
          Danach oben die Sprache auswählen.</div>
        <div class="row">
          <div><input type="file" name="file" accept=".tar" required></div>
          <div class="actions" style="margin:0"><button type="submit" class="primary small">Hochladen</button></div>
        </div>
        <div id="loc-job" style="margin-top:8px"></div>
        <div class="msg" id="m-locup"></div>
      </form>
    </div>`;

  const mb = (b) => b == null ? "?" : `${(b / 1048576).toFixed(b < 10485760 ? 1 : 0)} MB`;
  const CODES = { Germany: "DE", Austria: "AT", Switzerland: "CH", France: "FR", Italy: "IT", Spain: "ES",
                  Netherlands: "NL", United_Kingdom: "GB", United_States: "US" };
  const codeOf = (name) => CODES[name.split("_").slice(1).join("_")] || "";

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
      ${j.error ? `<div class="msg bad" style="display:block">${esc(j.error)}</div>` : ""}`;
  }

  function render() {
    $("#c-loc").textContent = L.locales.length ? `(${L.locales.length})` : "";
    const codes = [...new Set(L.locales.map(l => codeOf(l.name)).filter(Boolean))];
    for (const [sel, cur] of [["#loc-user", L.current.user], ["#loc-net", L.current.network]]) {
      const all = [...new Set([...codes, ...(cur ? [cur] : [])])];
      $(sel).innerHTML = `<option value="">Standard (Englisch / USA)</option>` +
        all.map(c => `<option value="${esc(c)}" ${c === cur ? "selected" : ""}>${esc(c)}${codes.includes(c) ? "" : " (Paket fehlt)"}</option>`).join("");
    }
    $("#t-loc").innerHTML = L.locales.map(l => `<tr>
        <td>${esc(l.name.replace("_", " / "))} <span class="muted mono">${esc(l.version)}</span></td>
        <td class="mono">${l.dirs.map(esc).join(", ")}</td>
        <td>${l.count}</td>
        <td><button class="small danger" data-loc-del="${esc(l.id)}">Löschen</button></td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Noch kein Sprachpaket am Router.</td></tr>`;
    renderJob();
  }

  async function load() {
    const j = await api("/api/locales");
    if (!j.ok) { $("#t-loc").innerHTML = `<tr><td colspan="4" class="empty">${esc(j.error || "Fehler")}</td></tr>`; return; }
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
      if (j.job.state !== "running") {
        clearInterval(poll);
        poll = null;
        load();
      }
    }, 1500);
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-locset") {
      const btn = f.querySelector("button[type=submit]");
      btn.disabled = true;
      const j = await api("/api/locales/activate", { method: "POST",
        body: JSON.stringify({ user: f.user.value, network: f.network.value, reset: f.reset.checked }) });
      btn.disabled = false;
      if (!j.ok) { showMsg($("#m-locset"), false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-locset"), true, j.unchanged ? "Keine Änderung." : `Übernommen.${j.config?.length ? "\n\n" + j.config.join("\n") : ""}`);
      return;
    }
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
      f.reset();
      L.job = j.job;
      renderJob();
      startPoll();
    };
    xhr.onerror = () => { btn.disabled = false; showMsg(m, false, "Übertragung abgebrochen."); };
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });

  card.addEventListener("click", async (e) => {
    const id = e.target.dataset?.locDel;
    if (!id) return;
    if (!confirm(`Sprachpaket ${id} vom Router löschen (Dateien und tftp-server-Einträge)?`)) return;
    e.target.disabled = true;
    const j = await api(`/api/locales/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!j.ok) { alert(j.error || "Fehler"); e.target.disabled = false; return; }
    load();
  });

  load();
})();
