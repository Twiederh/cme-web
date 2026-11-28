// Fax (Empfang und Senden) und Sprachmenues (IVR) mit Asterisk. Haengt sich in die Voicemail-Karte ein
// (wird von voicemail.js nachgeladen) und nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  const vmCard = document.getElementById("voicemail-card");
  if (!vmCard || document.getElementById("vmapps")) return;
  let A = { enabled: false, fax: [], received: [], ivrs: [], numbers: [], mailboxes: [], keys: [], queues: [] };
  let player = null;

  const ACTION_LABEL = { "": "–", dial: "Durchstellen zu", ivr: "Untermenü", queue: "Warteschlange", conf: "Konferenzraum", voicemail: "Voicemail", repeat: "Ansage wiederholen", hangup: "Auflegen" };
  const h2 = (t, id) => `<h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">${t}
    <span class="count" id="${id}" style="color:var(--muted);font-weight:400"></span></h2>`;
  const box = document.createElement("div");
  box.id = "vmapps";
  box.innerHTML = `${h2("Fax", "c-fax")}<div id="fax-body"></div>
    ${h2("Sprachmenüs (IVR)", "c-ivr")}<div id="ivr-body"></div>`;
  vmCard.appendChild(box);

  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
  const peerBadge = (x) => x.dialpeer ? `<span class="muted">Dial-Peer ${esc(x.dialpeer)}</span>` : badge("warn", "kein Dial-Peer");
  const numName = (num) => { const n = (A.dial_targets || A.numbers).find(x => x.number === num); return n && n.name ? `${num} (${n.name})` : num; };

  // ---------------- Fax ----------------

  function renderFax() {
    $("#c-fax").textContent = A.fax.length ? `(${A.fax.length})` : "";
    $("#fax-body").innerHTML = `
      <div class="body">
        <div class="hint">Faxe an diese Nummern nimmt Asterisk an und schickt sie als PDF per E-Mail.
          Die Nummer ist die, die der Router für den Anruf sieht (nach den Übersetzungsregeln des Trunks).
          Für jede Nummer legt die Seite einen Dial-Peer mit G.711 und Fax-Passthrough an.
          ${A.mail ? "" : `<br><b>E-Mail-Versand ist noch nicht eingerichtet</b> (VM_SMTP_USER, VM_SMTP_PASSWORD im Stack).`}</div>
      </div>
      <table><thead><tr><th>Faxnummer</th><th>Name</th><th>E-Mail an</th><th>Router</th><th></th></tr></thead><tbody>
        ${A.fax.map(f => `<tr><td class="mono">${esc(f.number)}</td><td>${esc(f.name) || '<span class="muted">–</span>'}</td>
          <td>${esc(f.email)}</td><td>${peerBadge(f)}</td>
          <td><div class="actions"><button class="small" data-fax-edit="${esc(f.number)}">Bearbeiten</button>
            <button class="small danger" data-fax-del="${esc(f.number)}">Löschen</button></div></td></tr>`).join("")
          || `<tr><td colspan="5" class="empty">Noch keine Faxnummer.</td></tr>`}</tbody></table>
      <div class="body" style="border-top:1px solid var(--line)">
        <form id="f-fax">
          <input type="hidden" name="edit" value="">
          <label id="fax-title" style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Neue Faxnummer</label>
          <div class="row">
            <div><label>Faxnummer *</label><input name="number" class="mono" required placeholder="4961874191289"></div>
            <div><label>Name</label><input name="name" maxlength="40" placeholder="Fax Büro"></div>
          </div>
          <div class="row">
            <div><label>E-Mail an *</label><input name="email" type="email" required placeholder="name@icloud.com"></div>
            <div></div>
          </div>
          <div class="actions"><button class="primary" type="submit" id="fax-submit">Anlegen</button>
            <button type="button" class="small" id="fax-cancel" hidden>Abbrechen</button></div>
          <div class="msg" id="m-fax"></div>
        </form>
      </div>
      <h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">Fax senden</h2>
      <div class="body">
        <form id="f-faxsend">
          <div class="row">
            <div><label>An (Faxnummer) *</label><input name="to" class="mono" required placeholder="069123456"></div>
            <div><label>Absender *</label><select name="from">${senders()}</select></div>
          </div>
          <div class="row">
            <div><label>PDF *</label><input name="file" type="file" accept="application/pdf,.pdf" required></div>
            <div><label>Kopfzeile <span class="muted">(oben auf jeder Seite)</span></label>
              <input name="header" maxlength="50" placeholder="z. B. Thomas Wiederhold"></div>
          </div>
          <div class="hint">Die Zielnummer so eingeben, wie man sie am Telefon wählt. Asterisk ruft über den Router an
            (G.711, Absender wie gewählt) – am Provider-Dial-Peer muss Fax durchgereicht werden
            (<span class="mono">fax protocol pass-through g711alaw</span>), sonst bricht die Übertragung ab.</div>
          <div class="actions"><button class="primary" type="submit" id="faxsend-submit">Senden</button></div>
          <div class="msg" id="m-faxsend"></div>
        </form>
      </div>
      ${(A.sent || []).length ? `<table><thead><tr><th>Zeit</th><th>An</th><th>Von</th><th>Datei</th><th>Seiten</th><th>Status</th><th></th></tr></thead><tbody>
        ${A.sent.map(j => `<tr><td>${fmtTime(j.time)}</td><td class="mono">${esc(j.to)}</td><td class="mono">${esc(j.from)}</td>
          <td>${esc(j.file)}</td><td>${j.status === "SUCCESS" ? esc(j.sent || j.pages) : esc(j.pages)}</td>
          <td>${j.status === "calling" ? badge("neutral", "wird gesendet …") : j.status === "SUCCESS" ? badge("ok", "gesendet")
                : badge("bad", "fehlgeschlagen")} ${j.error && j.status !== "SUCCESS" ? `<span class="muted">${esc(j.error)}</span>` : ""}
            ${j.tries > 1 ? `<span class="muted">(${j.tries}. Versuch)</span>` : ""}</td>
          <td><div class="actions">${j.pdf ? `<a class="btn" style="text-decoration:none;padding:2px 8px;font-size:12px" target="_blank" href="/api/vmapps/fax/sent/${esc(j.id)}.pdf">PDF</a>` : ""}
            ${j.status === "FAILED" ? `<button class="small" data-faxsent-retry="${esc(j.id)}">Nochmal</button>` : ""}
            ${j.status !== "calling" ? `<button class="small danger" data-faxsent-del="${esc(j.id)}">Löschen</button>` : ""}</div></td></tr>`).join("")}
        </tbody></table>` : ""}
      <h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)">Empfangene Faxe</h2>
      ${A.received.length ? `<table><thead><tr><th>Zeit</th><th>An</th><th>Von</th><th>Seiten</th><th>Status</th><th></th></tr></thead><tbody>
        ${A.received.map(r => `<tr><td>${fmtTime(r.time)}</td><td>${esc(r.name || r.number)}</td>
          <td class="mono">${esc(r.caller || r.station || "unbekannt")}</td><td>${esc(r.pages)}</td>
          <td>${r.status === "SUCCESS" ? badge("ok", "empfangen") : badge("bad", r.status || "Fehler")}
            ${r.mailed ? '<span class="muted">✉</span>' : (r.email ? badge("warn", "Mail fehlgeschlagen") : "")}</td>
          <td><div class="actions">${r.pdf ? `<a class="btn" style="text-decoration:none;padding:2px 8px;font-size:12px" target="_blank" href="/api/vmapps/fax/files/${esc(r.id)}.pdf">PDF</a>
              <a class="btn" title="Herunterladen" style="text-decoration:none;padding:2px 8px;font-size:12px" href="/api/vmapps/fax/files/${esc(r.id)}.pdf?download=1">⤓</a>` : ""}
            <button class="small danger" data-faxfile-del="${esc(r.id)}">Löschen</button></div></td></tr>`).join("")}
        </tbody></table>` : `<div class="empty">Noch keine Faxe.</div>`}`;
  }

  function senders() {
    const seen = new Set(), opts = [];
    for (const f of A.fax) if (!seen.has(f.number)) { seen.add(f.number); opts.push([f.number, `Fax ${f.number}${f.name ? " – " + f.name : ""}`]); }
    for (const n of A.numbers) if (!seen.has(n.number)) { seen.add(n.number); opts.push([n.number, `${n.number}${n.name ? " – " + n.name : ""}`]); }
    return opts.map(([v, l]) => `<option value="${esc(v)}" ${v === lastSender ? "selected" : ""}>${esc(l)}</option>`).join("");
  }
  let lastSender = "", pollTimer = null;

  function endFaxEdit() {
    const f = $("#f-fax");
    f.reset(); f.edit.value = "";
    $("#fax-title").textContent = "Neue Faxnummer";
    $("#fax-submit").textContent = "Anlegen";
    $("#fax-cancel").hidden = true;
  }

  // ---------------- IVR ----------------

  const keyLabel = (k) => k === "*" ? "✱" : k;
  function actionText(a) {
    if (!a || !a.action) return "";
    if (a.action === "dial") return `→ ${esc(numName(a.target))}`;
    if (a.action === "ivr") return `→ Menü ${esc((A.ivrs.find(i => String(i.id) === a.target) || {}).name || a.target)}`;
    if (a.action === "voicemail") return `→ Voicemail ${esc(a.target)}`;
    if (a.action === "conf") return `→ Konferenz ${esc(((A.confs || []).find(c => c.id === a.target) || {}).name || a.target)}`;
    if (a.action === "queue") return `→ Warteschlange ${esc(((A.queues || []).find(q => q.id === a.target) || {}).name || a.target)}`;
    return esc(ACTION_LABEL[a.action]);
  }

  function renderIvr() {
    $("#c-ivr").textContent = A.ivrs.length ? `(${A.ivrs.length})` : "";
    $("#ivr-body").innerHTML = `
      <div class="body">
        <div class="hint">Ein Sprachmenü spielt eine Ansage ab und verbindet je nach gedrückter Taste weiter.
          Hat das Menü eine Nummer, legt die Seite dafür einen Dial-Peer zu Asterisk an. Für eingehende Anrufe
          die Nummer eintragen, die der Router für den Anruf sieht. Untermenüs brauchen keine Nummer.</div>
        <div class="actions"><button class="primary small" id="ivr-new">+ Neues Menü</button></div>
      </div>
      <table><thead><tr><th>Menü</th><th>Nummer</th><th>Ansage</th><th>Tasten</th><th>Router</th><th></th></tr></thead><tbody>
        ${A.ivrs.map(i => `<tr><td>${esc(i.name)}</td>
          <td class="mono">${i.number ? esc(i.number) : '<span class="muted">Untermenü</span>'}</td>
          <td>${i.greeting ? `<button class="small" data-ivr-play="${i.id}">▶</button>` : badge("warn", "keine Ansage")}</td>
          <td class="mono" style="font-size:12px">${Object.entries(i.keys).map(([k, a]) => `${esc(keyLabel(k))} ${actionText(a)}`).join("<br>") || '<span class="muted">–</span>'}
            ${i.direct ? '<br><span class="muted">Durchwahl erlaubt</span>' : ""}</td>
          <td>${i.number ? peerBadge(i) : '<span class="muted">–</span>'}</td>
          <td><div class="actions"><button class="small" data-ivr-edit="${i.id}">Bearbeiten</button>
            <button class="small danger" data-ivr-del="${i.id}">Löschen</button></div></td></tr>`).join("")
          || `<tr><td colspan="6" class="empty">Noch kein Sprachmenü.</td></tr>`}</tbody></table>
      <div class="body" style="border-top:1px solid var(--line)" id="ivr-editor" hidden></div>`;
  }

  function dialList() {   // Vorschläge einmal im body, damit sie beim Neuzeichnen einer Zeile nicht verschwinden
    let dl = document.getElementById("vmapps-dial");
    if (!dl) { dl = document.createElement("datalist"); dl.id = "vmapps-dial"; document.body.appendChild(dl); }
    dl.innerHTML = (A.dial_targets || A.numbers).map(n =>
      `<option value="${esc(n.number)}">${esc(n.number)}${n.name ? " – " + esc(n.name) : ""}</option>`).join("");
    return "";
  }

  function targetField(action, value, ownId) {
    if (action === "dial") return `<input name="target" list="vmapps-dial" value="${esc(value)}" placeholder="Rufnummer"
      title="Jede Nummer, die die CME wählen kann (Telefone, Jitsi-Räume, Dial-Peers)" style="width:100%">${dialList()}`;
    if (action === "ivr") return `<select name="target">${A.ivrs.filter(i => i.id !== ownId).map(i =>
      `<option value="${i.id}" ${String(i.id) === value ? "selected" : ""}>${esc(i.name)}</option>`).join("")
      || '<option value="">(kein anderes Menü)</option>'}</select>`;
    if (action === "queue") return `<select name="target">${(A.queues || []).map(q =>
      `<option value="${esc(q.id)}" ${q.id === value ? "selected" : ""}>${esc(q.name)}</option>`).join("")
      || '<option value="">(keine Warteschlange)</option>'}</select>`;
    if (action === "conf") return `<select name="target">${(A.confs || []).map(c =>
      `<option value="${esc(c.id)}" ${c.id === value ? "selected" : ""}>${esc(c.name)} (${esc(c.number)})</option>`).join("")
      || '<option value="">(kein Konferenzraum)</option>'}</select>`;
    if (action === "voicemail") return `<select name="target">${A.mailboxes.map(b =>
      `<option value="${esc(b.number)}" ${b.number === value ? "selected" : ""}>${esc(b.number)}</option>`).join("")
      || '<option value="">(kein Postfach)</option>'}</select>`;
    return `<span class="muted">–</span>`;
  }
  function actionRow(label, name, a, ownId, allowRepeat = true) {
    const acts = Object.keys(ACTION_LABEL).filter(k => allowRepeat || k !== "repeat");
    const cur = a?.action || "";
    return `<div class="ln" data-act="${esc(name)}"><span style="width:130px">${label}</span>
      <select name="action" style="width:auto">${acts.map(k => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(ACTION_LABEL[k])}</option>`).join("")}</select>
      <span class="tgt" style="width:auto;flex:1">${targetField(cur, a?.target || "", ownId)}</span></div>`;
  }

  function openEditor(ivr) {
    const ed = $("#ivr-editor");
    const id = ivr ? ivr.id : null;
    const v = ivr || { name: "", number: "", timeout: 5, repeats: 2, direct: false, keys: {},
                       on_timeout: { action: "hangup" }, on_invalid: { action: "repeat" } };
    ed.innerHTML = `
      <form id="f-ivr" data-id="${id ?? ""}">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">${ivr ? `Menü „${esc(ivr.name)}“ bearbeiten` : "Neues Sprachmenü"}</label>
        <div class="row">
          <div><label>Name *</label><input name="name" required maxlength="40" value="${esc(v.name)}" placeholder="Hauptmenü"></div>
          <div><label>Nummer (leer = Untermenü)</label><input name="number" class="mono" value="${esc(v.number || "")}" placeholder="4961874191280"></div>
        </div>
        <div class="row">
          <div class="row" style="margin:0">
            <div><label>Warten auf Eingabe (s)</label><input name="timeout" type="number" min="1" max="30" value="${esc(v.timeout)}"></div>
            <div><label>Ansage höchstens (mal)</label><input name="repeats" type="number" min="1" max="5" value="${esc(v.repeats)}"></div>
          </div>
          <div><label class="check" style="margin-top:18px"><input type="checkbox" name="direct" ${v.direct ? "checked" : ""}>
            Durchwahl: Anrufer darf interne Rufnummern direkt wählen</label></div>
        </div>
        <label>Tasten</label>
        <div class="lines">${A.keys.map(k => actionRow(`Taste ${keyLabel(k)}`, `key:${k}`, v.keys[k], id)).join("")}</div>
        <label>Wenn nach den Wiederholungen …</label>
        <div class="lines">${actionRow("keine Eingabe", "on_timeout", v.on_timeout, id, false)}
          ${actionRow("falsche Eingabe", "on_invalid", v.on_invalid?.action === "repeat" ? { action: "hangup" } : v.on_invalid, id, false)}</div>
        <div class="hint">Bei falscher Eingabe sagt Asterisk „ungültig“ und spielt die Ansage noch einmal, bis die Höchstzahl erreicht ist.</div>
        <div class="actions"><button class="primary" type="submit">${ivr ? "Änderungen speichern" : "Anlegen"}</button>
          <button type="button" class="small" id="ivr-close">Schließen</button></div>
        <div class="msg" id="m-ivr"></div>
      </form>
      ${ivr ? `<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Ansage</label>
        <div class="actions" style="margin-bottom:10px">${ivr.greeting ? `<button type="button" class="small" data-ivr-play="${id}">▶ Aktuelle Ansage anhören</button>
          <button type="button" class="small danger" id="ivr-greet-del">Ansage löschen</button>` : '<span class="muted">Noch keine Ansage – es ertönt nur ein Piepton.</span>'}</div>
      </div>
      ${A.tts?.available ? `<form id="f-ivr-tts">
        <label>Text vorlesen lassen</label>
        <textarea name="text" rows="4" maxlength="1500" style="width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--bg2, transparent);color:inherit"
          placeholder="Willkommen bei … Für … drücken Sie die 1. Für … drücken Sie die 2.">${esc(ivr.tts?.text || "")}</textarea>
        <div class="row" style="margin-top:8px">
          <div><label>Stimme</label><select name="voice">${A.tts.voices.map(v =>
            `<option value="${esc(v.id)}" ${v.id === (ivr.tts?.voice || "de_DE-thorsten-medium") ? "selected" : ""}>${esc(v.label)}${v.installed ? "" : " – wird beim ersten Mal geladen"}</option>`).join("")}</select></div>
          <div><label>Tempo: <span id="tts-speed-v">${Math.round((ivr.tts?.speed || 1) * 100)} %</span></label>
            <input name="speed" type="range" min="0.7" max="1.4" step="0.05" value="${ivr.tts?.speed || 1}" style="width:100%"></div>
        </div>
        <div class="hint">Satzzeichen steuern die Pausen. Zahlen und Abkürzungen am besten ausschreiben („eins“, „zum Beispiel“).
          Beim ersten Gebrauch einer Stimme lädt der Server sie einmalig herunter (60–110 MB), das dauert etwas.</div>
        <div class="actions"><button type="button" id="tts-preview">▶ Probehören</button>
          <button type="submit" class="primary" id="tts-save">Als Ansage speichern</button></div>
        <div class="msg" id="m-ivr-tts"></div>
      </form>` : '<div class="hint">Die Sprachausgabe ist auf dem Server nicht installiert (piper-tts). Nach dem nächsten Neubau von cme-web ist sie da.</div>'}
      <form id="f-ivr-greet" style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
        <div class="row">
          <div><label>… oder eigene Aufnahme hochladen (MP3, WAV, M4A …)</label><input type="file" name="file" accept="audio/*" required></div>
          <div></div>
        </div>
        <div class="actions"><button type="submit" id="ivr-greet-submit">Hochladen</button></div>
        <div class="msg" id="m-ivr-greet"></div>
      </form>` : '<div class="hint" style="margin-top:12px">Die Ansage kannst du nach dem Anlegen hochladen.</div>'}`;
    ed.hidden = false;
    const sp = ed.querySelector("#f-ivr-tts [name=speed]");
    if (sp) sp.addEventListener("input", () => { $("#tts-speed-v").textContent = `${Math.round(sp.value * 100)} %`; });
    ed.querySelectorAll(".ln[data-act] select[name=action]").forEach(sel => sel.addEventListener("change", () => {
      sel.closest(".ln").querySelector(".tgt").innerHTML = targetField(sel.value, "", id);
    }));
    ed.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function readIvr(f) {
    const act = (ln) => ({ action: ln.querySelector("[name=action]").value, target: ln.querySelector("[name=target]")?.value || "" });
    const body = { name: f.name.value, number: f.number.value, timeout: f.timeout.value, repeats: f.repeats.value,
                   direct: f.direct.checked, keys: {} };
    f.querySelectorAll(".ln[data-act]").forEach(ln => {
      const n = ln.dataset.act;
      if (n.startsWith("key:")) body.keys[n.slice(4)] = act(ln);
      else body[n] = act(ln);
    });
    return body;
  }

  const ttsBody = (f) => ({ text: f.text.value, voice: f.voice.value, speed: f.speed.value });

  // ---------------- Laden ----------------

  async function loadApps(keepEditor = null) {
    const j = await api("/api/vmapps");
    if (!j.ok || !j.enabled) { box.hidden = true; return; }
    box.hidden = false;
    A = j;
    renderFax(); renderIvr();
    clearTimeout(pollTimer);
    if ((A.sent || []).some(j => j.status === "calling")) pollTimer = setTimeout(refreshFax, 5000);
    if (keepEditor !== null) {
      const ivr = A.ivrs.find(i => i.id === keepEditor);
      if (ivr) openEditor(ivr);
    }
  }

  // Nur die Fax-Liste nachladen, solange ein Fax unterwegs ist (Eingaben im Sende-Formular bleiben erhalten)
  async function refreshFax() {
    const j = await api("/api/vmapps");
    if (!j.ok || !j.enabled) return;
    const f = $("#f-faxsend"), keep = f ? { to: f.to.value, from: f.from.value, header: f.header.value } : null;
    const file = f && f.file.files.length ? f.file.files : null;
    A = j;
    renderFax();
    const g = $("#f-faxsend");
    if (keep && g) { g.to.value = keep.to; g.from.value = keep.from; g.header.value = keep.header; if (file) g.file.files = file; }
    if ((A.sent || []).some(x => x.status === "calling")) pollTimer = setTimeout(refreshFax, 5000);
  }

  // ---------------- Ereignisse ----------------

  box.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-fax") {
      const m = $("#m-fax"), edit = f.edit.value;
      const body = { number: f.number.value, name: f.name.value, email: f.email.value };
      const j = edit ? await api(`/api/vmapps/fax/${encodeURIComponent(edit)}`, { method: "PUT", body: JSON.stringify(body) })
                     : await api("/api/vmapps/fax", { method: "POST", body: JSON.stringify(body) });
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await loadApps();
      showMsg($("#m-fax"), true, "Gespeichert." + (j.config?.length ? `\n\n${j.config.join("\n")}` : ""));
    } else if (f.id === "f-faxsend") {
      const m = $("#m-faxsend"), btn = $("#faxsend-submit");
      lastSender = f.from.value;
      btn.disabled = true;
      showMsg(m, true, "PDF wird umgerechnet …");
      let j;
      try {
        const r = await fetch("/api/vmapps/fax/send", { method: "POST", body: new FormData(f), headers: { "X-CME-Request": "1" } });
        j = r.status === 413 ? { ok: false, error: "Datei zu groß." } : await r.json();
      } catch (err) { j = { ok: false, error: err.message }; }
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); await loadApps(); showMsg($("#m-faxsend"), false, j.error || "Fehler"); return; }
      await loadApps();
      showMsg($("#m-faxsend"), true, `Fax an ${j.job.to} (${j.job.pages} Seite${j.job.pages === 1 ? "" : "n"}) wird gesendet.`);
    } else if (f.id === "f-ivr") {
      const m = $("#m-ivr"), id = f.dataset.id;
      const j = id ? await api(`/api/vmapps/ivr/${id}`, { method: "PUT", body: JSON.stringify(readIvr(f)) })
                   : await api("/api/vmapps/ivr", { method: "POST", body: JSON.stringify(readIvr(f)) });
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await loadApps(j.id);
      showMsg($("#m-ivr"), true, "Gespeichert." + (j.config?.length ? `\n\n${j.config.join("\n")}` : "") +
        (id ? "" : "\n\nJetzt kannst du unten die Ansage hochladen."));
    } else if (f.id === "f-ivr-tts") {
      const m = $("#m-ivr-tts"), id = +$("#f-ivr").dataset.id, btn = $("#tts-save");
      btn.disabled = true;
      showMsg(m, true, "Wird gesprochen …");
      const j = await api(`/api/vmapps/ivr/${id}/greeting/tts`, { method: "POST", body: JSON.stringify(ttsBody(f)) });
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await loadApps(id);
      showMsg($("#m-ivr-tts"), true, "Ansage gespeichert.");
    } else if (f.id === "f-ivr-greet") {
      const m = $("#m-ivr-greet"), id = +$("#f-ivr").dataset.id, btn = $("#ivr-greet-submit");
      btn.disabled = true;
      showMsg(m, true, "Wird umgerechnet …");
      let j;
      try {
        const r = await fetch(`/api/vmapps/ivr/${id}/greeting`, { method: "POST", body: new FormData(f), headers: { "X-CME-Request": "1" } });
        j = r.status === 413 ? { ok: false, error: "Datei zu groß (höchstens 30 MB)." } : await r.json();
      } catch (err) { j = { ok: false, error: err.message }; }
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await loadApps(id);
      showMsg($("#m-ivr-greet"), true, "Ansage gespeichert.");
    }
  });

  box.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (e.target.id === "tts-preview") {
      const f = $("#f-ivr-tts"), m = $("#m-ivr-tts");
      e.target.disabled = true;
      showMsg(m, true, "Wird gesprochen …");
      try {
        const r = await fetch("/api/vmapps/tts/preview", { method: "POST", headers: { "Content-Type": "application/json" },
                                                           body: JSON.stringify(ttsBody(f)) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
        const url = URL.createObjectURL(await r.blob());
        if (player) player.pause();
        player = new Audio(url);
        m.className = "msg";
        await player.play();
      } catch (err) { showMsg(m, false, err.message); }
      e.target.disabled = false;
      return;
    }
    if (d.faxEdit) {
      const fx = A.fax.find(x => x.number === d.faxEdit), f = $("#f-fax");
      f.edit.value = fx.number; f.number.value = fx.number; f.name.value = fx.name; f.email.value = fx.email;
      $("#fax-title").textContent = `Faxnummer ${fx.number} bearbeiten`;
      $("#fax-submit").textContent = "Änderungen speichern";
      $("#fax-cancel").hidden = false;
      f.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (e.target.id === "fax-cancel") {
      endFaxEdit();
    } else if (d.faxDel) {
      if (!confirm(`Faxnummer ${d.faxDel} löschen? Der Dial-Peer am Router wird entfernt.`)) return;
      const j = await api(`/api/vmapps/fax/${encodeURIComponent(d.faxDel)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadApps();
    } else if (d.faxsentRetry) {
      e.target.disabled = true;
      const j = await api(`/api/vmapps/fax/sent/${encodeURIComponent(d.faxsentRetry)}/retry`, { method: "POST" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      loadApps();
    } else if (d.faxsentDel) {
      if (!confirm("Gesendetes Fax aus der Liste löschen?")) return;
      const j = await api(`/api/vmapps/fax/sent/${encodeURIComponent(d.faxsentDel)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadApps();
    } else if (d.faxfileDel) {
      if (!confirm("Fax löschen?")) return;
      const j = await api(`/api/vmapps/fax/files/${encodeURIComponent(d.faxfileDel)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadApps();
    } else if (e.target.id === "ivr-new") {
      openEditor(null);
    } else if (e.target.id === "ivr-close") {
      $("#ivr-editor").hidden = true;
    } else if (d.ivrEdit) {
      openEditor(A.ivrs.find(i => String(i.id) === d.ivrEdit));
    } else if (d.ivrDel) {
      const ivr = A.ivrs.find(i => String(i.id) === d.ivrDel);
      if (!confirm(`Menü „${ivr.name}“ löschen?${ivr.number ? " Der Dial-Peer am Router wird entfernt." : ""}`)) return;
      const j = await api(`/api/vmapps/ivr/${d.ivrDel}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadApps();
    } else if (d.ivrPlay) {
      if (player) player.pause();
      player = new Audio(`/api/vmapps/ivr/${d.ivrPlay}/greeting?t=${Date.now()}`);
      player.play().catch(() => alert("Abspielen nicht möglich."));
    } else if (e.target.id === "ivr-greet-del") {
      const id = +$("#f-ivr").dataset.id;
      if (!confirm("Ansage löschen?")) return;
      const j = await api(`/api/vmapps/ivr/${id}/greeting`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      loadApps(id);
    }
  });

  loadApps();
})();
