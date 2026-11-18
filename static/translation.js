// Uebersetzungsregeln (voice translation-rule) und -profile (voice translation-profile)
// Wird von cme_web in index.html eingehaengt und nutzt dessen Helfer ($, esc, badge, api, showMsg).
(() => {
  const KIND_LABEL = { "calling": "Anrufer (calling)", "called": "Ziel (called)", "redirect-called": "Umleitung (redirect-called)" };
  let T = { rulesets: [], profiles: [], number_types: [], number_plans: [], translate_kinds: [] };

  const card = document.createElement("section");
  card.className = "card";
  card.id = "translation-card";
  card.innerHTML = `
    <h2>Übersetzungsregeln <span class="count" id="c-trules"></span>
      <span class="right muted" style="font-weight:400;font-size:12px">voice translation-rule</span></h2>
    <table><thead><tr><th>Regelsatz</th><th>Regeln (in dieser Reihenfolge geprüft)</th><th>Verwendet in</th><th></th></tr></thead>
      <tbody id="t-trules"></tbody></table>
    <div class="body" style="border-top: 1px solid var(--line)">
      <form id="f-trule">
        <input type="hidden" name="edit_tag" value="">
        <label id="trule-title" style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Neuer Regelsatz</label>
        <div class="row" id="trule-tag-row">
          <div><label>Nummer des Regelsatzes</label><input name="tag" type="number" min="1" placeholder="automatisch"></div>
          <div></div>
        </div>
        <div class="lines" id="trule-rows"></div>
        <div class="row" style="margin-top:8px">
          <div><label>Vorschau: Testnummer</label><input id="tprev-num" class="mono" placeholder="061871234567" autocomplete="off"></div>
          <div><label>Nummerntyp der Testnummer</label><select id="tprev-type"></select></div>
        </div>
        <div id="tprev-out" class="mono" style="min-height:20px;margin:4px 0 8px"></div>
        <div class="hint">Muster und Ersetzung sind IOS-Regex ohne Leerzeichen: <span class="mono">^</span> Anfang,
          <span class="mono">$</span> Ende, <span class="mono">.</span> beliebige Ziffer, <span class="mono">*</span> beliebig oft,
          <span class="mono">\\(…\\)</span> merken und <span class="mono">\\1</span> wieder einsetzen.
          Beispiel: <span class="mono">/^0\\(.*\\)/</span> → <span class="mono">/+49\\1/</span> macht aus 06187… die Nummer +496187….
          Die erste passende Regel gewinnt.</div>
        <div class="hint" id="trule-warn" hidden></div>
        <div class="actions">
          <button class="primary" type="submit" id="trule-submit">Anlegen</button>
          <button type="button" id="trule-add" class="small">+ Regel</button>
          <button type="button" id="trule-cancel" class="small" hidden>Abbrechen</button>
        </div>
        <div class="msg" id="m-trule"></div>
      </form>
    </div>
    <div class="body" style="border-top: 1px solid var(--line)">
      <form id="f-ttest">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Gespeicherten Regelsatz testen (am Router)</label>
        <div class="row">
          <div><label>Regelsatz</label><select name="tag" id="ttest-tag"></select></div>
          <div><label>Nummer</label><input name="number" placeholder="061871234567" required></div>
        </div>
        <div class="actions"><button type="submit" id="ttest-submit">Testen</button>
          <span id="ttest-result" class="mono"></span></div>
        <pre class="raw mono" id="ttest-out" hidden style="margin-top:10px;border:1px solid var(--line);border-radius:7px"></pre>
      </form>
    </div>
    <h2 style="border-top: 1px solid var(--line)">Übersetzungsprofile <span class="count" id="c-tprofiles"></span>
      <span class="right muted" style="font-weight:400;font-size:12px">voice translation-profile</span></h2>
    <table><thead><tr><th>Profil</th><th>Anrufer</th><th>Ziel</th><th>Umleitung</th><th>Verwendet von</th><th></th></tr></thead>
      <tbody id="t-tprofiles"></tbody></table>
    <div class="body" style="border-top: 1px solid var(--line)">
      <form id="f-tprofile">
        <input type="hidden" name="edit_name" value="">
        <label id="tprofile-title" style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Neues Profil</label>
        <div class="row">
          <div><label>Name *</label><input name="name" required maxlength="30" placeholder="TRUNK-OUT"></div>
          <div></div>
        </div>
        <div class="row" id="tprofile-selects"></div>
        <div class="hint">Ein Profil wird mit <span class="mono">translation-profile incoming|outgoing &lt;Name&gt;</span>
          an einen Dial-Peer oder Pool gehängt. Das geschieht am Router; hier steht, wo es verwendet wird.</div>
        <div class="hint" id="tprofile-warn" hidden></div>
        <div class="actions">
          <button class="primary" type="submit" id="tprofile-submit">Anlegen</button>
          <button type="button" id="tprofile-cancel" class="small" hidden>Abbrechen</button>
        </div>
        <div class="msg" id="m-tprofile"></div>
      </form>
    </div>
    <div class="body" style="border-top: 1px solid var(--line)">
      <form id="f-tptest">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">Profil testen (am Router)</label>
        <div class="row">
          <div><label>Profil</label><select name="name" id="tptest-name"></select></div>
          <div><label>Anrufer (calling)</label><input name="calling" class="mono" placeholder="061871234567"></div>
        </div>
        <div class="row">
          <div><label>Ziel (called)</label><input name="called" class="mono" placeholder="0301234567"></div>
          <div><label>Umleitung (redirect-called)</label><input name="redirect-called" class="mono" placeholder="optional"></div>
        </div>
        <div class="actions"><button type="submit" id="tptest-submit">Testen</button></div>
        <div id="tptest-out" style="margin-top:8px"></div>
      </form>
    </div>`;
  const anchor = document.getElementById("raw-out")?.closest("section.card");
  (anchor ? anchor.parentNode.insertBefore(card, anchor) : document.querySelector("main").appendChild(card));

  // ---------------- Darstellung ----------------

  const ruleText = (r) => r.reject
    ? `<span class="muted">${r.prec}:</span> /${esc(r.match)}/ ${badge("bad", "abweisen")}`
    : `<span class="muted">${r.prec}:</span> /${esc(r.match)}/ <span class="muted">→</span> /${esc(r.replace)}/` +
      (r.type_from ? ` <span class="muted">type ${esc(r.type_from)}→${esc(r.type_to)}</span>` : "") +
      (r.plan_from ? ` <span class="muted">plan ${esc(r.plan_from)}→${esc(r.plan_to)}</span>` : "");

  function render() {
    $("#c-trules").textContent = `(${T.rulesets.length})`;
    $("#t-trules").innerHTML = T.rulesets.length ? T.rulesets.map(rs => `<tr>
        <td class="mono">${esc(rs.tag)}</td>
        <td class="mono">${rs.rules.map(r => `<div>${ruleText(r)}</div>`).join("") || '<span class="muted">leer</span>'}
          ${rs.other.length ? `<div class="muted">${rs.other.map(esc).join("<br>")}</div>` : ""}</td>
        <td>${rs.profiles.length ? rs.profiles.map(p => `${esc(p.profile)} <span class="muted">(${esc(KIND_LABEL[p.kind] || p.kind)})</span>`).join("<br>")
              : '<span class="muted">nicht verwendet</span>'}</td>
        <td><div class="actions"><button class="small" data-edit-trule="${esc(rs.tag)}">Bearbeiten</button>
          <button class="small danger" data-delete-trule="${esc(rs.tag)}">Löschen</button></div></td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">Noch keine Übersetzungsregeln.</td></tr>`;

    const tsel = $("#ttest-tag"), tv = tsel.value;
    tsel.innerHTML = T.rulesets.map(rs => `<option value="${esc(rs.tag)}">Regelsatz ${esc(rs.tag)}</option>`).join("");
    if ([...tsel.options].some(o => o.value === tv)) tsel.value = tv;

    const psel = $("#tptest-name"), pv = psel.value;
    psel.innerHTML = T.profiles.map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
    if ([...psel.options].some(o => o.value === pv)) psel.value = pv;
    const ty = $("#tprev-type");
    if (!ty.options.length) {
      ty.innerHTML = T.number_types.filter(t => t !== "any").map(t => `<option ${t === "unknown" ? "selected" : ""}>${esc(t)}</option>`).join("");
    }

    $("#c-tprofiles").textContent = `(${T.profiles.length})`;
    const cell = (p, k) => p.translate[k] ? `<span class="mono">${esc(p.translate[k])}</span>` : '<span class="muted">–</span>';
    $("#t-tprofiles").innerHTML = T.profiles.length ? T.profiles.map(p => `<tr>
        <td class="mono">${esc(p.name)}</td><td>${cell(p, "calling")}</td><td>${cell(p, "called")}</td>
        <td>${cell(p, "redirect-called")}</td>
        <td>${p.used.length ? p.used.map(u => `${esc(u.where)} ${esc(u.id)} <span class="muted">${u.dir === "incoming" ? "eingehend" : "ausgehend"}</span>`).join("<br>")
              : '<span class="muted">nicht verwendet</span>'}</td>
        <td><div class="actions"><button class="small" data-edit-tprofile="${esc(p.name)}">Bearbeiten</button>
          <button class="small danger" data-delete-tprofile="${esc(p.name)}">Löschen</button></div></td></tr>`).join("")
      : `<tr><td colspan="6" class="empty">Noch keine Übersetzungsprofile.</td></tr>`;

    // Auswahlfelder fuer Profile
    const box = $("#tprofile-selects");
    const keep = Object.fromEntries([...box.querySelectorAll("select")].map(s => [s.name, s.value]));
    box.innerHTML = T.translate_kinds.map(k => `<div><label>${esc(KIND_LABEL[k] || k)}</label>
      <select name="${esc(k)}"><option value="">– keiner –</option>${T.rulesets.map(rs =>
        `<option value="${esc(rs.tag)}">Regelsatz ${esc(rs.tag)}</option>`).join("")}</select></div>`).join("");
    box.querySelectorAll("select").forEach(s => { if (keep[s.name]) s.value = keep[s.name]; });
    if (!$("#trule-rows").children.length) addRuleRow();
    preview();
  }

  async function loadT() {
    const j = await api("/api/translation");
    if (!j.ok) {
      $("#t-trules").innerHTML = `<tr><td colspan="4" class="empty bad-text">${esc(j.error || "Fehler beim Laden")}</td></tr>`;
      return;
    }
    T = j;
    render();
  }

  // ---------------- Regelsatz-Formular ----------------

  function typeOptions(sel) {
    return `<option value="">–</option>` + T.number_types.map(t =>
      `<option ${t === sel ? "selected" : ""}>${esc(t)}</option>`).join("");
  }
  function addRuleRow(r = null) {
    const box = $("#trule-rows");
    const used = [...box.querySelectorAll("input[name=prec]")].map(i => +i.value);
    const prec = r ? r.prec : (Math.max(0, ...used) + 1);
    const div = document.createElement("div");
    div.className = "ln";
    div.style.flexWrap = "wrap";
    div.dataset.plan = r && r.plan_from ? `${r.plan_from} ${r.plan_to}` : "";
    div.innerHTML = `
      <input name="prec" type="number" min="1" max="15" value="${prec}" style="width:56px" title="Regelnummer (1–15)">
      <input name="match" placeholder="^0\\(.*\\)" style="flex:1;min-width:120px" class="mono" value="${esc(r?.match || "")}">
      <span style="width:auto">→</span>
      <input name="replace" placeholder="+49\\1" style="flex:1;min-width:100px" class="mono" value="${esc(r?.replace || "")}">
      <label class="check" style="margin:0" title="Anruf abweisen statt umschreiben"><input type="checkbox" name="reject" ${r?.reject ? "checked" : ""}> abweisen</label>
      <select name="type_from" title="Nummerntyp vorher" style="width:auto">${typeOptions(r?.type_from)}</select>
      <select name="type_to" title="Nummerntyp nachher" style="width:auto">${typeOptions(r?.type_to)}</select>
      <button type="button" class="small" title="entfernen">✕</button>
      <span class="tprev-hit" style="width:auto"></span>`;
    const sync = () => {
      const rej = div.querySelector("[name=reject]").checked;
      ["replace", "type_from", "type_to"].forEach(n => div.querySelector(`[name=${n}]`).disabled = rej);
    };
    div.querySelector("[name=reject]").addEventListener("change", sync);
    div.querySelector("button").addEventListener("click", () => { div.remove(); preview(); });
    box.appendChild(div);
    sync();
  }
  function readRules() {
    return [...$("#trule-rows").children].map(div => {
      const v = (n) => div.querySelector(`[name=${n}]`).value.trim();
      const [plan_from, plan_to] = (div.dataset.plan || " ").split(" ");
      return { prec: v("prec"), match: v("match"), replace: v("replace"),
               reject: div.querySelector("[name=reject]").checked,
               type_from: v("type_from"), type_to: v("type_to"), plan_from, plan_to };
    }).filter(r => r.match || r.replace);
  }
  // ---------------- Vorschau im Browser (IOS-Regex nachgebildet) ----------------

  // IOS: \( \) bilden Gruppen fuer \1..\9, einfache Klammern nur Gruppierung
  function iosRegex(p) {
    let out = "";
    for (let i = 0; i < p.length; i++) {
      const c = p[i];
      if (c === "\\" && i + 1 < p.length) {
        const n = p[++i];
        out += (n === "(" || n === ")") ? n : "\\" + n;
      } else out += c === "(" ? "(?:" : c;
    }
    return new RegExp(out);
  }
  function iosReplace(m, repl) {
    let out = "";
    for (let i = 0; i < repl.length; i++) {
      const c = repl[i];
      if (c === "\\" && i + 1 < repl.length) {
        const n = repl[++i];
        out += /[0-9]/.test(n) ? (m[+n] ?? "") : n;
      } else out += c;
    }
    return out;
  }
  // Wendet die Regeln wie IOS an: nach Nummer sortiert, die erste passende gewinnt
  function simulate(rules, number, type) {
    const sorted = [...rules].sort((a, b) => +a.prec - +b.prec);
    for (const r of sorted) {
      if (!r.match) continue;
      if (r.type_from && r.type_from !== "any" && r.type_from !== type) continue;
      let re;
      try { re = iosRegex(r.match); } catch (err) { return { error: `Regel ${r.prec}: ungültiges Muster`, rule: r.prec }; }
      const m = re.exec(number);
      if (!m) continue;
      if (r.reject) return { rule: r.prec, rejected: true };
      const res = number.slice(0, m.index) + iosReplace(m, r.replace) + number.slice(m.index + m[0].length);
      return { rule: r.prec, translated: res,
               type: r.type_to && r.type_to !== "any" ? r.type_to : type };
    }
    return { rule: null };
  }
  function preview() {
    const num = $("#tprev-num").value.trim(), out = $("#tprev-out");
    const rows = [...$("#trule-rows").children];
    rows.forEach(d => d.querySelector(".tprev-hit").innerHTML = "");
    if (!num) { out.innerHTML = '<span class="muted">Nummer eingeben, um die Regeln schon vor dem Speichern auszuprobieren.</span>'; return; }
    const res = simulate(readRules(), num, $("#tprev-type").value);
    const row = rows.find(d => d.querySelector("[name=prec]").value.trim() === String(res.rule));
    if (row) row.querySelector(".tprev-hit").innerHTML = badge(res.error ? "bad" : "ok", res.error ? "Fehler" : "trifft");
    out.innerHTML = res.error ? `<span class="bad-text">${esc(res.error)}</span>`
      : res.rejected ? `${esc(num)} → ${badge("bad", `abgewiesen (Regel ${res.rule})`)}`
      : res.rule ? `${esc(num)} → <b>${esc(res.translated)}</b> <span class="muted">(Regel ${esc(res.rule)}` +
                   `${res.type !== $("#tprev-type").value ? `, Typ ${esc(res.type)}` : ""})</span>`
      : `${esc(num)} → ${badge("neutral", "keine Regel passt, Nummer bleibt")}`;
    out.innerHTML += ' <span class="muted" style="font-family:inherit">· Vorschau im Browser, maßgeblich ist der Test am Router</span>';
  }
  $("#f-trule").addEventListener("input", preview);
  $("#f-trule").addEventListener("change", preview);

  function resetRuleForm() {
    const f = $("#f-trule");
    f.reset(); f.edit_tag.value = "";
    $("#trule-rows").innerHTML = ""; addRuleRow();
    $("#trule-tag-row").hidden = false;
    $("#trule-warn").hidden = true;
    preview();
  }
  function startRuleEdit(tag) {
    const rs = T.rulesets.find(x => x.tag === tag);
    if (!rs) return;
    const f = $("#f-trule");
    f.reset(); f.edit_tag.value = tag;
    $("#trule-rows").innerHTML = "";
    (rs.rules.length ? rs.rules : [null]).forEach(r => addRuleRow(r));
    $("#trule-tag-row").hidden = true;
    $("#trule-title").textContent = `Regelsatz ${tag} bearbeiten`;
    $("#trule-submit").textContent = "Änderungen speichern";
    $("#trule-cancel").hidden = false;
    const warn = $("#trule-warn");
    const users = rs.profiles.map(p => p.profile);
    warn.hidden = !users.length;
    warn.textContent = users.length ? `Achtung: Der Regelsatz steckt in Profil ${[...new Set(users)].join(", ")}. ` +
      `Eine Änderung wirkt sofort auf alle Anrufe, die darüber laufen.` : "";
    $("#m-trule").className = "msg";
    preview();
    card.classList.add("editing");
    f.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function endRuleEdit(clearMsg = true) {
    resetRuleForm();
    $("#trule-title").textContent = "Neuer Regelsatz";
    $("#trule-submit").textContent = "Anlegen";
    $("#trule-cancel").hidden = true;
    card.classList.remove("editing");
    if (clearMsg) $("#m-trule").className = "msg";
  }

  $("#f-trule").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = $("#trule-submit"), m = $("#m-trule"), editTag = f.edit_tag.value;
    const body = { rules: readRules(), tag: f.tag.value };
    btn.disabled = true;
    const j = editTag
      ? await api(`/api/translation/rules/${editTag}`, { method: "PUT", body: JSON.stringify(body) })
      : await api("/api/translation/rules", { method: "POST", body: JSON.stringify(body) });
    btn.disabled = false;
    if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
    showMsg(m, true, j.unchanged ? "Keine Änderungen." :
      `Regelsatz ${j.tag} ${editTag ? "gespeichert" : "angelegt"}.\n\n${j.config.join("\n")}`);
    endRuleEdit(false);
    await loadT();
    load(true);
  });
  $("#trule-add").addEventListener("click", () => {
    if ($("#trule-rows").children.length >= 15) { alert("Höchstens 15 Regeln je Regelsatz."); return; }
    addRuleRow();
  });
  $("#trule-cancel").addEventListener("click", () => endRuleEdit());

  $("#f-ttest").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, out = $("#ttest-out"), res = $("#ttest-result");
    if (!f.tag.value) { res.textContent = "Erst einen Regelsatz anlegen."; return; }
    $("#ttest-submit").disabled = true;
    res.textContent = "…";
    const j = await api("/api/translation/test", { method: "POST", body: JSON.stringify({ tag: f.tag.value, number: f.number.value }) });
    $("#ttest-submit").disabled = false;
    if (!j.ok) { res.innerHTML = `<span class="bad-text">${esc(j.error)}</span>`; out.hidden = true; return; }
    res.innerHTML = j.rejected ? badge("bad", `abgewiesen (Regel ${j.rule})`)
      : j.translated ? `${esc(f.number.value)} → <b>${esc(j.translated)}</b> <span class="muted">(Regel ${esc(j.rule)})</span>`
      : badge("neutral", "keine Regel passt");
    out.textContent = j.output; out.hidden = false;
  });

  // ---------------- Profil testen ----------------

  $("#f-tptest").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, out = $("#tptest-out");
    if (!f.name.value) { out.textContent = "Erst ein Profil anlegen."; return; }
    const body = Object.fromEntries(["calling", "called", "redirect-called"].map(k => [k, f[k].value.trim()]));
    $("#tptest-submit").disabled = true;
    out.innerHTML = '<span class="muted">…</span>';
    const j = await api(`/api/translation/profiles/${encodeURIComponent(f.name.value)}/test`,
                        { method: "POST", body: JSON.stringify(body) });
    $("#tptest-submit").disabled = false;
    if (!j.ok) { out.innerHTML = `<span class="bad-text">${esc(j.error)}</span>`; return; }
    out.innerHTML = `<table><tbody>${j.results.map(r => `<tr>
        <td>${esc(KIND_LABEL[r.kind] || r.kind)}</td>
        <td class="mono">${esc(r.number)} → ${!r.tag ? '<span class="muted">bleibt (kein Regelsatz im Profil)</span>'
          : r.rejected ? badge("bad", `abgewiesen (Regelsatz ${r.tag}, Regel ${r.rule})`)
          : r.translated ? `<b>${esc(r.translated)}</b> <span class="muted">(Regelsatz ${esc(r.tag)}, Regel ${esc(r.rule)}${r.type && r.type !== "none" ? `, Typ ${esc(r.type)}` : ""})</span>`
          : `${badge("neutral", "keine Regel passt")} <span class="muted">(Regelsatz ${esc(r.tag)})</span>`}</td></tr>`).join("")}
      </tbody></table>`;
  });

  // ---------------- Profil-Formular ----------------

  function startProfileEdit(name) {
    const p = T.profiles.find(x => x.name === name);
    if (!p) return;
    const f = $("#f-tprofile");
    f.reset(); f.edit_name.value = name;
    f.name.value = name; f.name.readOnly = true;
    $("#tprofile-selects").querySelectorAll("select").forEach(s => s.value = p.translate[s.name] || "");
    $("#tprofile-title").textContent = `Profil ${name} bearbeiten`;
    $("#tprofile-submit").textContent = "Änderungen speichern";
    $("#tprofile-cancel").hidden = false;
    const warn = $("#tprofile-warn");
    warn.hidden = !p.used.length;
    warn.textContent = p.used.length ? `Achtung: Das Profil hängt an ${p.used.map(u => `${u.where} ${u.id}`).join(", ")}. ` +
      `Eine Änderung wirkt dort sofort.` : "";
    $("#m-tprofile").className = "msg";
    card.classList.add("editing");
    f.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function endProfileEdit(clearMsg = true) {
    const f = $("#f-tprofile");
    f.reset(); f.edit_name.value = ""; f.name.readOnly = false;
    $("#tprofile-selects").querySelectorAll("select").forEach(s => s.value = "");
    $("#tprofile-title").textContent = "Neues Profil";
    $("#tprofile-submit").textContent = "Anlegen";
    $("#tprofile-cancel").hidden = true;
    $("#tprofile-warn").hidden = true;
    card.classList.remove("editing");
    if (clearMsg) $("#m-tprofile").className = "msg";
  }

  $("#f-tprofile").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, btn = $("#tprofile-submit"), m = $("#m-tprofile"), editName = f.edit_name.value;
    const translate = Object.fromEntries([...$("#tprofile-selects").querySelectorAll("select")].map(s => [s.name, s.value]));
    btn.disabled = true;
    const j = editName
      ? await api(`/api/translation/profiles/${encodeURIComponent(editName)}`, { method: "PUT", body: JSON.stringify({ translate }) })
      : await api("/api/translation/profiles", { method: "POST", body: JSON.stringify({ name: f.name.value, translate }) });
    btn.disabled = false;
    if (!j.ok) { showMsg(m, false, j.error || "Fehler"); return; }
    showMsg(m, true, j.unchanged ? "Keine Änderungen." :
      `Profil ${j.name} ${editName ? "gespeichert" : "angelegt"}.\n\n${j.config.join("\n")}`);
    endProfileEdit(false);
    await loadT();
    load(true);
  });
  $("#tprofile-cancel").addEventListener("click", () => endProfileEdit());

  // ---------------- Tabellen-Knoepfe ----------------

  document.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.editTrule) { startRuleEdit(d.editTrule); return; }
    if (d.editTprofile) { startProfileEdit(d.editTprofile); return; }
    let url, q;
    if (d.deleteTrule) {
      url = `/api/translation/rules/${d.deleteTrule}`;
      q = `Regelsatz ${d.deleteTrule} wirklich löschen?`;
    } else if (d.deleteTprofile) {
      url = `/api/translation/profiles/${encodeURIComponent(d.deleteTprofile)}`;
      q = `Profil ${d.deleteTprofile} wirklich löschen?`;
    } else return;
    if (!confirm(q)) return;
    e.target.disabled = true;
    const j = await api(url, { method: "DELETE" });
    e.target.disabled = false;
    if (!j.ok) { alert(j.error || "Fehler"); return; }
    if (d.deleteTrule && $("#f-trule").edit_tag.value === d.deleteTrule) endRuleEdit();
    if (d.deleteTprofile && $("#f-tprofile").edit_name.value === d.deleteTprofile) endProfileEdit();
    await loadT();
    load(true);
  });

  loadT();
})();

// Tab-Aufteilung der Seite (static/tabs.js) erst laden, wenn diese Karte eingehaengt ist
(() => {
  const s = document.createElement("script");
  s.src = "/static/tabs.js";
  document.body.appendChild(s);
})();
