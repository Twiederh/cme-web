// Telefontyp "CME Softphone" (CME Phone, https://gitea.wiederhol.de/thomas/cme-softphone) im Formular
// "Telefon anlegen". Das Softphone meldet sich ueber den Push-Gateway Flexisip an; CME ordnet den Pool ueber
// "id network <Gateway-IP>" zu. Haengt sich in index.html ein (TYPE_LABEL, updateTypeHint, startEdit, META).
(() => {
  const f = document.getElementById("f-phone");
  if (!f || f.gw_ip) return;
  const SOFT = "CME-Softphone";
  TYPE_LABEL[SOFT] = "CME Phone (Softphone über Flexisip)";
  const opt = [...document.querySelectorAll("#sel-type option")].find(o => o.value === SOFT);
  if (opt) opt.textContent = TYPE_LABEL[SOFT];

  const wrap = document.createElement("div");
  wrap.id = "wrap-gw";
  wrap.hidden = true;
  wrap.innerHTML = `<label>IP des Push-Gateways (Flexisip) *</label><input name="gw_ip" class="mono" placeholder="192.168.1.110">`;
  document.getElementById("wrap-device").after(wrap);

  const hint = document.createElement("div");
  hint.className = "hint";
  hint.id = "softphone-hint";
  hint.hidden = true;
  hint.innerHTML = `<b>CME Phone</b> (eigenes Softphone für iPhone, iPad und Mac) meldet sich über den Push-Gateway
    <b>Flexisip</b> an. Alle Softphones kommen von dessen IP; CME ordnet sie über
    <span class="mono">id network &lt;IP&gt;</span> und die Nummer im REGISTER zu – ohne MAC.
    In der App eintragen: <b>Nebenstelle</b> = Rufnummer der ersten Leitung, <b>CME-Adresse</b> = IP des Routers,
    Push-Gateway <span class="mono">sip-gw.wiederhol.de:5062</span> (TCP). Ein Passwort braucht die App nur, wenn unter
    <span class="mono">voice register global</span> <span class="mono">authenticate register</span> gesetzt ist –
    dann das SIP-Passwort von unten. Als Codec am besten eine Liste mit G.722 vor G.711.`;
  document.getElementById("jabber-hint").after(hint);

  function softHint() {
    const soft = f.type.value === SOFT;
    hint.hidden = !soft;
    wrap.hidden = !soft;
    f.gw_ip.required = soft;
    if (soft) {
      if (!f.gw_ip.value) f.gw_ip.value = META.softphone_gw || "";
      document.getElementById("wrap-mac").hidden = true;
      f.mac.required = false;
      document.getElementById("mobility-row").hidden = true;
      f.mobility_key.checked = false;
    }
  }
  const origHint = updateTypeHint;
  updateTypeHint = function () { origHint(); softHint(); };
  f.type.addEventListener("change", softHint);

  const origEdit = startEdit;
  startEdit = function (tag) {
    const p = (data.phones || []).find(x => x.tag === tag);
    f.gw_ip.value = "";
    origEdit(tag);
    if (p && p.id_network) f.gw_ip.value = p.id_ip || "";
    softHint();
  };
})();
