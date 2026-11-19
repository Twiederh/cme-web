// Administration, Tab Benutzer: welche Rufnummern (Durchwahlen, Faxnummern) zu welchem angemeldeten Benutzer
// gehoeren. Die Benutzer sehen auf /me nur ihre eigene Voicemail, Faxe und Kurzwahlen (portal.py).
// Nutzt die Helfer aus index.html ($, esc, badge, api, showMsg).
(() => {
  let card = document.getElementById("users-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "users-card";
    document.querySelector("main").appendChild(card);
  }
  // Link zur eigenen Benutzerseite im Kopf
  const hdr = document.querySelector("header .user");
  if (hdr && !document.getElementById("me-link")) {
    hdr.insertAdjacentHTML("beforebegin", `<a id="me-link" href="/me" style="color:var(--accent);text-decoration:none;font-size:13px">Meine Seite</a>`);
  }
  let D = { users: [], numbers: [] }, open = null;
  const fmt = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
  const numLabel = (n) => `${n.number}${n.name ? " – " + n.name : ""}${n.kind === "fax" ? " (Fax)" : ""}`;

  function render() {
    card.innerHTML = `<h2>Benutzer <span class="count">(${D.users.length})</span>
        <span class="right muted" style="font-weight:400;font-size:12px">Benutzerseite: /me</span></h2>
      <div class="body"><div class="hint">Wer zur Gruppe <span class="mono">cme-admins</span> gehört (CME_ADMIN_GROUPS) oder sich lokal
        anmeldet, ist Administrator und sieht diese Seite. Alle anderen sehen nur <b>Mein Telefon</b>: die Voicemail ihrer
        Rufnummern, Faxe senden und empfangen, die Kurzwahltasten ihrer Telefone und das gemeinsame Telefonbuch.
        Benutzer erscheinen hier nach der ersten Anmeldung – oder vorab mit ihrer E-Mail-Adresse eintragen.</div></div>
      <table><thead><tr><th>Benutzer</th><th>Rolle</th><th>Zuletzt angemeldet</th><th>Rufnummern</th><th></th></tr></thead><tbody>
      ${D.users.map(u => `<tr><td>${esc(u.name || u.key)}<br><span class="muted mono" style="font-size:12px">${esc(u.key)}</span></td>
        <td>${u.admin ? badge("ok", "Administrator") : badge("neutral", "Benutzer")}</td>
        <td>${fmt(u.ts)}${u.provider ? ` <span class="muted">${esc(u.provider)}</span>` : ""}</td>
        <td class="mono">${u.numbers.length ? u.numbers.map(esc).join("<br>") : '<span class="muted">keine</span>'}</td>
        <td><div class="actions"><button class="small" data-uedit="${esc(u.key)}">${open === u.key ? "Schließen" : "Rufnummern"}</button>
          ${u.key !== D.me ? `<button class="small danger" data-udel="${esc(u.key)}">Entfernen</button>` : ""}</div></td></tr>
        ${open === u.key ? `<tr><td colspan="5"><form data-uform="${esc(u.key)}">
          <div style="display:flex;flex-wrap:wrap;gap:4px 18px;margin:4px 0 10px">${D.numbers.map(n => `<label class="check" style="margin:0">
            <input type="checkbox" name="n" value="${esc(n.number)}" ${u.numbers.includes(n.number) ? "checked" : ""}> ${esc(numLabel(n))}</label>`).join("")
            || '<span class="muted">Keine Rufnummern am Router gefunden.</span>'}</div>
          <div class="actions"><button class="primary small" type="submit">Speichern</button></div>
          <div class="msg"></div></form></td></tr>` : ""}`).join("")
        || `<tr><td colspan="5" class="empty">Noch niemand angemeldet.</td></tr>`}</tbody></table>
      <div class="body" style="border-top:1px solid var(--line)">
        <form id="f-uadd"><div class="row">
          <div><label>Benutzer vorab eintragen (E-Mail-Adresse wie im Identity Provider)</label>
            <input name="key" type="email" required placeholder="name@example.de"></div><div></div></div>
          <div class="actions"><button class="primary" type="submit">Hinzufügen</button></div>
          <div class="msg" id="m-uadd"></div></form>
      </div>`;
  }

  async function load() {
    const j = await api("/api/users");
    if (!j.ok) { card.innerHTML = `<h2>Benutzer</h2><div class="empty">${esc(j.error || "Fehler")}</div>`; return; }
    D = j;
    render();
  }

  card.addEventListener("click", async (e) => {
    const d = e.target.dataset || {};
    if (d.uedit) {
      open = open === d.uedit ? null : d.uedit;
      render();
    } else if (d.udel) {
      if (!confirm(`${d.udel} entfernen? Die Zuordnung der Rufnummern geht verloren.`)) return;
      const j = await api(`/api/users/${encodeURIComponent(d.udel)}`, { method: "DELETE" });
      if (!j.ok) { alert(j.error || "Fehler"); return; }
      load();
    }
  });

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.dataset.uform) {
      const key = f.dataset.uform;
      const numbers = [...f.querySelectorAll("input[name=n]:checked")].map(i => i.value);
      const j = await api(`/api/users/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ numbers }) });
      if (!j.ok) { showMsg(f.querySelector(".msg"), false, j.error || "Fehler"); return; }
      open = null;
      await load();
    } else if (f.id === "f-uadd") {
      const key = f.key.value.trim().toLowerCase();
      if (!D.users.some(u => u.key === key)) D.users.push({ key, name: "", numbers: [], admin: false, ts: 0 });
      open = key;
      render();
    }
  });

  load();
})();
