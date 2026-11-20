// Dial-Peers, SIP-Tenants und SIP-Profile bearbeiten (Tab "Dial-Peers & Trunks").
// Bearbeitet wird der Konfigurationstext; cme-web schickt nur die Unterschiede an den Router.
// Nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let card = document.getElementById("dialpeer-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "dialpeer-card";
    document.querySelector("main").appendChild(card);
  }
  let D = { dialpeers: [], tenants: [], profiles: [], uris: [], servergroups: [], keepalives: [], globals: [] };
  let edit = null;     // {kind, tag, new, type, text, title, warn}
  const NAMES = { dialpeer: "Dial-Peer", tenant: "Tenant", profile: "SIP-Profil", uri: "URI-Klasse",
                  servergroup: "Server-Gruppe", keepalive: "OPTIONS-Keepalive-Profil", global: "Abschnitt" };
  const GLOBAL_LABEL = { voip: "SIP-Grundeinstellungen, vertrauenswürdige Adressen (voice service voip)",
                         regglobal: "CME-Grundeinstellungen: Zeitzone, Datumsformat, Limits, Sprache (voice register global)",
                         telephony: "Telefonie-Dienst: Konferenzen, Weitervermittlung (telephony-service)",
                         sipua: "SIP-User-Agent: MWI-Server, Transport, Timer (sip-ua)" };
  const GLOBAL_HEAD = { voip: "voice service voip", regglobal: "voice register global", telephony: "telephony-service", sipua: "sip-ua" };
  const HEAD = { dialpeer: (t, ty) => `dial-peer voice ${t} ${ty}`, tenant: (t) => `voice class tenant ${t}`,
                 profile: (t) => `voice class sip-profiles ${t}`, uri: (t, ty) => `voice class uri ${t} ${ty || "sip"}`,
                 servergroup: (t) => `voice class server-group ${t}`, keepalive: (t) => `voice class sip-options-keepalive ${t}`,
                 global: (t) => GLOBAL_HEAD[t] };

  function editor() {
    if (!edit) return "";
    const e = edit;
    return `<div class="body" id="dp-edit" style="border-top:1px solid var(--line);background:var(--bg, transparent)">
      <form id="f-dp">
        <label style="font-weight:600;color:var(--text);font-size:13px;margin-bottom:8px">${esc(e.title)}</label>
        ${e.new ? `<div class="row">
          <div><label>${e.kind === "uri" ? "Name" : "Nummer (Tag)"}</label><input name="tag" class="mono" value="${esc(e.tag)}" required></div>
          ${e.kind === "dialpeer" ? `<div><label>Typ</label><select name="type">
            <option value="voip" ${e.type === "voip" ? "selected" : ""}>voip (SIP)</option>
            <option value="pots" ${e.type === "pots" ? "selected" : ""}>pots (analog/ISDN)</option></select></div>`
            : e.kind === "uri" ? `<div><label>Typ</label><select name="type">
            <option value="sip" ${e.type !== "tel" ? "selected" : ""}>sip</option>
            <option value="tel" ${e.type === "tel" ? "selected" : ""}>tel</option></select></div>` : "<div></div>"}
        </div>` : `<div class="mono muted" style="margin-bottom:4px">${esc(HEAD[e.kind](e.tag, e.type))}</div>`}
        ${e.warn ? `<div class="msg bad" style="display:block">${e.warn}</div>` : ""}
        <textarea name="text" class="mono" rows="${Math.max(8, e.text.split("\n").length + 2)}" spellcheck="false"
          style="width:100%;font-size:12px;white-space:pre">${esc(e.text)}</textarea>
        <div class="hint">Zeilen wie in der running-config, ohne die Kopfzeile. Unterzeilen (z. B. <span class="mono">credentials</span> unter
          <span class="mono">registrar</span>) mit einem Leerzeichen mehr einrücken. Weggelassene Zeilen werden mit „no …“ entfernt,
          neue hinzugefügt. Verschlüsselte Passwörter (<span class="mono">password 6 …</span>) unverändert lassen – zum Ändern
          <span class="mono">password &lt;klartext&gt;</span> eintragen, der Router verschlüsselt es selbst.
          ${e.kind === "profile" ? "<br>Neue Regeln hängt der Router ans Ende an; für eine andere Reihenfolge Regeln entfernen und neu eintragen." : ""}
          ${e.kind === "global" ? "<br><b>Vorsicht:</b> Diese Einstellungen gelten für die ganze Anlage. Untermodi (z. B. <span class='mono'>sip</span>, <span class='mono'>ip address trusted list</span>) wie in der running-config einrücken." +
            (e.tag === "regglobal" ? " Nach dem Übernehmen führt cme-web <span class='mono'>create profile</span> aus; die Telefone übernehmen die Änderung nach einem Neustart." : "") : ""}</div>
        <div class="actions"><button class="primary" type="submit">${e.new ? "Anlegen" : "Am Router übernehmen"}</button>
          <button type="button" class="small" id="dp-cancel">Schließen</button></div>
        <div class="msg" id="m-dp"></div>
      </form></div>`;
  }

  function render() {
    const dp = D.dialpeers, used = (u) => u?.length ? u.map(esc).join(", ") : '<span class="muted">–</span>';
    card.innerHTML = `
      <h2>Dial-Peers <span class="count">(${dp.length})</span>
        <span class="right"><button class="small" data-new="dialpeer">Neuer Dial-Peer</button></span></h2>
      <table><thead><tr><th>Tag</th><th>Beschreibung</th><th>Muster / eingehend</th><th>Ziel</th><th>Tenant</th><th></th></tr></thead><tbody>
        ${dp.map(p => `<tr>
          <td class="mono">${esc(p.tag)} <span class="muted">${esc(p.type)}</span></td>
          <td>${esc(p.desc) || '<span class="muted">–</span>'} ${p.managed ? badge("neutral", "cme-web") : ""} ${p.shutdown ? badge("bad", "shutdown") : ""}</td>
          <td class="mono">${esc(p.pattern)}${p.incoming ? `<br><span class="muted">ein: ${esc(p.incoming)}</span>` : ""}</td>
          <td class="mono" style="font-size:12px">${esc(p.target)}</td>
          <td class="mono">${esc(p.tenant)}</td>
          <td><div class="actions"><button class="small" data-edit="dialpeer:${esc(p.tag)}">Bearbeiten</button>
            <button class="small" data-copy="dialpeer:${esc(p.tag)}">Kopieren</button>
            <button class="small danger" data-del="dialpeer:${esc(p.tag)}">Löschen</button></div></td></tr>`).join("")
          || '<tr><td colspan="6" class="empty">Keine Dial-Peers.</td></tr>'}
      </tbody></table>
      ${edit?.kind === "dialpeer" ? editor() : ""}
      <h2 style="border-top:1px solid var(--line)">SIP-Tenants <span class="count">(${D.tenants.length})</span>
        <span class="right muted" style="font-weight:400;font-size:12px">voice class tenant
          <button class="small" data-new="tenant" style="margin-left:8px">Neuer Tenant</button></span></h2>
      <table><thead><tr><th>Tag</th><th>Registrar</th><th>Benutzer</th><th>Benutzt von</th><th></th></tr></thead><tbody>
        ${D.tenants.map(t => `<tr><td class="mono">${esc(t.tag)}</td>
          <td class="mono" style="font-size:12px">${esc(t.registrar)}${t.proxy ? `<br><span class="muted">Proxy ${esc(t.proxy)}</span>` : ""}</td>
          <td class="mono">${t.users.map(esc).join("<br>")}</td>
          <td>${t.used.length ? "Dial-Peer " + t.used.map(esc).join(", ") : '<span class="muted">–</span>'}</td>
          <td><div class="actions"><button class="small" data-edit="tenant:${esc(t.tag)}">Bearbeiten</button>
            <button class="small" data-copy="tenant:${esc(t.tag)}">Kopieren</button>
            <button class="small danger" data-del="tenant:${esc(t.tag)}">Löschen</button></div></td></tr>`).join("")
          || '<tr><td colspan="5" class="empty">Keine Tenants.</td></tr>'}
      </tbody></table>
      ${edit?.kind === "tenant" ? editor() : ""}
      <h2 style="border-top:1px solid var(--line)">SIP-Profile <span class="count">(${D.profiles.length})</span>
        <span class="right muted" style="font-weight:400;font-size:12px">voice class sip-profiles
          <button class="small" data-new="profile" style="margin-left:8px">Neues Profil</button></span></h2>
      <table><thead><tr><th>Tag</th><th>Regeln</th><th>Benutzt von</th><th></th></tr></thead><tbody>
        ${D.profiles.map(p => `<tr><td class="mono">${esc(p.tag)}</td>
          <td class="mono" style="font-size:11px">${p.lines.slice(0, 3).map(esc).join("<br>")}${p.lines.length > 3 ? `<br><span class="muted">… ${p.lines.length - 3} weitere</span>` : ""}</td>
          <td>${used(p.used)}</td>
          <td><div class="actions"><button class="small" data-edit="profile:${esc(p.tag)}">Bearbeiten</button>
            <button class="small" data-copy="profile:${esc(p.tag)}">Kopieren</button>
            <button class="small danger" data-del="profile:${esc(p.tag)}">Löschen</button></div></td></tr>`).join("")
          || '<tr><td colspan="4" class="empty">Keine SIP-Profile.</td></tr>'}
      </tbody></table>
      ${edit?.kind === "profile" ? editor() : ""}
      ${simpleTable("uri", D.uris, "SIP-URI-Klassen", "voice class uri", "Neue URI-Klasse")}
      ${simpleTable("servergroup", D.servergroups, "Server-Gruppen", "voice class server-group", "Neue Server-Gruppe")}
      ${simpleTable("keepalive", D.keepalives, "OPTIONS-Keepalive-Profile", "voice class sip-options-keepalive", "Neues Profil")}
      <h2 style="border-top:1px solid var(--line)">Globale Einstellungen</h2>
      <table><tbody>
        ${D.globals.map(g => `<tr><td>${esc(GLOBAL_LABEL[g.tag] || g.head)}</td>
          <td class="mono muted" style="font-size:11px">${g.lines.length} Zeilen</td>
          <td><div class="actions"><button class="small" data-edit="global:${esc(g.tag)}">Bearbeiten</button></div></td></tr>`).join("")}
      </tbody></table>
      ${edit?.kind === "global" ? editor() : ""}`;
  }

  function simpleTable(kind, items, title, head, newLabel) {
    return `<h2 style="border-top:1px solid var(--line)">${title} <span class="count">(${items.length})</span>
        <span class="right muted" style="font-weight:400;font-size:12px">${head}
          <button class="small" data-new="${kind}" style="margin-left:8px">${newLabel}</button></span></h2>
      <table><thead><tr><th>${kind === "uri" ? "Name" : "Tag"}</th><th>Inhalt</th><th>Benutzt von</th><th></th></tr></thead><tbody>
        ${items.map(x => `<tr><td class="mono">${esc(x.tag)}${x.type ? ` <span class="muted">${esc(x.type)}</span>` : ""}</td>
          <td class="mono" style="font-size:11px">${x.lines.slice(0, 3).map(esc).join("<br>")}${x.lines.length > 3 ? `<br><span class="muted">… ${x.lines.length - 3} weitere</span>` : ""}</td>
          <td>${x.used?.length ? x.used.map(esc).join(", ") : '<span class="muted">–</span>'}</td>
          <td><div class="actions"><button class="small" data-edit="${kind}:${esc(x.tag)}">Bearbeiten</button>
            <button class="small" data-copy="${kind}:${esc(x.tag)}">Kopieren</button>
            <button class="small danger" data-del="${kind}:${esc(x.tag)}">Löschen</button></div></td></tr>`).join("")
          || `<tr><td colspan="4" class="empty">Keine.</td></tr>`}
      </tbody></table>
      ${edit?.kind === kind ? editor() : ""}`;
  }

  const list = (kind) => ({ dialpeer: D.dialpeers, tenant: D.tenants, profile: D.profiles, uri: D.uris,
                             servergroup: D.servergroups, keepalive: D.keepalives, global: D.globals })[kind];
  const freeTag = (kind) => {
    const used = new Set(list(kind).map(x => +x.tag));
    if (kind === "uri") return "neu";
    let t = kind === "dialpeer" ? 100 : 1;
    while (used.has(t)) t++;
    return String(t);
  };

  async function load() {
    const j = await api("/api/dialpeers");
    if (!j.ok) { card.innerHTML = `<h2>Dial-Peers</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    D = j;
    render();
  }

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.new) {
      edit = { kind: d.new, tag: freeTag(d.new), new: true, type: d.new === "uri" ? "sip" : "voip", text: "", title: `${NAMES[d.new]} anlegen` };
      render();
    } else if (d.edit || d.copy) {
      const [kind, tag] = (d.edit || d.copy).split(":");
      const x = list(kind).find(y => y.tag === tag);
      const warn = x.managed ? "Diesen Dial-Peer verwaltet cme-web (Voicemail, Fax, Aufzeichnung, Berechtigungen …). " +
        "Änderungen hier können dort wieder überschrieben werden." : "";
      edit = d.edit ? { kind, tag, new: false, type: x.type, text: x.lines.join("\n"),
                        title: kind === "global" ? `${GLOBAL_HEAD[tag]} bearbeiten` : `${NAMES[kind]} ${tag} bearbeiten`, warn }
                    : { kind, tag: freeTag(kind), new: true, type: x.type || "voip",
                        text: x.lines.filter(l => !/^description /.test(l.trim())).join("\n"), title: `Kopie von ${NAMES[kind]} ${tag}` };
      render();
      $("#dp-edit")?.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (e.target.id === "dp-cancel") {
      edit = null;
      render();
    } else if (d.del) {
      const [kind, tag] = d.del.split(":");
      if (!confirm(`${NAMES[kind]} ${tag} am Router löschen?`)) return;
      const j = await api(`/api/dialpeers/${kind}/${tag}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      if (edit && edit.kind === kind && edit.tag === tag) edit = null;
      load();
    }
  });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id !== "f-dp" || !edit) return;
    const tag = edit.new ? f.tag.value.trim() : edit.tag;
    const body = { text: f.text.value, new: edit.new, type: f.type?.value || edit.type };
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const j = await api(`/api/dialpeers/${edit.kind}/${encodeURIComponent(tag)}`, { method: "PUT", body: JSON.stringify(body) });
    btn.disabled = false;
    if (!j.ok) { edit.text = f.text.value; showMsg($("#m-dp"), false, j.error || "Fehler"); return; }
    const kind = edit.kind;
    await load();
    const x = list(kind).find(y => y.tag === tag);
    edit = x ? { kind, tag, new: false, type: x.type, text: x.lines.join("\n"),
                 title: kind === "global" ? `${GLOBAL_HEAD[tag]} bearbeiten` : `${NAMES[kind]} ${tag} bearbeiten` } : null;
    render();
    if (edit) showMsg($("#m-dp"), true, (j.unchanged ? "Keine Änderung." : "Übernommen.") +
      (j.config?.length ? "\n\n" + j.config.join("\n") : "") +
      (j.differences?.length ? "\n\nDer Router zeigt danach anders an:\n" + j.differences.join("\n") : ""));
  });

  load();
})();
