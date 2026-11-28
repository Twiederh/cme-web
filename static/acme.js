// Tab "Softphone": Let's-Encrypt-Zertifikat für das SIP-Gateway (Kamailio, TLS) per DNS-Challenge. Server: acme.py
// Wie im Nginx Proxy Manager: DNS-Anbieter wählen, Zugangsdaten als NAME=Wert-Zeilen. Helfer aus index.html.
(() => {
  let card = document.getElementById("acme-card");
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = "acme-card";
    document.querySelector("main").appendChild(card);
  }
  card.innerHTML = `<h2>Zertifikat für das SIP-Gateway (Let's Encrypt)</h2><div class="body" id="ac-body"><div class="empty">Lädt …</div></div>`;
  let A = null, timer = null;
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const day = (t) => new Date(t * 1000).toLocaleDateString("de-DE");

  async function load() {
    A = await api("/api/acme");
    render();
    clearTimeout(timer);
    if (A.ok && A.state && A.state.running) timer = setTimeout(load, 3000);
  }

  function template(p) {
    const v = (A.providers[p] || {}).vars || [];
    return v.map(k => `${k}=`).join("\n");
  }

  function render() {
    const b = $("#ac-body");
    if (!A.ok) { b.innerHTML = `<div class="msg bad" style="display:block">${esc(A.error || "Fehler")}</div>`; return; }
    const s = A.settings, c = A.cert, st = A.state || {};
    const known = Object.keys(A.providers).includes(s.provider);
    const certTxt = c
      ? `${badge(c.days > 30 ? "ok" : c.days > 7 ? "warn" : "bad", `gültig bis ${day(c.until)} (${c.days} Tage)`)}
         <span class="mono">${esc(c.names.join(", "))}</span> <span class="muted">${esc(c.issuer)}</span>`
      : badge("neutral", "noch keins von cme-web – Kamailio nutzt das Zertifikat von Traefik");
    const run = st.running ? badge("warn", `${esc(st.action)} läuft …`)
      : st.ok === true ? badge("ok", "zuletzt erfolgreich") : st.ok === false ? badge("bad", "zuletzt fehlgeschlagen") : "";
    b.innerHTML = `
      <div>Zertifikat: ${certTxt}</div>
      <div class="hint">Kamailio nimmt es für TLS (Port 5061) unter dem Gateway-Namen und den SIP-Domains. Ausgestellt wird per DNS-Challenge:
        cme-web legt beim DNS-Anbieter kurz einen TXT-Eintrag <span class="mono">_acme-challenge</span> an – dafür sind keine Ports nötig.
        Alle Namen müssen beim selben Anbieter liegen. Kamailio übernimmt ein neues Zertifikat binnen 20 s, verlängert wird
        automatisch 30 Tage vor Ablauf.</div>
      <form id="f-ac">
        <div><label>Namen im Zertifikat (kommagetrennt, Platzhalter wie *.example.de erlaubt)</label>
          <input name="domains" class="mono" value="${esc(s.domains.join(", "))}" placeholder="${esc(A.suggest.join(", "))}"></div>
        <div class="row">
          <div><label>DNS-Anbieter</label><select name="provider">
            ${Object.entries(A.providers).map(([k, p]) => `<option value="${esc(k)}" ${k === s.provider ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
            <option value="" ${known ? "" : "selected"}>anderer (Name aus der lego-Doku) …</option></select></div>
          <div id="ac-other" style="${known ? "display:none" : ""}"><label>lego-Name des Anbieters</label>
            <input name="other" class="mono" value="${known ? "" : esc(s.provider)}" placeholder="z. B. hostingde"></div>
        </div>
        <div><label>Zugangsdaten (NAME=Wert, eine Zeile je Variable; gespeicherte Werte erscheinen als ********)</label>
          <textarea name="env" class="mono" rows="4" spellcheck="false">${esc(s.env || template(s.provider))}</textarea>
          <div class="hint">Namen der Variablen: <a href="https://go-acme.github.io/lego/dns/" target="_blank" rel="noopener">lego – DNS-Anbieter</a>.
            Bei All-Inkl die KAS-Anmeldung (nicht das FTP-Konto); die KAS-API muss im Kundenmenü erlaubt sein.</div></div>
        <div class="row">
          <div><label>E-Mail-Adresse für das Konto bei Let's Encrypt</label><input name="email" type="email" value="${esc(s.email)}"></div>
          <div><label>DNS-Server für die Prüfung</label><input name="resolver" class="mono" value="${esc(s.resolver)}" placeholder="1.1.1.1:53">
            <div class="hint">Öffentlicher Server – der interne DNS kennt die Zone selbst und sähe den TXT-Eintrag nicht.</div></div>
        </div>
        <label class="check"><input type="checkbox" name="staging" ${s.staging ? "checked" : ""}> Testumgebung von Let's Encrypt (Zertifikat ist nicht vertrauenswürdig, zum Ausprobieren)</label>
        <label class="check"><input type="checkbox" name="auto" ${s.auto ? "checked" : ""}> automatisch verlängern</label>
        <div class="actions"><button class="primary" type="submit">Speichern</button>
          <button type="button" id="ac-issue" ${A.lego && !st.running ? "" : "disabled"}>Speichern und Zertifikat ausstellen</button> ${run}</div>
        <div class="msg" id="m-ac"></div>
        ${st.log ? `<details ${st.ok === false ? "open" : ""}><summary>Protokoll von lego</summary>
          <pre class="mono" style="white-space:pre-wrap;font-size:11.5px;max-height:260px;overflow:auto">${esc(st.log)}</pre></details>` : ""}
        ${A.lego ? "" : `<div class="msg bad" style="display:block">lego fehlt im Image von cme-web.</div>`}
      </form>`;
    const f = $("#f-ac");
    f.provider.onchange = () => {
      $("#ac-other").style.display = f.provider.value ? "none" : "";
      if (f.provider.value && !/=\S/.test(f.env.value)) f.env.value = template(f.provider.value);
    };
    const body = () => ({ domains: f.domains.value, provider: f.provider.value || f.other.value.trim().toLowerCase(),
      env: f.env.value, email: f.email.value.trim(), resolver: f.resolver.value.trim(), staging: f.staging.checked, auto: f.auto.checked });
    f.onsubmit = async (e) => {
      e.preventDefault();
      const r = await post("/api/acme", body());
      showMsg($("#m-ac"), r.ok, r.ok ? "Gespeichert." : r.error);
      if (r.ok) setTimeout(load, 800);
    };
    $("#ac-issue").onclick = async () => {
      let r = await post("/api/acme", body());
      if (r.ok) r = await post("/api/acme/issue", {});
      showMsg($("#m-ac"), r.ok, r.ok ? "Läuft – das kann ein, zwei Minuten dauern (DNS-Eintrag muss sich verbreiten)." : r.error);
      if (r.ok) setTimeout(load, 1500);
    };
  }

  load();
})();
