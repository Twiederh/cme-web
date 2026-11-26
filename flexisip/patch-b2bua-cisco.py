#!/usr/bin/env python3
"""Patch fuer den Flexisip-B2BUA (2.6.2) im Betrieb mit Cisco CME. Aufruf: patch-b2bua-cisco.py <flexisip-quellordner>

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
src.write_text(s)
print(f"patch-b2bua-cisco: {src} angepasst ({n} re-INVITE-Stellen)")
