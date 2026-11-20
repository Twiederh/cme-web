// Gespraechsaufzeichnung ueber den CUBE (SIPREC, Container cme-recorder).
// Eigene Karte (Tab "Aufzeichnung"); nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let R = { enabled: false };
  let player = null, query = "", openRow = null;

  // Karte legt voicemail.js an (damit sie beim Aufbau der Tabs schon da ist)
  let card = document.getElementById("recording-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "recording-card";
    const anchor = document.getElementById("raw-out")?.closest("section.card");
    (anchor ? anchor.parentNode.insertBefore(card, anchor) : document.querySelector("main").appendChild(card));
  }
  if (card.dataset.ready) return;
  card.dataset.ready = "1";
  card.innerHTML = `<h2>Gesprächsaufzeichnung <span class="count" id="c-rec"></span>
      <span class="right muted" id="rec-version" style="font-weight:400;font-size:12px"></span></h2>
    <div id="rec-body"><div class="empty">Lädt …</div></div>`;

  const h2 = (t, extra = "") => `<h2 style="font-size:14px;margin:0;padding:12px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px">${t}${extra}</h2>`;
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
  const fmtDur = (s) => { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const fmtSize = (b) => b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round((b || 0) / 1024)} KB`;
  const numName = (num) => { const n = (R.numbers || []).find(x => x.number === num); return n && n.name ? `${num} (${n.name})` : num; };
  const DIR = { in: "↘ eingehend", out: "↗ ausgehend" };
  const DEFAULT_NOTICE = "Dieses Gespräch wird aufgezeichnet. Wenn Sie damit nicht einverstanden sind, beenden Sie bitte jetzt den Anruf.";

  function render() {
    const body = $("#rec-body");
    if (!R.enabled) {
      body.innerHTML = `<div class="body"><div class="hint">Die Aufzeichnung braucht den Asterisk-Teil (Voicemail):
        im Stack <span class="mono">VM_HOST_IP</span> und <span class="mono">VM_AMI_SECRET</span> setzen.</div></div>`;
      return;
    }
    const st = R.router, s = R.settings, rc = R.recorder;
    $("#c-rec").textContent = R.recordings.length ? `(${R.recordings.length})` : "";
    $("#rec-version").textContent = rc.online ? `cme-recorder ${rc.version}` : "";
    const active = (rc.active || []).length;
    const tabN = document.getElementById("n-recording");
    if (tabN) tabN.innerHTML = active ? ` ${badge("bad", "● läuft")}` : "";
    const recPeers = st.peers.filter(p => p.recorded);

    body.innerHTML = `
      <div class="body">
        <div class="actions" style="flex-wrap:wrap;gap:6px;margin-bottom:10px">
          ${rc.online ? badge("ok", `Recorder läuft${active ? ` · ${active} Gespräch${active > 1 ? "e" : ""} aktiv` : ""}`) : badge("bad", "Recorder nicht erreichbar")}
          ${st.configured ? badge("ok", `Router: Aufzeichnung an Dial-Peer ${recPeers.map(p => p.tag).join(", ") || "–"}`) : badge("warn", "Router noch nicht eingerichtet")}
          ${s.enabled ? "" : badge("warn", "pausiert")}
          ${s.notice ? (R.notice.exists ? badge("ok", "Ansage vorhanden") : badge("warn", "Ansage fehlt")) : badge("neutral", "ohne Ansage")}
        </div>
        ${rc.online ? "" : `<div class="hint">Der Container <b>cme-recorder</b> meldet sich nicht. Er braucht auf dem Docker-Host
          UDP/TCP ${R.host_ip}:5070 und UDP 10100–10139 (im Stack enthalten, nach dem Update neu deployen).</div>`}
        ${R.router_error ? `<div class="msg bad" style="display:block">${esc(R.router_error)}</div>` : ""}
        <div class="hint">Der Router kopiert die Sprache der Gespräche über die gewählten Dial-Peers zum Provider (SIPREC) und schickt
          sie an den Recorder. Aufgezeichnet werden damit alle externen Gespräche, interne Gespräche und Anrufe an Voicemail/IVR nicht.
          Jede Aufnahme ist Stereo: links die interne Seite, rechts die Gegenseite.
          <br><b>Rechtlich:</b> Gespräche dürfen nur mit Einwilligung aller Beteiligten aufgezeichnet werden (§ 201 StGB).</div>
      </div>

      ${activePart()}

      ${h2("Einrichtung am Router")}
      <div class="body">
        <table><thead><tr><th></th><th>Dial-Peer</th><th>Beschreibung</th><th>Ziel / eingehend</th><th>Status</th></tr></thead><tbody>
          ${st.peers.map(p => `<tr><td><input type="checkbox" name="rec-peer" value="${esc(p.tag)}"
              ${p.recorded || (!st.configured && /sipgate|trunk|provider|telekom|easybell/i.test(`${p.desc} ${p.target}`)) ? "checked" : ""}></td>
            <td class="mono">${esc(p.tag)}</td><td>${esc(p.desc) || '<span class="muted">–</span>'}</td>
            <td class="mono">${esc(p.target || p.pattern || "")}${p.incoming ? `<br><span class="muted">${esc(p.incoming)}</span>` : ""}</td>
            <td>${p.recorded ? badge("ok", "wird aufgezeichnet") : (p.media_class ? badge("neutral", `media-class ${p.media_class}`) : "")}</td></tr>`).join("")
            || `<tr><td colspan="5" class="empty">Keine VoIP-Dial-Peers gefunden.</td></tr>`}
        </tbody></table>
        <div class="hint" style="margin-top:8px">Die Dial-Peers zum Provider auswählen (meist der sipgate-Trunk für eingehende und ausgehende Gespräche).
          Die Seite legt an: einen Dial-Peer zum Recorder${st.rec_peer ? ` (${st.rec_peer})` : ""}, <span class="mono">media profile recorder</span> und
          <span class="mono">media class … siprec</span>, einen Dial-Peer für Anrufe von Asterisk${st.ast_peer ? ` (${st.ast_peer})` : ""}
          (damit Gespräche über Asterisk nicht doppelt aufgezeichnet werden)${s.notice ? ` und den Dial-Peer <span class="mono">${esc(s.prefix)}T</span> → Asterisk für die Ansage` : ""}.</div>
        <div class="actions" style="margin-top:10px">
          <button class="primary" id="rec-setup">${st.configured ? "Router-Einstellungen erneut setzen" : "Am Router einrichten"}</button>
          ${st.configured ? `<button class="danger" id="rec-remove">Aufzeichnung am Router entfernen</button>` : ""}
        </div>
        <div class="msg" id="m-rec-router"></div>
      </div>

      ${h2("Einstellungen")}
      <div class="body">
        <form id="f-recset">
          <div class="row">
            <div><label class="check"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> Aufzeichnen (aus = pausiert, der Router bleibt eingerichtet)</label>
              <label style="margin-top:6px">Aufnahme je Gespräch</label>
              <select name="mode">
                <option value="always" ${s.mode !== "demand" ? "selected" : ""}>immer – läuft ab Gesprächsbeginn, lässt sich ausschalten</option>
                <option value="demand" ${s.mode === "demand" ? "selected" : ""}>nur auf Wunsch – startet aus, wird bei Bedarf eingeschaltet</option>
              </select>
              <label class="check" style="margin-top:6px"><input type="checkbox" name="transcript" ${s.transcript ? "checked" : ""}> Abschrift erstellen (Whisper, lokal)</label>
              <label class="check"><input type="checkbox" name="mail" ${s.mail ? "checked" : ""} ${R.mail_ready ? "" : "disabled"}> Aufnahme per E-Mail schicken
                ${R.mail_ready ? "" : '<span class="muted">(VM_SMTP_USER fehlt)</span>'}</label></div>
            <div class="row" style="margin:0">
              <div><label>Aufbewahren (Tage, 0 = immer)</label><input name="retention_days" type="number" min="0" max="3650" value="${esc(s.retention_days)}"></div>
              <div><label>Kürzer als (s) verwerfen</label><input name="min_secs" type="number" min="0" max="60" value="${esc(s.min_secs)}"></div>
            </div>
          </div>
          <label>E-Mail je interner Rufnummer</label>
          <table><thead><tr><th>Rufnummer</th><th>E-Mail</th></tr></thead><tbody>
            ${(R.numbers || []).map(n => `<tr><td class="mono">${esc(numName(n.number))}</td>
              <td><input name="email:${esc(n.number)}" type="email" value="${esc(s.emails[n.number] || "")}"
                placeholder="${esc(R.vm_emails[n.number] || "")}" style="width:100%"></td></tr>`).join("")}
            ${Object.keys(s.emails).filter(k => !(R.numbers || []).some(n => n.number === k)).map(k => `<tr><td class="mono">${esc(k)}</td>
              <td><input name="email:${esc(k)}" type="email" value="${esc(s.emails[k])}" style="width:100%"></td></tr>`).join("")}
          </tbody></table>
          <div class="row" style="margin-top:10px">
            <div><label>Sonst an (Standard-Empfänger)</label><input name="default_email" type="email" value="${esc(s.default_email)}" placeholder="name@icloud.com"></div>
            <div></div>
          </div>
          <div class="hint">Die Mail geht an die Adresse der internen Rufnummer, die am Gespräch beteiligt war.
            Ein leeres Feld nimmt die E-Mail-Adresse des Voicemail-Postfachs (grau angezeigt).
            Wer intern ist, erkennt der Recorder an den Rufnummern der Telefone und den eigenen Nummern beim Provider
            ${R.settings.own?.length ? `(<span class="mono">${R.settings.own.map(esc).join(", ")}</span>)` : ""}.</div>

          <label style="margin-top:14px">Ansage vor eingehenden Gesprächen</label>
          <div class="row">
            <div><label class="check"><input type="checkbox" name="notice" ${s.notice ? "checked" : ""}> Anrufer von außen hören vor dem Klingeln den Hinweis</label></div>
            <div><label>Vorangestellte Nummer</label><input name="prefix" class="mono" value="${esc(s.prefix)}" pattern="[0-9]{2,6}"></div>
          </div>
          <div class="actions"><button class="primary" type="submit">Einstellungen speichern</button></div>
          <div class="msg" id="m-recset"></div>
        </form>
      </div>
      ${s.notice ? noticePart() : ""}

      ${h2("Aufzeichnungen", `<input id="rec-q" placeholder="Suchen (Nummer, Name, Text) …" value="${esc(query)}" style="margin-left:auto;font-weight:400;max-width:260px">`)}
      ${R.recordings.length ? `<table><thead><tr><th>Zeit</th><th>Richtung</th><th>Intern</th><th>Extern</th><th>Dauer</th><th>Abschrift / Mail</th><th></th></tr></thead><tbody>
        ${R.recordings.map(r => `<tr><td>${fmtTime(r.start)}</td><td>${esc(DIR[r.direction] || "–")}</td>
          <td class="mono">${esc(numName(r.internal) || "–")}</td><td class="mono">${esc(r.external || "–")}</td>
          <td>${fmtDur(r.duration)} <span class="muted">${fmtSize(r.size)}</span>${r.partial ? ` <span title="Während des Gesprächs aus- oder eingeschaltet; Pausen sind Stille">${badge("neutral", "teilweise")}</span>` : ""}</td>
          <td>${{ ok: badge("ok", "Abschrift"), pending: badge("neutral", "Abschrift läuft …"), failed: badge("bad", "Abschrift fehlgeschlagen") }[r.transcript_status] || ""}
            ${r.mailed ? `<span class="muted" title="${esc(r.mailed)}">✉</span>` : (r.mail_error ? `<span title="${esc(r.mail_error)}">${badge("warn", "Mail fehlgeschlagen")}</span>` : "")}</td>
          <td><div class="actions">
            <button class="small" data-rec-play="${esc(r.id)}">▶</button>
            ${r.transcript ? `<button class="small" data-rec-text="${esc(r.id)}">Text</button>` : ""}
            <a class="btn" title="Herunterladen" style="text-decoration:none;padding:2px 8px;font-size:12px" href="/api/recording/files/${esc(r.id)}?download=1">⤓</a>
            <button class="small danger" data-rec-del="${esc(r.id)}">Löschen</button></div></td></tr>
          <tr data-rec-row="${esc(r.id)}" ${openRow === r.id ? "" : "hidden"}><td colspan="7">
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Links: ${esc(r.channels?.[0] || "–")} · Rechts: ${esc(r.channels?.[1] || "–")} · ${esc(r.codec || "")}</div>
            <div style="white-space:pre-wrap;font-size:13px;line-height:1.5">${esc(r.transcript || "")}</div></td></tr>`).join("")}
        </tbody></table>` : `<div class="empty">${query ? "Nichts gefunden." : "Noch keine Aufzeichnungen."}</div>`}
      ${rc.log?.length ? `<details style="padding:10px 16px;border-top:1px solid var(--line)"><summary class="muted" style="cursor:pointer">Protokoll des Recorders</summary>
        <pre class="mono" style="font-size:11px;white-space:pre-wrap;margin:8px 0 0">${esc(rc.log.join("\n"))}</pre></details>` : ""}`;

    const sp = body.querySelector("#f-rec-tts [name=speed]");
    if (sp) sp.addEventListener("input", () => { $("#rec-speed-v").textContent = `${Math.round(sp.value * 100)} %`; });
    const q = $("#rec-q");
    let timer = null;
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { query = q.value; load(true); }, 400); });
  }

  // Laufende Gespraeche: Aufnahme ein-/ausschalten
  function activePart() {
    const list = R.active || [];
    if (!list.length) return "";
    const since = (t) => fmtDur(Date.now() / 1000 - t);
    return `${h2("Laufende Gespräche")}
      <table><thead><tr><th>Seit</th><th>Beteiligte</th><th>Aufnahme</th><th></th></tr></thead><tbody>
        ${list.map(a => `<tr><td>${since(a.start)}</td>
          <td class="mono">${(a.numbers || []).map(n => esc(numName(n))).join("<br>") || "–"}</td>
          <td>${a.recording ? badge("bad", "● läuft") : badge("neutral", "○ aus")}</td>
          <td><button class="small ${a.recording ? "danger" : "primary"}" data-rec-toggle="${esc(a.id)}" data-on="${a.recording ? "0" : "1"}">
            ${a.recording ? "Aufnahme stoppen" : "Aufnahme starten"}</button></td></tr>`).join("")}
      </tbody></table>
      <div class="body" style="padding-top:6px"><div class="hint">Ausgeschaltete Abschnitte werden in der Aufnahme zu Stille.
        Am Telefon geht das auch: Taste <b>Dienste</b> → „Aufnahme … ein/aus“ (Telefondienst wie bei der Visual Voicemail).</div></div>`;
  }

  function noticePart() {
    const s = R.settings, t = s.notice_tts || {}, p = esc(s.prefix);
    return `${h2("Ansage")}
      <div class="body">
        <div class="hint"><b>So kommen eingehende Anrufe an der Ansage vorbei:</b> im Tab <a href="#translation">Übersetzungen</a>
          die Regel für eingehende Anrufe so ändern, dass vor die interne Nummer <span class="mono">${p}</span> gesetzt wird, z. B.
          <span class="mono">/^\\+49301234567\\(..\\)$/</span> → <span class="mono">/${p}\\1/</span> statt <span class="mono">/\\1/</span>.
          Der Router gibt den Anruf dann an Asterisk, das spielt die Ansage (ohne den Anruf anzunehmen) und verbindet zur Nummer dahinter.
          Faxnummern und IVR-Nummern ohne ${p} lassen.</div>
        <div class="actions" style="margin:10px 0">${R.notice.exists ? `<button type="button" class="small" id="rec-notice-play">▶ Aktuelle Ansage anhören</button>
          <button type="button" class="small danger" id="rec-notice-del">Ansage löschen</button>` : '<span class="muted">Noch keine Ansage – Anrufe gehen ohne Hinweis durch.</span>'}</div>
        ${R.tts?.available ? `<form id="f-rec-tts">
          <label>Text vorlesen lassen</label>
          <textarea name="text" rows="3" maxlength="1500" style="width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:7px;background:transparent;color:inherit">${esc(t.text || DEFAULT_NOTICE)}</textarea>
          <div class="row" style="margin-top:8px">
            <div><label>Stimme</label><select name="voice">${R.tts.voices.map(v =>
              `<option value="${esc(v.id)}" ${v.id === (t.voice || "de_DE-thorsten-medium") ? "selected" : ""}>${esc(v.label)}${v.installed ? "" : " – wird beim ersten Mal geladen"}</option>`).join("")}</select></div>
            <div><label>Tempo: <span id="rec-speed-v">${Math.round((t.speed || 1) * 100)} %</span></label>
              <input name="speed" type="range" min="0.7" max="1.4" step="0.05" value="${t.speed || 1}" style="width:100%"></div>
          </div>
          <div class="actions"><button type="button" id="rec-tts-preview">▶ Probehören</button>
            <button type="submit" class="primary" id="rec-tts-save">Als Ansage speichern</button></div>
          <div class="msg" id="m-rec-tts"></div>
        </form>` : ""}
        <form id="f-rec-notice" style="margin-top:12px">
          <div class="row"><div><label>… oder eigene Aufnahme hochladen</label><input type="file" name="file" accept="audio/*" required></div><div></div></div>
          <div class="actions"><button type="submit">Hochladen</button></div>
          <div class="msg" id="m-rec-notice"></div>
        </form>
      </div>`;
  }

  const ttsBody = (f) => ({ text: f.text.value, voice: f.voice.value, speed: f.speed.value });

  async function load(keep = false) {
    const j = await api(`/api/recording?q=${encodeURIComponent(query)}`);
    if (!j.ok) { $("#rec-body").innerHTML = `<div class="body"><div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div></div>`; return; }
    const focus = document.activeElement?.id === "rec-q";
    R = j;
    render();
    if (focus) { const q = $("#rec-q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  }

  function play(url) {
    if (player) player.pause();
    player = new Audio(url);
    player.play().catch(() => alert("Abspielen nicht möglich."));
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "f-recset") {
      const emails = {};
      f.querySelectorAll("input[name^='email:']").forEach(i => { if (i.value.trim()) emails[i.name.slice(6)] = i.value.trim(); });
      const body = { enabled: f.enabled.checked, transcript: f.transcript.checked, mail: f.mail.checked,
                     retention_days: f.retention_days.value, min_secs: f.min_secs.value, emails, mode: f.mode.value,
                     default_email: f.default_email.value, notice: f.notice.checked, prefix: f.prefix.value };
      const j = await api("/api/recording/settings", { method: "PUT", body: JSON.stringify(body) });
      if (!j.ok) { showMsg($("#m-recset"), false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-recset"), true, "Gespeichert." + (j.router_change ? "\nBitte oben „Router-Einstellungen erneut setzen“, damit der Dial-Peer für die Ansage passt." : ""));
    } else if (f.id === "f-rec-tts") {
      const m = $("#m-rec-tts"), btn = $("#rec-tts-save");
      btn.disabled = true; showMsg(m, true, "Wird gesprochen …");
      const j = await api("/api/recording/notice/tts", { method: "POST", body: JSON.stringify(ttsBody(f)) });
      btn.disabled = false;
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-rec-tts"), true, "Ansage gespeichert.");
    } else if (f.id === "f-rec-notice") {
      const m = $("#m-rec-notice");
      showMsg(m, true, "Wird umgerechnet …");
      let j;
      try {
        const r = await fetch("/api/recording/notice", { method: "POST", body: new FormData(f), headers: { "X-CME-Request": "1" } });
        j = await r.json();
      } catch (err) { j = { ok: false, error: err.message }; }
      if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-rec-notice"), true, "Ansage gespeichert.");
    }
  });

  card.addEventListener("click", async (e) => {
    const t = e.target, d = t.dataset || {};
    if (t.id === "rec-setup") {
      const peers = [...card.querySelectorAll("input[name=rec-peer]:checked")].map(i => i.value);
      if (!peers.length) { showMsg($("#m-rec-router"), false, "Mindestens einen Dial-Peer auswählen."); return; }
      if (!confirm(`Aufzeichnung am Router einrichten?\n\n• media class / media profile recorder (SIPREC) an Dial-Peer ${peers.join(", ")}\n` +
                   `• Dial-Peer zum Recorder ${R.host_ip}:5070\n• Dial-Peer für Anrufe von Asterisk (incoming uri via)` +
                   (R.settings.notice ? `\n• Dial-Peer ${R.settings.prefix}T → Asterisk (Ansage)` : ""))) return;
      t.disabled = true;
      const j = await api("/api/recording/router", { method: "POST", body: JSON.stringify({ peers }) });
      t.disabled = false;
      if (!j.ok) { showMsg($("#m-rec-router"), false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-rec-router"), true, "Eingerichtet.\n\n" + (j.config || []).join("\n"));
    } else if (t.id === "rec-remove") {
      if (!confirm("Aufzeichnung am Router entfernen? Die vorhandenen Aufnahmen bleiben erhalten.")) return;
      t.disabled = true;
      const j = await api("/api/recording/router", { method: "DELETE" });
      t.disabled = false;
      if (!j.ok) { showMsg($("#m-rec-router"), false, j.error || "Fehler"); return; }
      await load();
      showMsg($("#m-rec-router"), true, "Entfernt.\n\n" + (j.config || []).join("\n"));
    } else if (d.recToggle) {
      t.disabled = true;
      const j = await api(`/api/recording/active/${d.recToggle}`, { method: "POST", body: JSON.stringify({ on: d.on === "1" }) });
      if (!j.ok) { t.disabled = false; alert(j.error || "Fehler"); return; }
      await load();
    } else if (d.recPlay) {
      play(`/api/recording/files/${d.recPlay}`);
    } else if (d.recText) {
      openRow = openRow === d.recText ? null : d.recText;
      card.querySelectorAll("tr[data-rec-row]").forEach(r => r.hidden = r.dataset.recRow !== openRow);
    } else if (d.recDel) {
      if (!confirm("Aufnahme löschen?")) return;
      const j = await api(`/api/recording/files/${d.recDel}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    } else if (t.id === "rec-notice-play") {
      play(`/api/recording/notice?t=${Date.now()}`);
    } else if (t.id === "rec-notice-del") {
      if (!confirm("Ansage löschen? Anrufe gehen dann ohne Hinweis durch.")) return;
      await api("/api/recording/notice", { method: "DELETE" });
      load();
    } else if (t.id === "rec-tts-preview") {
      const f = $("#f-rec-tts"), m = $("#m-rec-tts");
      t.disabled = true; showMsg(m, true, "Wird gesprochen …");
      try {
        const r = await fetch("/api/vmapps/tts/preview", { method: "POST", headers: { "Content-Type": "application/json" },
                                                           body: JSON.stringify(ttsBody(f)) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
        play(URL.createObjectURL(await r.blob()));
        m.className = "msg";
      } catch (err) { showMsg(m, false, err.message); }
      t.disabled = false;
    }
  });

  load();
  // Liste regelmaessig auffrischen, aber nicht waehrend der Eingabe
  setInterval(() => {
    const a = document.activeElement;
    if (!document.hidden && card.offsetParent !== null && !(a && card.contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName))) load();
  }, 30000);
  // waehrend Gespraechen oefter, damit der Ein/Aus-Zustand stimmt
  setInterval(() => {
    const a = document.activeElement;
    if ((R.active || []).length && !document.hidden && card.offsetParent !== null
        && !(a && card.contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName))) load();
  }, 5000);
})();
