// Telefontyp "CME Softphone" (CME Phone, https://gitea.wiederhol.de/thomas/cme-softphone) im Formular
// "Telefon anlegen". CME nimmt im CME-Modus nur MAC-Adressen oder - fuer Softclients - einen Geraetenamen an;
// das Softphone meldet sich deshalb wie Jabber mit Geraetenamen (CMEP...) an.
// Haengt sich in index.html ein (TYPE_LABEL, updateTypeHint, META).
(() => {
  const f = document.getElementById("f-phone");
  if (!f || document.getElementById("softphone-hint")) return;
  const SOFT = "CME-Softphone";
  TYPE_LABEL[SOFT] = "CME Phone (eigenes Softphone)";
  const opt = [...document.querySelectorAll("#sel-type option")].find(o => o.value === SOFT);
  if (opt) opt.textContent = TYPE_LABEL[SOFT];

  const hint = document.createElement("div");
  hint.className = "hint";
  hint.id = "softphone-hint";
  hint.hidden = true;
  hint.innerHTML = `<b>CME Phone</b> (eigenes Softphone für iPhone, iPad und Mac) meldet sich wie Jabber mit einem
    <b>Gerätenamen</b> an – CME nimmt im CME-Modus nur MAC-Adressen oder Gerätenamen an. Der Name ist <span class="mono">CMEP</span> + Nebenstelle
    (z. B. <span class="mono">CMEP21</span>) – genau so schickt ihn die App bei der Anmeldung mit. Am Router wird der Pool als <span class="mono">type Jabber-CSF-Client</span> mit
    <span class="mono">id device-id-name</span> angelegt. In der App außerdem: <b>Nebenstelle</b> = Rufnummer der ersten
    Leitung, <b>CME-Adresse</b> = IP des Routers, Push-Gateway <span class="mono">sip-gw.wiederhol.de:5062</span> (TCP).`;
  document.getElementById("jabber-hint").after(hint);
  const devInput = f.device_id;
  const devLabel = document.querySelector("#wrap-device label");
  const jabberLabel = devLabel ? devLabel.textContent : "";

  function softHint() {
    const soft = f.type.value === SOFT;
    hint.hidden = !soft;
    if (!soft) {
      if (devLabel) devLabel.textContent = jabberLabel;
      devInput.placeholder = "CSFTHOMAS";
      return;
    }
    document.getElementById("wrap-mac").hidden = true;
    document.getElementById("wrap-device").hidden = false;
    f.mac.required = false;
    devInput.required = true;
    devInput.placeholder = "CMEP21";
    if (devLabel) devLabel.textContent = "Gerätename (wie in der App) *";
    if (!devInput.value) {
      // Vorschlag aus der ersten gewaehlten Rufnummer
      const first = f.querySelector("select[name=dn], [name^=dn]");
      const num = first && first.selectedOptions && first.selectedOptions[0] ? first.selectedOptions[0].textContent.match(/\+?\d+/) : null;
      devInput.value = (META.softphone_prefix || "CMEP") + (num ? num[0].replace("+", "") : "");
    }
    document.getElementById("mobility-row").hidden = true;
    f.mobility_key.checked = false;
  }
  const origHint = updateTypeHint;
  updateTypeHint = function () { origHint(); softHint(); };
  f.type.addEventListener("change", softHint);
})();
