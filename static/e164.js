// E.164-Pattern-Maps (voice class e164-pattern-map): anzeigen, anlegen, loeschen, Eintraege bearbeiten.
// Eigene Karte im Tab "Uebersetzungen"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let M = [];
  let open = null;            // aufgeklappte Map
  let testNum = "";

  const KIND = { "destination": "Ziel (destination)", "incoming called": "eingehend, angerufene Nr.",
                 "incoming calling": "eingehend, Anrufer" };

  // Die Karte legt tabs.js an (damit sie beim Aufbau der Tabs schon da ist)
  let card = document.getElementById("e164-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "e164-card";
    const anchor = document.getElementById("translation-card");
    (anchor ? anchor.parentNode.insertBefore(card, anchor.nextSibling) : document.querySelector("main").appendChild(card));
  }
  card.innerHTML = `
    <h2>E.164-Pattern-Maps <span class="count" id="c-e164"></span>
      <span class="right muted" style="font-weight:400;font-size:12px">voice class e164-pattern-map</span></h2>
    <div class="body" style="padding-bottom:0">
      <div class="row">
        <div><label>Nummer prüfen: welche Muster passen?</label>
          <input id="e164-test" class="mono" placeholder="+4961874191283" autocomplete="off"></div>
        <div id="e164-test-out" class="mono" style="align-self:end;padding-bottom:8px"></div>
      </div>
    </div>
    <table><thead><tr><th>Map</th><th>Beschreibung</th><th>Muster</th><th>Verwendet von</th><th></th></tr></thead>
      <tbody id="t-e164"></tbody></table>
    <div class="body" style="border-top:1px solid var(--line)">
      <form id="f-e164new">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Neue Pattern Map</label>
        <div class="row">
          <div class="row" style="margin:0">
            <div><label>Nummer</label><input name="tag" type="number" min="1" max="10000" placeholder="automatisch"></div>
            <div><label>Beschreibung</label><input name="description" maxlength="80" placeholder="z. B. Eigene Rufnummern"></div>
          </div>
          <div><label>Muster (eins pro Zeile)</label>
            <textarea name="entries" rows="3" class="mono" placeholder="+4961874191[2-9]..&#10;0T"
              style="width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:7px;background:transparent;color:inherit"></textarea></div>
        </div>
        <div class="hint">Muster wie bei <span class="mono">destination-pattern</span>: <span class="mono">.</span> eine Ziffer,
          <span class="mono">T</span> beliebig viele Ziffern, <span class="mono">[2-9]</span> Bereich, ein <span class="mono">+</span> am Anfang
          steht für das Pluszeichen. Verwendet wird eine Map im Dial-Peer mit <span class="mono">destination e164-pattern-map N</span>
          oder <span class="mono">incoming called|calling e164-pattern-map N</span>.</div>
        <div class="actions"><button class="primary" type="submit">Anlegen</button></div>
        <div class="msg" id="m-e164new"></div>
      </form>
    </div>`;

  // IOS-Muster (dial-peer-Syntax) -> JS-Regex, nur fuer die Vorschau im Browser
  function toRegex(p) {
    let out = "^", i = 0;
    if (p.startsWith("+")) { out += "\\+"; i = 1; }
    if (p.startsWith("^")) { i = 1; }
    for (; i < p.length; i++) {
      const c = p[i];
      if (c === "[") {
        const j = p.indexOf("]", i);
        if (j < 0) return null;
        out += p.slice(i, j + 1).replace(/^\[\^/, "[^");
        i = j;
      } else if (c === ".") out += "[0-9*#A-D]";
      else if (c === "T") out += "[0-9*#A-D]*";
      else if (c === "%") out += "*";
      else if (c === "?") out += "?";
      else if (c === "+") out += "+";
      else if (c === "$") out += "$";
      else if (/[0-9A-D]/.test(c)) out += c;
      else if (c === "*" || c === "#") out += "\\" + c;
      else if (c === ",") continue;
      else return null;
    }
    // ohne T und ohne $: IOS vergleicht die ganze Nummer (gleiche Laenge)
    return new RegExp(out.endsWith("$") ? out : out + "$");
  }
  const matches = (p, n) => { try { const r = toRegex(p); return r ? r.test(n) : false; } catch { return false; } };

  function render() {
    $("#c-e164").textContent = M.length ? `(${M.length})` : "";
    const n = testNum.trim();
    let hits = 0;
    $("#t-e164").innerHTML = M.map(m => {
      const hit = n ? m.entries.filter(e => matches(e, n)) : [];
      hits += hit.length ? 1 : 0;
      const isOpen = open === m.tag;
      const shown = isOpen ? m.entries : m.entries.slice(0, 6);
      return `<tr>
        <td class="mono">${esc(m.tag)} ${hit.length ? badge("ok", "passt") : ""}</td>
        <td>${esc(m.description) || '<span class="muted">–</span>'}</td>
        <td class="mono">${m.url ? `<span class="muted">aus Datei:</span> ${esc(m.url)}` :
          shown.map(e => hit.includes(e) ? `<b style="color:var(--ok, #1a7f37)">${esc(e)}</b>` : esc(e)).join("<br>") +
          (!isOpen && m.entries.length > 6 ? `<br><span class="muted">… und ${m.entries.length - 6} weitere</span>` : "")}</td>
        <td>${m.used.length ? m.used.map(u => `Dial-Peer <span class="mono">${esc(u.peer)}</span> <span class="muted">${esc(KIND[u.kind] || u.kind)}</span>`).join("<br>")
              : '<span class="muted">nicht verwendet</span>'}</td>
        <td><div class="actions"><button class="small" data-e164-open="${esc(m.tag)}">${isOpen ? "Schließen" : "Bearbeiten"}</button>
          <button class="small danger" data-e164-del="${esc(m.tag)}" ${m.used.length ? 'disabled title="Wird noch verwendet"' : ""}>Löschen</button></div></td></tr>
        ${isOpen ? `<tr><td colspan="5"><div style="padding:4px 0 8px">
          ${m.url ? `<div class="hint">Diese Map wird aus der Datei <span class="mono">${esc(m.url)}</span> geladen und lässt sich hier nicht bearbeiten.</div>` : `
          <form class="f-e164desc" data-tag="${esc(m.tag)}">
            <div class="row"><div><label>Beschreibung</label><input name="description" maxlength="80" value="${esc(m.description)}"></div>
              <div style="align-self:end"><button class="small" type="submit">Beschreibung speichern</button></div></div>
          </form>
          <label style="margin-top:8px">Muster</label>
          <table style="margin:4px 0 8px"><tbody>${m.entries.map(e => `<tr>
            <td class="mono">${hit.includes(e) ? `<b>${esc(e)}</b> ${badge("ok", "passt")}` : esc(e)}</td>
            <td style="width:1%"><button class="small danger" data-e164-rm="${esc(m.tag)}" data-pattern="${esc(e)}">Entfernen</button></td></tr>`).join("")}
          </tbody></table>
          <form class="f-e164add" data-tag="${esc(m.tag)}">
            <div class="row"><div><label>Muster hinzufügen (mehrere mit Leerzeichen oder Zeilen trennen)</label>
              <input name="entries" class="mono" placeholder="+4961874191299"></div>
              <div style="align-self:end"><button class="small primary" type="submit">Hinzufügen</button></div></div>
          </form>`}
          <div class="msg" id="m-e164-${esc(m.tag)}"></div></div></td></tr>` : ""}`;
    }).join("") || `<tr><td colspan="5" class="empty">Keine Pattern Maps.</td></tr>`;
    $("#e164-test-out").innerHTML = !n ? "" : hits ? badge("ok", `passt zu ${hits} Map${hits > 1 ? "s" : ""}`)
      : badge("neutral", "passt zu keiner Map");
  }

  async function loadE() {
    const j = await api("/api/e164");
    if (!j.ok) { $("#t-e164").innerHTML = `<tr><td colspan="5" class="empty">${esc(j.error || "Fehler")}</td></tr>`; return; }
    M = j.maps;
    render();
  }

  const msgFor = (tag) => document.getElementById(`m-e164-${tag}`);
  const done = (tag, j, text) => {
    if (!j.ok) { showMsg(msgFor(tag), false, j.error || "Fehler"); return false; }
    return loadE().then(() => { const m = msgFor(tag); if (m) showMsg(m, true, text + (j.config?.length ? `\n\n${j.config.join("\n")}` : "")); });
  };

  $("#e164-test").addEventListener("input", (e) => { testNum = e.target.value; render(); });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    if (btn) btn.disabled = true;
    try {
      if (f.id === "f-e164new") {
        const j = await api("/api/e164/maps", { method: "POST", body: JSON.stringify({
          tag: f.tag.value, description: f.description.value, entries: f.entries.value }) });
        if (!j.ok) { showMsg($("#m-e164new"), false, j.error || "Fehler"); return; }
        f.reset();
        open = j.tag;
        await loadE();
        showMsg($("#m-e164new"), true, `Pattern Map ${j.tag} angelegt.\n\n${(j.config || []).join("\n")}`);
      } else if (f.classList.contains("f-e164desc")) {
        const tag = f.dataset.tag;
        await done(tag, await api(`/api/e164/maps/${tag}`, { method: "PUT", body: JSON.stringify({ description: f.description.value }) }),
                   "Beschreibung gespeichert.");
      } else if (f.classList.contains("f-e164add")) {
        const tag = f.dataset.tag;
        await done(tag, await api(`/api/e164/maps/${tag}/entries`, { method: "POST", body: JSON.stringify({ entries: f.entries.value }) }),
                   "Hinzugefügt.");
      }
    } finally { if (btn && document.body.contains(btn)) btn.disabled = false; }
  });

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.e164Open) {
      open = open === d.e164Open ? null : d.e164Open;
      render();
    } else if (d.e164Rm) {
      if (!confirm(`Muster ${d.pattern} aus Pattern Map ${d.e164Rm} entfernen?`)) return;
      e.target.disabled = true;
      await done(d.e164Rm, await api(`/api/e164/maps/${d.e164Rm}/entries`, { method: "DELETE", body: JSON.stringify({ pattern: d.pattern }) }),
                 "Entfernt.");
    } else if (d.e164Del) {
      if (!confirm(`Pattern Map ${d.e164Del} löschen?`)) return;
      e.target.disabled = true;
      const j = await api(`/api/e164/maps/${d.e164Del}`, { method: "DELETE" });
      if (!j.ok) { e.target.disabled = false; alert(j.error || "Fehler"); return; }
      if (open === d.e164Del) open = null;
      loadE();
    }
  });

  loadE();
})();
