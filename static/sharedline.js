// Tab "Telefone": Shared Line für Geräte ohne eigenes Abo (z. B. Gigaset). Server: sharedline.py
(() => {
  const card = document.getElementById("sharedline-card");
  if (!card) return;
  card.innerHTML = `<h2>Shared Line für Fremdgeräte</h2><div class="body" id="sl-body"><div class="empty">Lädt …</div></div>`;

  const since = (t) => t ? new Date(t * 1000).toLocaleString("de-DE") : "–";

  async function load() {
    const j = await api("/api/sharedline");
    const b = $("#sl-body");
    if (!j.ok) { b.innerHTML = `<div class="msg bad" style="display:block">${esc(j.error || "Fehler")}</div>`; return; }
    const subs = j.subs || [];
    b.innerHTML = `
      <div class="hint">CME klingelt eine Shared Line nur an Geräten an, die die Leitung selbst abonnieren
        (Cisco-Telefone, CME Phone). Für andere Geräte auf einer Shared Line – etwa eine Gigaset-Basis – hält
        cme-web das Abo stellvertretend, damit sie mitklingeln. Das Gerät muss dazu mit der Nummer angemeldet sein.</div>
      <label class="check"><input type="checkbox" id="sl-enabled" ${j.enabled ? "checked" : ""}>
        Shared-Line-Abos für Fremdgeräte halten</label>
      ${j.error ? `<div class="msg bad" style="display:block">${esc(j.error)}</div>` : ""}
      <div class="kv"><span class="muted">Port für CME</span><span class="mono">${esc(j.host || "?")}:${esc(j.port)} (UDP)</span></div>
      ${(j.mobile || []).length ? `<h3>Mobilgeräte (Pools MOB…, Ziel ${esc(j.flexisip || "Gateway")})</h3>
        <table><thead><tr><th>Rufnummer</th><th>Pool</th><th>Anmeldung an CME</th><th>Letzte App-Anmeldung</th></tr></thead><tbody>
        ${j.mobile.map(m => `<tr><td class="mono">${esc(m.number)}</td><td class="mono">${esc(m.device)}</td>
          <td>${m.state === "angemeldet" ? badge("ok", "angemeldet") : badge("bad", esc(m.error || m.state))}</td>
          <td>${since(m.apps)}</td></tr>`).join("")}</tbody></table>` : ""}
      <h3>Abos</h3>
      ${subs.length ? `<table><thead><tr><th>Rufnummer</th><th>Gerät</th><th>Pool</th><th>Status</th><th>Seit</th></tr></thead><tbody>
        ${subs.map(s => `<tr><td class="mono">${esc(s.number)}</td><td class="mono">${esc(s.ip)}</td><td>${esc(s.pool)}</td>
          <td>${s.state === "aktiv" ? badge("ok", "aktiv") : badge("bad", esc(s.error || s.state))}</td>
          <td>${since(s.since)}</td></tr>`).join("")}</tbody></table>`
        : `<div class="empty">Kein Fremdgerät auf einer Shared Line${j.enabled ? "" : " (ausgeschaltet)"}.</div>`}`;
  }

  card.addEventListener("change", async (e) => {
    if (e.target.id !== "sl-enabled") return;
    const r = await api("/api/sharedline", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) });
    if (!r.ok) alert(r.error || "Fehler");
    setTimeout(load, 1500);
  });

  load();
  setInterval(() => { if (!card.closest("[hidden]")) load(); }, 30000);
})();
