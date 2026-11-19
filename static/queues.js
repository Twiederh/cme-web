// Warteschlangen (Asterisk app_queue) und Wartemusik. Eigene Karte im Tab Voicemail;
// nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let Q = { enabled: false, queues: [], music: [], numbers: [], mailboxes: [], ivrs: [], strategies: {} };
  let player = null;
  let editing = null;          // id der bearbeiteten Warteschlange, "" = neu, null = zu

  let card = document.getElementById("queue-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "queue-card";
    document.querySelector("main").appendChild(card);
  }
  const h2 = (t, id) => `<h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">${t}
    <span class="count" id="${id}" style="color:var(--muted);font-weight:400"></span></h2>`;
  card.innerHTML = `<h2>Warteschlangen <span class="count" id="c-queue"></span></h2>
    <div id="queue-body"><div class="empty">Lädt …</div></div>
    ${h2("Wartemusik", "c-moh")}<div id="moh-body"></div>`;

  const numName = (num) => { const n = Q.numbers.find(x => x.number === num); return n && n.name ? `${num} (${n.name})` : num; };
  const fmtSecs = (s) => s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min` : `${s} s`;
  const FB_LABEL = { hangup: "Auflegen", voicemail: "Voicemail", dial: "Durchstellen zu", ivr: "Sprachmenü" };
  const STATE_DE = { "Not in use": "frei", "In use": "im Gespräch", "Busy": "besetzt", "Ringing": "klingelt",
                     "Unavailable": "nicht erreichbar", "Invalid": "ungültig", "On Hold": "hält", "Unknown": "frei", "paused": "pausiert" };

  function musicName(id) {
    if (id === "ring") return "Freizeichen statt Musik";
    if (!id) return "keine (Stille)";
    return (Q.music.find(m => m.id === id) || {}).name || id;
  }
  function fbText(f) {
    if (!f || !f.action || f.action === "hangup") return "Auflegen";
    if (f.action === "voicemail") return `Voicemail ${esc(f.target)}`;
    if (f.action === "dial") return `→ ${esc(numName(f.target))}`;
    if (f.action === "ivr") return `→ Menü ${esc((Q.ivrs.find(i => i.id === f.target) || {}).name || f.target)}`;
    return esc(f.action);
  }

  function renderQueues() {
    $("#c-queue").textContent = Q.queues.length ? `(${Q.queues.length})` : "";
    $("#queue-body").innerHTML = `
      <div class="body">
        <div class="hint">Anrufer warten mit Musik, bis eines der Mitglieder (Telefone) frei ist und abnimmt.
          Hat die Warteschlange eine Nummer, legt die Seite dafür einen Dial-Peer zu Asterisk an; ohne Nummer ist sie
          nur über ein Sprachmenü erreichbar (Aktion „Warteschlange“). Asterisk sieht nicht, ob ein Telefon gerade telefoniert –
          bei besetzten Telefonen gilt, was am Router für Anklopfen/Besetzt eingestellt ist.</div>
        <div class="actions"><button class="primary small" id="q-new">+ Neue Warteschlange</button></div>
      </div>
      <table><thead><tr><th>Warteschlange</th><th>Nummer</th><th>Mitglieder</th><th>Verteilung</th><th>Musik / Ausweichziel</th><th>Jetzt</th><th></th></tr></thead><tbody>
        ${Q.queues.map(q => {
          const st = q.status;
          return `<tr><td>${esc(q.name)}</td>
          <td class="mono">${q.number ? esc(q.number) + "<br>" + (q.dialpeer ? `<span class="muted">Dial-Peer ${esc(q.dialpeer)}</span>` : badge("warn", "kein Dial-Peer")) : '<span class="muted">nur über IVR</span>'}</td>
          <td style="font-size:12px">${q.members.map(m => `${esc(numName(m))}${st && st.members[m] ? ` <span class="muted">(${esc(STATE_DE[st.members[m]] || st.members[m])})</span>` : ""}`).join("<br>")}</td>
          <td style="font-size:12px">${esc(Q.strategies[q.strategy] || q.strategy)}<br><span class="muted">${q.ring} s klingeln, ${q.retry} s Pause</span></td>
          <td style="font-size:12px">${esc(musicName(q.music))}<br><span class="muted">${q.maxwait ? `nach ${fmtSecs(q.maxwait)}: ${fbText(q.fallback)}` : "ohne Zeitlimit"}</span></td>
          <td style="font-size:12px">${st ? `${st.calls ? badge("warn", `${st.calls} warten`) : '<span class="muted">niemand wartet</span>'}
            <br><span class="muted">${st.completed} angenommen, ${st.abandoned} aufgelegt</span>` : '<span class="muted">–</span>'}</td>
          <td><div class="actions"><button class="small" data-q-edit="${esc(q.id)}">Bearbeiten</button>
            <button class="small danger" data-q-del="${esc(q.id)}">Löschen</button></div></td></tr>`;
        }).join("") || `<tr><td colspan="7" class="empty">Noch keine Warteschlange.</td></tr>`}</tbody></table>
      <div class="body" style="border-top:1px solid var(--line)" id="q-editor" hidden></div>`;
    if (editing !== null) openEditor(editing);
  }

  function fbTarget(action, value) {
    if (action === "voicemail") return `<select name="fb_target">${Q.mailboxes.map(b =>
      `<option value="${esc(b)}" ${b === value ? "selected" : ""}>${esc(b)}</option>`).join("") || '<option value="">(kein Postfach)</option>'}</select>`;
    if (action === "dial") return `<select name="fb_target">${Q.numbers.map(n =>
      `<option value="${esc(n.number)}" ${n.number === value ? "selected" : ""}>${esc(n.number)}${n.name ? " – " + esc(n.name) : ""}</option>`).join("")}</select>`;
    if (action === "ivr") return `<select name="fb_target">${Q.ivrs.map(i =>
      `<option value="${esc(i.id)}" ${i.id === value ? "selected" : ""}>${esc(i.name)}</option>`).join("") || '<option value="">(kein Sprachmenü)</option>'}</select>`;
    return `<span class="muted">–</span>`;
  }

  function openEditor(id) {
    editing = id;
    const ed = $("#q-editor");
    const q = Q.queues.find(x => x.id === id) || { name: "", number: "", strategy: "ringall", members: [], ring: 15, retry: 3,
                                                   maxwait: 120, music: Q.music[0]?.id || "", announce: false, fallback: { action: "voicemail", target: Q.mailboxes[0] || "" } };
    const fb = q.fallback || { action: "hangup" };
    ed.hidden = false;
    ed.innerHTML = `
      <form id="f-queue" data-id="${esc(id || "")}">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">${id ? `Warteschlange „${esc(q.name)}“ bearbeiten` : "Neue Warteschlange"}</label>
        <div class="row">
          <div><label>Name</label><input name="name" value="${esc(q.name)}" maxlength="40" required></div>
          <div><label>Nummer (leer = nur über Sprachmenü)</label><input name="number" class="mono" value="${esc(q.number || "")}" placeholder="z. B. 8100"></div>
        </div>
        <div class="row">
          <div><label>Verteilung</label><select name="strategy">${Object.entries(Q.strategies).map(([k, v]) =>
            `<option value="${k}" ${k === q.strategy ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
          <div><label>Wartemusik</label><select name="music">
            ${Q.music.map(m => `<option value="${esc(m.id)}" ${m.id === q.music ? "selected" : ""}>${esc(m.name)}${m.files.length ? "" : " (noch leer)"}</option>`).join("")}
            <option value="ring" ${q.music === "ring" ? "selected" : ""}>Freizeichen statt Musik</option>
            <option value="" ${!q.music ? "selected" : ""}>keine (Stille)</option></select></div>
        </div>
        <label>Mitglieder (Telefone)</label>
        <div style="display:flex;flex-wrap:wrap;gap:4px 18px;margin-bottom:10px">${Q.numbers.map(n =>
          `<label class="check" style="margin:0"><input type="checkbox" name="members" value="${esc(n.number)}" ${q.members.includes(n.number) ? "checked" : ""}>
            <span class="mono">${esc(n.number)}</span> ${esc(n.name || "")}</label>`).join("")}</div>
        <div class="row">
          <div class="row" style="margin:0">
            <div><label>Klingeln je Versuch (s)</label><input name="ring" type="number" min="5" max="120" value="${esc(q.ring)}"></div>
            <div><label>Pause dazwischen (s)</label><input name="retry" type="number" min="1" max="60" value="${esc(q.retry)}"></div>
          </div>
          <div><label>Höchstens warten (s, 0 = unbegrenzt)</label><input name="maxwait" type="number" min="0" max="3600" value="${esc(q.maxwait)}"></div>
        </div>
        <div class="ln" style="display:flex;gap:8px;align-items:center;margin-bottom:10px"><span>Danach:</span>
          <select name="fb_action" style="width:auto">${Object.entries(FB_LABEL).map(([k, v]) =>
            `<option value="${k}" ${k === fb.action ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
          <span id="fb-tgt" style="flex:1">${fbTarget(fb.action, fb.target || "")}</span></div>
        <label class="check"><input type="checkbox" name="announce" ${q.announce ? "checked" : ""}> Position in der Warteschlange jede Minute ansagen</label>
        <div class="actions"><button class="primary" type="submit">${id ? "Änderungen speichern" : "Anlegen"}</button>
          <button type="button" id="q-cancel">Abbrechen</button></div>
        <div class="msg" id="m-queue"></div>
      </form>`;
  }

  function renderMusic() {
    $("#c-moh").textContent = Q.music.length ? `(${Q.music.length})` : "";
    $("#moh-body").innerHTML = `
      <div class="body">
        <div class="hint">Eine Wartemusik besteht aus einer oder mehreren Dateien (MP3, WAV, M4A, …), die nacheinander laufen.
          Die Seite rechnet sie für Telefon-Qualität um (8 und 16 kHz) und gleicht die Lautstärke an; höchstens 20 Minuten je Datei.
          Achtung: Für Musik in Warteschleifen braucht es in der Regel eine GEMA-Lizenz oder GEMA-freie Musik.</div>
        <form id="f-moh" class="row" style="align-items:end">
          <div><label>Neue Wartemusik</label><input name="name" maxlength="40" placeholder="z. B. Ruhig" required></div>
          <div class="actions" style="margin:0"><button class="small" type="submit">Anlegen</button></div>
        </form>
        <div class="msg" id="m-moh"></div>
      </div>
      <table><thead><tr><th>Wartemusik</th><th>Dateien</th><th>Datei hinzufügen</th><th></th></tr></thead><tbody>
        ${Q.music.map(m => `<tr><td>${esc(m.name)}<br><label class="check" style="margin:2px 0 0;font-size:12px">
            <input type="checkbox" data-moh-shuffle="${esc(m.id)}" ${m.shuffle ? "checked" : ""}> zufällige Reihenfolge</label></td>
          <td style="font-size:12px">${m.files.map(f => `<div style="display:flex;gap:6px;align-items:center;margin:2px 0">
              <button class="small" data-moh-play="${esc(m.id)}/${esc(f.id)}">▶</button>
              <span>${esc(f.name)} <span class="muted">${fmtSecs(f.secs)}</span></span>
              <button class="small danger" data-moh-delfile="${esc(m.id)}/${esc(f.id)}" title="Datei löschen">✕</button></div>`).join("")
            || '<span class="muted">noch keine Datei</span>'}</td>
          <td><form class="f-mohfile" data-id="${esc(m.id)}" style="display:flex;gap:6px;align-items:center">
              <input type="file" name="file" accept="audio/*,.mp3,.wav,.m4a,.ogg,.flac" required style="max-width:220px">
              <button class="small" type="submit">Hochladen</button></form><div class="msg"></div></td>
          <td><button class="small danger" data-moh-del="${esc(m.id)}">Löschen</button></td></tr>`).join("")
          || `<tr><td colspan="4" class="empty">Noch keine Wartemusik.</td></tr>`}</tbody></table>`;
  }

  function render() {
    if (!Q.enabled) {
      $("#queue-body").innerHTML = `<div class="body"><div class="hint">Warteschlangen brauchen den Asterisk-Container
        (siehe Voicemail): <span class="mono">VM_HOST_IP</span> und <span class="mono">VM_AMI_SECRET</span> im Stack setzen.</div></div>`;
      $("#moh-body").innerHTML = "";
      return;
    }
    renderQueues();
    renderMusic();
  }

  async function load() {
    const j = await api("/api/queues");
    if (!j.ok) { $("#queue-body").innerHTML = `<div class="empty bad-text">${esc(j.error || "Fehler beim Laden")}</div>`; return; }
    Q = { ...Q, ...j };
    render();
  }

  function readQueue(f) {
    return {
      name: f.name.value, number: f.number.value.trim(), strategy: f.strategy.value, music: f.music.value,
      members: [...f.querySelectorAll("input[name=members]:checked")].map(x => x.value),
      ring: f.ring.value, retry: f.retry.value, maxwait: f.maxwait.value, announce: f.announce.checked,
      fallback: { action: f.fb_action.value, target: f.querySelector("[name=fb_target]")?.value || "" },
    };
  }

  async function upload(url, file) {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(url, { method: "POST", body: fd, headers: { "X-CME-Request": "1" } });
    try { return await r.json(); } catch { return { ok: false, error: r.status === 413 ? "Datei zu groß." : `HTTP ${r.status}` }; }
  }

  card.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.name === "fb_action") {
      $("#fb-tgt").innerHTML = fbTarget(t.value, "");
    } else if (t.dataset.mohShuffle) {
      const m = Q.music.find(x => x.id === t.dataset.mohShuffle);
      await api(`/api/queues/music/${m.id}`, { method: "PUT", body: JSON.stringify({ name: m.name, shuffle: t.checked }) });
      load();
    }
  });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-queue") {
      const m = $("#m-queue"), id = f.dataset.id, btn = f.querySelector("button[type=submit]");
      btn.disabled = true;
      const j = id ? await api(`/api/queues/${id}`, { method: "PUT", body: JSON.stringify(readQueue(f)) })
                   : await api("/api/queues", { method: "POST", body: JSON.stringify(readQueue(f)) });
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      editing = null;
      await load();
    } else if (f.id === "f-moh") {
      const j = await api("/api/queues/music", { method: "POST", body: JSON.stringify({ name: f.name.value }) });
      if (!j.ok) { showMsg($("#m-moh"), false, j.error || "Fehler"); return; }
      load();
    } else if (f.classList.contains("f-mohfile")) {
      const m = f.parentNode.querySelector(".msg"), btn = f.querySelector("button");
      const file = f.file.files[0];
      if (!file) return;
      btn.disabled = true;
      showMsg(m, true, "Wird umgerechnet …");
      const j = await upload(`/api/queues/music/${f.dataset.id}/files`, file);
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      load();
    }
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (e.target.id === "q-new") {
      openEditor("");
      $("#q-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else if (e.target.id === "q-cancel") {
      editing = null;
      $("#q-editor").hidden = true;
    } else if (d.qEdit) {
      openEditor(d.qEdit);
      $("#q-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else if (d.qDel) {
      const q = Q.queues.find(x => x.id === d.qDel);
      if (!confirm(`Warteschlange „${q.name}“ löschen?${q.number ? `\nDer Dial-Peer für ${q.number} wird entfernt.` : ""}`)) return;
      const j = await api(`/api/queues/${d.qDel}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (d.mohPlay) {
      if (player) player.pause();
      player = new Audio(`/api/queues/music/${d.mohPlay.replace("/", "/files/")}`);
      player.play().catch(() => alert("Abspielen nicht möglich."));
    } else if (d.mohDelfile) {
      if (!confirm("Diese Datei aus der Wartemusik löschen?")) return;
      const [mid, fid] = d.mohDelfile.split("/");
      const j = await api(`/api/queues/music/${mid}/files/${fid}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (d.mohDel) {
      if (!confirm("Diese Wartemusik mit allen Dateien löschen?")) return;
      const j = await api(`/api/queues/music/${d.mohDel}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    }
  });

  load();
  // Status (wartende Anrufer) regelmaessig auffrischen, aber nicht beim Bearbeiten
  setInterval(() => { if (!document.hidden && editing === null && !$("#tab-voicemail")?.hidden) load(); }, 15000);
})();
