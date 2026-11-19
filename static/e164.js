// E.164-Pattern-Maps (voice class e164-pattern-map): anzeigen, anlegen, loeschen, Eintraege bearbeiten.
// Eigene Karte im Tab "Uebersetzungen"; nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let M = [];
  let open = null;            // aufgeklappte Map
  let testNum = "";

  const KIND = { "destination calling": "Absender (destination calling)", "destination": "Ziel (destination)", "incoming called": "eingehend, angerufene Nr.",
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
  // Wie genau ein Muster ist (wie bei der Dial-Peer-Auswahl: die laengste feste Uebereinstimmung gewinnt):
  // ohne T vor mit T, dann mehr feste Ziffern, dann mehr Bereiche [..] statt beliebiger Ziffern
  function score(p) {
    const fixed = (p.replace(/\[[^\]]*\]/g, "").match(/[0-9A-D*#+]/g) || []).length;
    const ranges = (p.match(/\[[^\]]*\]/g) || []).length;
    return [/[T%]/.test(p) ? 0 : 1, fixed, ranges];
  }
  const cmp = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];

  function render() {
    $("#c-e164").textContent = M.length ? `(${M.length})` : "";
    const n = testNum.trim();
    // nur die genauesten passenden Muster markieren, die ungenaueren kommen ohnehin nicht zum Zug
    let best = null;
    if (n) for (const m of M) for (const e of m.entries) {
      if (matches(e, n) && (!best || cmp(score(e), best) > 0)) best = score(e);
    }
    const isBest = (e) => best && matches(e, n) && cmp(score(e), best) === 0;
    let hits = 0;
    $("#t-e164").innerHTML = M.map(m => {
      const hit = n ? m.entries.filter(isBest) : [];
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
    const where = M.filter(m => m.entries.some(isBest)).map(m => `Map ${m.tag}`).join(", ");
    $("#e164-test-out").innerHTML = !n ? "" : hits ? badge("ok", `passt am genauesten: ${where}`)
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

  window.loadE164 = loadE;
  loadE();
})();

// Absender-Routing: welche eigene Rufnummer geht ueber welchen Provider raus (Tab Telefone & Rufnummern).
// Pflegt je Provider-Dial-Peer eine Pattern Map mit den Absendernummern (destination calling e164-pattern-map)
// und die Regel "Absender und Ziel muessen passen" (voice class dial-peer provision-policy: preference 1 calling called,
// preference 2 called) an den eingehenden Dial-Peers (destination provision-policy).
(() => {
  let E = null;
  let card = document.getElementById("egress-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "egress-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Ausgehend über Provider
      <span class="right muted" style="font-weight:400;font-size:12px">destination calling e164-pattern-map</span></h2>
    <div id="egress-body"><div class="empty">Lädt …</div></div>`;

  const provLabel = (p) => `Dial-Peer ${p.tag}${p.desc ? ` – ${p.desc}` : ""}`;
  const options = (sel, empty) => `<option value="">${empty}</option>` +
    E.providers.map(p => `<option value="${esc(p.tag)}" ${sel === p.tag ? "selected" : ""}>${esc(provLabel(p))}</option>`).join("");

  function render() {
    const body = $("#egress-body");
    if (!E.providers.length) {
      body.innerHTML = `<div class="body"><div class="hint">Keine Dial-Peers zu Providern gefunden
        (VoIP-Dial-Peers mit <span class="mono">session target</span>, außer zu Asterisk).</div></div>`;
      return;
    }
    const anyActive = E.providers.some(p => p.active);
    body.innerHTML = `
      <div class="body">
        <div class="hint">Jede eigene Rufnummer geht über den Provider raus, bei dem sie registriert ist. Der Router wählt dann den
          Dial-Peer nach Ziel <b>und</b> Absender. Rufnummern ohne Zuordnung gehen über den Standard-Provider –
          ist keiner gewählt, wählt der Router wie bisher nur nach dem Ziel.
          Der Standard gilt auch für weitergeleitete Anrufe, die mit der Nummer des externen Anrufers rausgehen.
          ${E.unmanaged.length ? `<br><b>Hinweis:</b> Dial-Peer ${E.unmanaged.map(esc).join(", ")} prüft den Absender nicht. Bedient er dieselben
          Ziele wie die Provider oben, konkurriert er weiter mit ihnen – dann einer Rufnummer zuordnen oder als Standard wählen.` : ""}</div>
        <form id="f-egress">
          <table><thead><tr><th>Rufnummer</th><th>Geht raus über</th></tr></thead><tbody>
            ${E.numbers.map(n => `<tr><td><span class="mono">${esc(n.number)}</span> <span class="muted">${esc(n.name)}</span></td>
              <td><select name="n:${esc(n.number)}" style="min-width:280px">${options(n.provider, "Standard-Provider")}</select></td></tr>`).join("")
              || `<tr><td colspan="2" class="empty">Keine Rufnummern.</td></tr>`}
            <tr><td><b>Alle anderen Absender</b> <span class="muted">(Standard)</span></td>
              <td><select name="default" style="min-width:280px">${options(E.default, "– keiner –")}</select></td></tr>
          </tbody></table>
          <div style="margin-top:12px"><b>Anrufe kommen herein über</b>
            <span class="muted">– an diesen Dial-Peers hängt die Regel (<span class="mono">destination provision-policy</span>)</span></div>
          <div style="display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:4px">
            ${E.inbound.map(i => `<label class="check" style="margin:0"><input type="checkbox" name="inbound" value="${esc(i.tag)}" ${i.selected ? "checked" : ""}>
              <span class="mono">${esc(i.tag)}</span> ${esc(i.desc || i.incoming)}</label>`).join("")
              || '<span class="muted">Keine Dial-Peers mit <span class="mono">incoming …</span> gefunden.</span>'}</div>
          <div class="actions" style="margin-top:10px"><button class="primary" type="submit">Am Router übernehmen</button>
            ${anyActive ? badge("ok", `aktiv an Dial-Peer ${E.providers.filter(p => p.active).map(p => p.tag).join(", ")}`) : badge("neutral", "noch nicht eingerichtet")}</div>
          <div class="msg" id="m-egress"></div>
        </form>
        <div class="hint" style="margin-top:8px">Prüfen mit <span class="mono">debug voip dialpeer inout</span> und je einem Anruf:
          in der Ausgabe muss <span class="mono">DP_MATCH_DEST_CALLING</span> den richtigen Dial-Peer finden.
          ${E.policy ? `Regel: <span class="mono">voice class dial-peer provision-policy ${esc(E.policy)}</span>
            (${E.policy_lines.map(esc).join(", ")}).` : ""}</div>
      </div>`;
  }

  async function loadEg() {
    const j = await api("/api/egress");
    if (!j.ok) { $("#egress-body").innerHTML = `<div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    E = j;
    render();
  }

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-egress") return;
    const assign = {};
    f.querySelectorAll("select[name^='n:']").forEach(s => { if (s.value) assign[s.name.slice(2)] = s.value; });
    const def = f.default.value;
    const inbound = [...f.querySelectorAll("input[name=inbound]:checked")].map(i => i.value);
    if ((Object.keys(assign).length || def) && !inbound.length) {
      showMsg($("#m-egress"), false, "Bitte mindestens einen Dial-Peer wählen, über den die Anrufe hereinkommen.");
      return;
    }
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const j = await api("/api/egress", { method: "POST", body: JSON.stringify({ assign, default: def, inbound }) });
    btn.disabled = false;
    if (!j.ok) { showMsg($("#m-egress"), false, j.error || "Fehler"); return; }
    await loadEg();
    showMsg($("#m-egress"), true, j.unchanged ? "Keine Änderung." : `Übernommen.\n\n${(j.config || []).join("\n")}`);
    if (window.loadE164) window.loadE164();
  });

  loadEg();
})();
