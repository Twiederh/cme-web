// Benutzerseite (/me): eigene Voicemail, Gespraechsaufzeichnungen, Fax senden/empfangen, Kurzwahltasten, gemeinsames Telefonbuch.
// Daten aus /api/user; Aktionen ueber die vorhandenen API-Aufrufe (portal.py prueft die Rechte).
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const badge = (cls, txt) => `<span class="badge ${cls}">${esc(txt)}</span>`;
  const showMsg = (el, ok, text) => { if (el) { el.className = "msg " + (ok ? "ok" : "bad"); el.textContent = text; } };
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (r.status === 401) { location.href = "/login?next=/me"; return { ok: false, error: "Nicht angemeldet" }; }
    let j; try { j = await r.json(); } catch { j = { ok: false, error: `HTTP ${r.status}` }; }
    if (!r.ok && j.ok === undefined) j.ok = false;
    return j;
  }
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
  const fmtDur = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const sub = (t) => `<h3 style="font-size:13px;margin:0;padding:10px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);color:var(--muted);font-weight:500">${t}</h3>`;
  const link = (href, text, title = "") => `<a class="btn" ${title ? `title="${esc(title)}"` : ""} style="text-decoration:none;padding:2px 8px;font-size:12px" target="_blank" href="${href}">${text}</a>`;

  let U = null, player = null, poll = null, pbFilter = "";

  // ---------------- Voicemail ----------------

  function renderVm() {
    const boxes = U.voicemail;
    $("#u-vm").hidden = !boxes.length;
    const nNew = boxes.reduce((a, b) => a + b.new, 0);
    $("#c-vm").textContent = nNew ? `(${nNew} neu)` : "";
    $("#u-vm-body").innerHTML = boxes.map(b => `
      ${boxes.length > 1 ? sub(`Postfach ${esc(b.number)}${b.name ? " – " + esc(b.name) : ""}`) : ""}
      ${b.messages.length ? `<table><thead><tr><th>Zeit</th><th>Von</th><th>Dauer</th><th></th><th></th></tr></thead><tbody>
        ${b.messages.map(m => {
          const ref = `${b.id}/${m.folder}/${m.file}`;
          return `<tr><td>${fmtTime(m.time)} ${m.folder === "INBOX" ? badge("ok", "neu") : ""}</td>
            <td>${m.caller_name ? `${esc(m.caller_name)} <span class="muted mono">${esc(m.caller)}</span>` : `<span class="mono">${esc(m.caller || "unbekannt")}</span>`}</td>
            <td class="mono">${fmtDur(m.duration)}</td>
            <td>${m.audio ? `<button class="small" data-play="${esc(ref)}">▶</button>
              ${link(`/api/voicemail/audio/${esc(ref)}?download=1`, "⤓", "Herunterladen")}` : '<span class="muted">–</span>'}</td>
            <td><div class="actions"><button class="small" data-move="${esc(ref)}">${m.folder === "INBOX" ? "als gehört" : "als neu"}</button>
              <button class="small danger" data-vmdel="${esc(ref)}">Löschen</button></div></td></tr>`;
        }).join("")}</tbody></table>` : `<div class="empty">Keine Nachrichten.</div>`}`).join("")
      + `<div class="body" style="border-top:1px solid var(--line)"><div class="hint">Am Telefon: Taste <b>Nachrichten</b>.
        ${boxes.some(b => b.email) ? "Neue Nachrichten kommen zusätzlich per E-Mail." : ""}</div></div>`;
  }

  // ---------------- Gespraechsaufzeichnungen ----------------

  const DIR = { in: "↘ eingehend", out: "↗ ausgehend" };
  let recOpen = null;
  function otherSide(r) {
    const mine = new Set(U.numbers.map(n => n.number));
    const p = (r.parties || []).find(x => x.number && !mine.has(x.number) && x.number !== r.internal);
    const num = p ? p.number : (mine.has(r.internal) ? r.external : r.internal) || r.external;
    const name = p?.name && p.name !== num ? p.name : (U.phonebook.find(e => e.number === num)?.name || "");
    return name ? `${esc(name)} <span class="muted mono">${esc(num)}</span>` : `<span class="mono">${esc(num || "unbekannt")}</span>`;
  }
  function renderRec() {
    const R = U.recordings;
    $("#u-rec").hidden = !R || !R.length;      // nur zeigen, wenn es Aufnahmen gibt
    if ($("#u-rec").hidden) return;
    $("#c-rec").textContent = R.length ? `(${R.length})` : "";
    $("#u-rec-body").innerHTML = R.length ? `<table><thead><tr><th>Zeit</th><th>Richtung</th><th>Meine Nummer</th><th>Gesprächspartner</th><th>Dauer</th><th></th></tr></thead><tbody>
      ${R.map(r => `<tr><td>${fmtTime(r.start)}</td><td>${esc(DIR[r.direction] || "–")}</td>
        <td class="mono">${esc(r.internal || "–")}</td><td>${otherSide(r)}</td>
        <td class="mono">${fmtDur(Math.round(r.duration || 0))}${r.partial ? ` <span title="Während des Gesprächs aus- oder eingeschaltet; Pausen sind Stille">${badge("neutral", "teilweise")}</span>` : ""}</td>
        <td><div class="actions"><button class="small" data-recplay="${esc(r.id)}">▶</button>
          ${r.transcript ? `<button class="small" data-rectext="${esc(r.id)}">Text</button>` : (r.transcript_status === "pending" ? badge("neutral", "Abschrift läuft …") : "")}
          ${link(`/api/recording/files/${esc(r.id)}?download=1`, "⤓", "Herunterladen")}</div></td></tr>
        ${r.transcript ? `<tr data-recrow="${esc(r.id)}" ${recOpen === r.id ? "" : "hidden"}><td colspan="6">
          <div style="white-space:pre-wrap;font-size:13px;line-height:1.5">${esc(r.transcript)}</div></td></tr>` : ""}`).join("")}
      </tbody></table>` : `<div class="empty">Noch keine Aufzeichnungen.</div>`;
  }

  // ---------------- Fax ----------------

  function renderFax() {
    const F = U.fax;
    $("#u-fax").hidden = !F.enabled || !F.senders.length;
    if ($("#u-fax").hidden) return;
    const keep = $("#u-faxsend") ? { to: $("#u-faxsend").to.value, from: $("#u-faxsend").from.value, header: $("#u-faxsend").header.value,
                                     files: $("#u-faxsend").file.files } : null;
    $("#u-fax-body").innerHTML = `
      <div class="body">
        <form id="u-faxsend">
          <div class="row">
            <div><label>An (Faxnummer) *</label><input name="to" class="mono" required placeholder="069123456"></div>
            <div><label>Absender *</label><select name="from">${F.senders.map(s =>
              `<option value="${esc(s.number)}" ${F.numbers.some(x => x.number === s.number) ? "selected" : ""}>${esc(s.number)}${s.name ? " – " + esc(s.name) : ""}</option>`).join("")}</select></div>
          </div>
          <div class="row">
            <div><label>PDF *</label><input name="file" type="file" accept="application/pdf,.pdf" required></div>
            <div><label>Kopfzeile <span class="muted">(oben auf jeder Seite)</span></label><input name="header" maxlength="50"></div>
          </div>
          <div class="actions"><button class="primary" type="submit">Fax senden</button></div>
          <div class="msg" id="m-faxsend"></div>
        </form>
      </div>
      ${F.sent.length ? sub("Gesendet") + `<table><thead><tr><th>Zeit</th><th>An</th><th>Datei</th><th>Seiten</th><th>Status</th><th></th></tr></thead><tbody>
        ${F.sent.map(j => `<tr><td>${fmtTime(j.time)}</td><td class="mono">${esc(j.to)}</td><td>${esc(j.file)}</td>
          <td>${esc(j.status === "SUCCESS" ? (j.sent || j.pages) : j.pages)}</td>
          <td>${j.status === "calling" ? badge("neutral", "wird gesendet …") : j.status === "SUCCESS" ? badge("ok", "gesendet") : badge("bad", "fehlgeschlagen")}
            ${j.error && j.status !== "SUCCESS" ? `<span class="muted">${esc(j.error)}</span>` : ""}</td>
          <td><div class="actions">${j.pdf ? link(`/api/vmapps/fax/sent/${esc(j.id)}.pdf`, "PDF") : ""}
            ${j.status === "FAILED" ? `<button class="small" data-retry="${esc(j.id)}">Nochmal</button>` : ""}
            ${j.status !== "calling" ? `<button class="small danger" data-sentdel="${esc(j.id)}">Löschen</button>` : ""}</div></td></tr>`).join("")}
        </tbody></table>` : ""}
      ${F.numbers.length ? sub(`Empfangen <span style="font-weight:400">an ${F.numbers.map(x => esc(x.number)).join(", ")}</span>`) +
        (F.received.length ? `<table><thead><tr><th>Zeit</th><th>Von</th><th>Seiten</th><th>Status</th><th></th></tr></thead><tbody>
        ${F.received.map(r => `<tr><td>${fmtTime(r.time)}</td><td class="mono">${esc(r.caller || r.station || "unbekannt")}</td>
          <td>${esc(r.pages)}</td><td>${r.status === "SUCCESS" ? badge("ok", "empfangen") : badge("bad", r.status || "Fehler")}</td>
          <td><div class="actions">${r.pdf ? link(`/api/vmapps/fax/files/${esc(r.id)}.pdf`, "PDF") : ""}
            <button class="small danger" data-faxdel="${esc(r.id)}">Löschen</button></div></td></tr>`).join("")}</tbody></table>`
          : `<div class="empty">Noch keine Faxe empfangen.</div>`) : ""}`;
    if (keep) {
      const f = $("#u-faxsend");
      f.to.value = keep.to; f.from.value = keep.from; f.header.value = keep.header;
      if (keep.files.length) f.file.files = keep.files;
    }
  }

  // ---------------- Kurzwahlen ----------------

  const sdRow = (i, s) => `<tr><td class="muted">${i}</td>
    <td><input name="sd-number" list="u-dl" value="${esc(s.number)}" placeholder="Nummer" class="mono" style="width:180px"></td>
    <td><input name="sd-label" value="${esc(s.label)}" placeholder="Beschriftung" maxlength="30" style="width:200px"></td>
    <td><button type="button" class="small" data-sdrm title="Zeile entfernen">✕</button></td></tr>`;

  function renderSd() {
    $("#u-sd").hidden = !U.pools.length;
    $("#u-sd-body").innerHTML = `<div class="body"><div class="hint">Freie Tasten am Telefon mit einer Nummer belegen – so eintragen,
      wie man sie am Telefon wählt. Nach dem Speichern startet das Telefon neu (dauert etwa eine Minute).</div></div>` +
      U.pools.map(p => `${U.pools.length > 1 ? sub(`${esc(p.desc || "Telefon " + p.tag)} <span class="mono">${esc(p.lines.join(", "))}</span>`) : ""}
        <form data-pool="${esc(p.tag)}" class="body" style="padding-top:6px">
          <table style="width:auto"><thead><tr><th>#</th><th>Nummer</th><th>Beschriftung</th><th></th></tr></thead>
            <tbody>${[...p.speed, { number: "", label: "" }].map((s, i) => sdRow(i + 1, s)).join("")}</tbody></table>
          <div class="actions" style="margin-top:8px"><button type="button" class="small" data-sdadd>Zeile hinzufügen</button>
            <button class="primary" type="submit">Speichern</button></div>
          <div class="msg"></div>
        </form>`).join("");
  }

  // ---------------- Telefonbuch ----------------

  function renderPb() {
    const all = U.phonebook;
    const q = pbFilter.trim().toLowerCase(), qd = q.replace(/\D/g, "");
    const rows = all.filter(e => !q || (e.name || "").toLowerCase().includes(q) || (qd && e.number.replace(/\D/g, "").includes(qd)));
    const unnamed = all.filter(e => !e.name).length;
    $("#c-pb").textContent = `(${all.length}${unnamed ? `, ${unnamed} ohne Namen` : ""})`;
    $("#u-dl").innerHTML = all.filter(e => e.name).map(e => `<option value="${esc(e.number)}">${esc(e.name)}</option>`).join("");
    const body = $("#u-pb-body");
    if (!body.querySelector("#u-pb-q")) {
      body.innerHTML = `<div class="body">
          <form id="u-pbadd"><div class="row">
            <div><label>Rufnummer</label><input name="number" class="mono" required placeholder="01701234567"></div>
            <div><label>Name</label><input name="name" required maxlength="60" placeholder="Max Muster"></div></div>
            <div class="actions"><button class="primary" type="submit">Eintragen</button></div>
            <div class="msg" id="m-pbadd"></div></form>
          <div style="margin-top:12px"><input id="u-pb-q" placeholder="Suchen (Name oder Nummer) …" style="max-width:320px"></div>
        </div>
        <table><thead><tr><th>Rufnummer</th><th>Name</th><th>Anrufe</th><th>Zuletzt</th><th></th></tr></thead><tbody id="u-pb-rows"></tbody></table>`;
      $("#u-pb-q").addEventListener("input", (e) => { pbFilter = e.target.value; renderPb(); });
    }
    $("#u-pb-rows").innerHTML = rows.map(e => `<tr><td class="mono">${esc(e.number)}</td>
      <td><input data-pbname="${esc(e.id)}" value="${esc(e.name)}" placeholder="Name eintragen" maxlength="60" style="max-width:280px"></td>
      <td>${esc(e.calls || 0)}</td><td>${fmtTime(e.last)}</td>
      <td><button class="small danger" data-pbdel="${esc(e.id)}">Löschen</button></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">${all.length ? "Keine Treffer." : "Noch keine Einträge."}</td></tr>`;
  }

  // ---------------- Laden ----------------

  async function load() {
    const j = await api("/api/user");
    if (!j.ok) { $("#errors").style.display = "block"; $("#errors").textContent = j.error || "Fehler"; return; }
    U = j;
    $("#user-name").textContent = j.user.name + (j.user.provider ? ` (${j.user.provider})` : "");
    $("#u-admin").hidden = !j.user.admin;
    $("#u-numbers").textContent = j.numbers.map(n => n.number + (n.name ? ` ${n.name}` : "")).join(" · ");
    $("#u-none").hidden = !!j.numbers.length;
    const err = $("#errors");
    err.style.display = j.error ? "block" : "none";
    err.textContent = j.error || "";
    renderVm(); renderRec(); renderFax(); renderSd(); renderPb();
    clearTimeout(poll);
    if (U.fax.enabled && U.fax.sent.some(x => x.status === "calling")) poll = setTimeout(load, 5000);
  }

  // ---------------- Ereignisse ----------------

  document.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.play) {
      if (player) player.pause();
      player = new Audio(`/api/voicemail/audio/${d.play}`);
      player.play().catch(() => alert("Abspielen nicht möglich."));
    } else if (d.recplay) {
      if (player) player.pause();
      player = new Audio(`/api/recording/files/${encodeURIComponent(d.recplay)}`);
      player.play().catch(() => alert("Abspielen nicht möglich."));
    } else if (d.rectext) {
      const row = document.querySelector(`tr[data-recrow="${d.rectext}"]`);
      if (row) { row.hidden = !row.hidden; recOpen = row.hidden ? null : d.rectext; }
    } else if (d.move || d.vmdel) {
      if (d.vmdel && !confirm("Nachricht löschen?")) return;
      e.target.disabled = true;
      const j = d.vmdel ? await api(`/api/voicemail/messages/${d.vmdel}`, { method: "DELETE" })
                        : await api(`/api/voicemail/messages/${d.move}/move`, { method: "POST" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      load();
    } else if (d.retry) {
      e.target.disabled = true;
      const j = await api(`/api/vmapps/fax/sent/${encodeURIComponent(d.retry)}/retry`, { method: "POST" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      load();
    } else if (d.sentdel || d.faxdel) {
      if (!confirm("Fax löschen?")) return;
      const j = await api(d.sentdel ? `/api/vmapps/fax/sent/${encodeURIComponent(d.sentdel)}` : `/api/vmapps/fax/files/${encodeURIComponent(d.faxdel)}`,
                          { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (d.sdadd !== undefined) {
      const tb = e.target.closest("form").querySelector("tbody");
      tb.insertAdjacentHTML("beforeend", sdRow(tb.children.length + 1, { number: "", label: "" }));
    } else if (d.sdrm !== undefined) {
      const tb = e.target.closest("tbody");
      e.target.closest("tr").remove();
      [...tb.children].forEach((tr, i) => { tr.firstElementChild.textContent = i + 1; });
    } else if (d.pbdel) {
      const ent = U.phonebook.find(x => x.id === d.pbdel);
      if (!confirm(`${ent ? ent.number : "Eintrag"} aus dem Telefonbuch löschen?`)) return;
      const j = await api(`/api/phonebook/entries/${encodeURIComponent(d.pbdel)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (e.target.id === "u-refresh") {
      load();
    }
  });

  // Name im Telefonbuch: beim Verlassen des Feldes speichern
  document.addEventListener("change", async (e) => {
    const id = e.target.dataset?.pbname;
    if (!id) return;
    const j = await api(`/api/phonebook/entries/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ name: e.target.value }) });
    e.target.style.outline = j.ok ? "2px solid var(--ok)" : "2px solid var(--bad)";
    setTimeout(() => { e.target.style.outline = ""; }, 1500);
    if (!j.ok) { alert(j.error || "Fehler"); return; }
    const ent = U.phonebook.find(x => x.id === id);
    if (ent) ent.name = e.target.value.trim();
    if (j.router && j.router.ok === false) alert("Gespeichert, aber die Anzeige beim Klingeln konnte nicht aktualisiert werden: " + (j.router.error || ""));
  });

  document.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    if (f.id === "u-faxsend") {
      const m = $("#m-faxsend");
      btn.disabled = true;
      showMsg(m, true, "PDF wird umgerechnet …");
      let j;
      try {
        const r = await fetch("/api/vmapps/fax/send", { method: "POST", body: new FormData(f), headers: { "X-CME-Request": "1" } });
        j = r.status === 413 ? { ok: false, error: "Datei zu groß." } : await r.json();
      } catch (err) { j = { ok: false, error: err.message }; }
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      f.reset();
      await load();
      showMsg($("#m-faxsend"), true, `Fax an ${j.job.to} (${j.job.pages} Seite${j.job.pages === 1 ? "" : "n"}) wird gesendet.`);
    } else if (f.dataset.pool) {
      const pool = f.dataset.pool;
      const speed = [...f.querySelectorAll("tbody tr")].map(tr => ({
        number: tr.querySelector("[name=sd-number]").value.trim(), label: tr.querySelector("[name=sd-label]").value.trim() }))
        .filter(s => s.number);
      btn.disabled = true;
      const j = await api(`/api/phonebook/speeddials/${pool}`, { method: "PUT", body: JSON.stringify({ speed, restart: true }) });
      btn.disabled = false;
      if (!j.ok) { showMsg(f.querySelector(".msg"), false, j.error || "Fehler"); return; }
      await load();
      showMsg(document.querySelector(`form[data-pool="${pool}"] .msg`), true, `${speed.length} Kurzwahl(en) gespeichert, das Telefon startet neu.`);
    } else if (f.id === "u-pbadd") {
      btn.disabled = true;
      const j = await api("/api/phonebook/entries", { method: "POST", body: JSON.stringify({ number: f.number.value, name: f.name.value }) });
      btn.disabled = false;
      if (!j.ok) { showMsg($("#m-pbadd"), false, j.error || "Fehler"); return; }
      f.reset();
      await load();
      showMsg($("#m-pbadd"), true, "Eingetragen.");
    }
  });

  load();
})();
