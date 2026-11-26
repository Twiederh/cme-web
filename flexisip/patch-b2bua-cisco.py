#!/usr/bin/env python3
"""Patch fuer den Flexisip-B2BUA (2.6.2) im Betrieb mit Cisco CME (re-INVITEs, Video, Apps-Domain, IPv4). Aufruf: patch-b2bua-cisco.py <flexisip-quellordner>

Bei Halten, Fortsetzen und anderen re-INVITEs baut der B2BUA die Parameter der Gegenseite mit
createCallParams(peerCall). Das kopiert die eigenen Header des ersten INVITE mit - nach einem Uebernehmen also
"Replaces", und solche re-INVITEs verwirft CME ("invalid IE content"). Ausserdem fehlt dann
"Call-Info: <urn:x-cisco-remotecc:hold|resume>", an dem CME ein auf der Shared Line gehaltenes Gespraech erkennt.

Deshalb: fuer re-INVITEs frische Parameter (Audio/Video, Verschluesselung, Konto der Gegenseite) und
"Call-Info" mit remotecc aus der Anfrage dieser Seite uebernehmen.
Bricht ab, wenn der Quelltext nicht mehr wie erwartet aussieht (Flexisip-Update: Patch pruefen).
"""
import sys
from pathlib import Path

src = Path(sys.argv[1]) / "src/b2bua/b2bua-server.cc"
s = src.read_text()

helper_anchor = "/**\n * Return if the call media direction is set to the provided direction."
helper = '''/**
 * CME (Cisco): Parameter fuer ein re-INVITE der Gegenseite - frisch statt createCallParams(peerCall) (das kopiert
 * "Replaces" aus dem ersten INVITE mit, CME verwirft das), dazu "Call-Info" mit remotecc aus der Anfrage dieser Seite
 * (Halten/Fortsetzen auf der Shared Line).
 */
std::shared_ptr<linphone::CallParams> cmeUpdateParams(const std::shared_ptr<linphone::Core>& core,
                                                      const std::shared_ptr<linphone::Call>& peerCall,
                                                      const std::shared_ptr<linphone::Call>& call) {
	const auto current = peerCall->getCurrentParams();
	auto params = core->createCallParams(nullptr);
	params->enableAudio(current->audioEnabled());
	params->enableVideo(current->videoEnabled());
	params->setMediaEncryption(current->getMediaEncryption());
	if (const auto account = peerCall->getParams()->getAccount()) params->setAccount(account);
	if (call && call->getRemoteParams()) {
		const auto callInfo = call->getRemoteParams()->getCustomHeader("Call-Info");
		if (callInfo.find("remotecc") != std::string::npos) params->addCustomHeader("Call-Info", callInfo);
	}
	return params;
}

'''
if s.count(helper_anchor) != 1:
    sys.exit("patch-b2bua-cisco: Einfuegestelle fuer cmeUpdateParams nicht gefunden")
s = s.replace(helper_anchor, helper + helper_anchor, 1)

old = "const auto peerCallParams = mCore->createCallParams(peerCall);"
new = "const auto peerCallParams = cmeUpdateParams(mCore, peerCall, call);"
n = s.count(old)
if n != 5:
    sys.exit(f"patch-b2bua-cisco: {n} statt 5 Stellen mit createCallParams(peerCall) fuer re-INVITEs")
s = s.replace(old, new)

def replace_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        sys.exit(f"patch-b2bua-cisco: Stelle fuer {what} nicht (eindeutig) gefunden")
    return text.replace(old, new, 1)

# Video nur, wenn die anrufende Seite es anbietet: die versteckte Konferenz wird sonst immer mit Video angelegt
# und bietet dem iPhone Video an, auch wenn CME nur Audio geschickt hat (iPhone schickt dann H.265 ins Leere).
s = replace_once(s, "conferenceParams->enableVideo(true);",
                 "conferenceParams->enableVideo(remoteParams->videoEnabled());", "Video der Konferenz")

# Anrufe an die Apps: CME ruft den Pool unter der IP des Docker-Hosts an (To = sip:<nummer>@<HOST_IP>). Diese IP
# darf im Proxy kein Alias sein (sonst haelt er den B2BUA auf HOST_IP:5064 fuer sich selbst: 482 Loop Detected).
# Deshalb leitet der B2BUA solche Anrufe unter der Apps-Domain (Gateway-Name, Alias des Proxys) weiter.
s = replace_once(s, "	if (callee == nullptr) return;\n",
                 """	if (callee == nullptr) return;
	if (const char *fromHost = getenv("B2BUA_REWRITE_HOST"), *appDomain = getenv("B2BUA_APP_DOMAIN");
	    fromHost && appDomain && *fromHost && *appDomain && callee->getDomain() == fromHost) {
		auto rewritten = callee->clone();
		rewritten->setDomain(appDomain);
		rewritten->setPort(0);
		LOGD << "CME: Anruf an die Apps unter " << rewritten->asStringUriOnly();
		callee = rewritten;
	}
""", "Ziel der Anrufe an die Apps")
if s.count("const auto callee = Match(") != 1:
    sys.exit("patch-b2bua-cisco: callee-Definition nicht gefunden")
s = s.replace("const auto callee = Match(", "auto callee = Match(", 1)

src.write_text(s)
print(f"patch-b2bua-cisco: {src} angepasst ({n} re-INVITE-Stellen, Video, Apps-Domain)")

# Nur IPv4: sonst traegt liblinphone eine IPv6-Adresse als Contact ein (CME spricht nur IPv4)
core_src = Path(sys.argv[1]) / "src/b2bua/b2bua-core.cc"
c = core_src.read_text()
c = replace_once(c, '	core->setLabel("Flexisip B2BUA");\n',
                 '	core->setLabel("Flexisip B2BUA");\n	core->enableIpv6(false);\n', "IPv6 abschalten")
core_src.write_text(c)
print(f"patch-b2bua-cisco: {core_src} angepasst (nur IPv4)")
