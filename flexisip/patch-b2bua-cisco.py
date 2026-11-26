#!/usr/bin/env python3
"""Patch fuer den Flexisip-B2BUA (2.6.2) im Betrieb mit Cisco CME (re-INVITEs, Video, Apps-Domain, IPv4). Aufruf: patch-b2bua-cisco.py <flexisip-quellordner>

Bei Halten, Fortsetzen und anderen re-INVITEs baut der B2BUA die Parameter der Gegenseite mit
createCallParams(peerCall). Das kopiert die eigenen Header des ersten INVITE mit - nach einem Uebernehmen also
"Replaces", und solche re-INVITEs verwirft CME ("invalid IE content"). Ausserdem fehlt dann
"Call-Info: <urn:x-cisco-remotecc:hold|resume>", an dem CME ein auf der Shared Line gehaltenes Gespraech erkennt.

Deshalb: die Parameter wie bisher von der Gegenseite ableiten (Bindung an die Konferenz bleibt), "Replaces" und
ein frueheres "Call-Info" entfernen (kleine C-Funktion in liblinphone) und "Call-Info" mit remotecc aus der Anfrage
dieser Seite neu setzen.
Bricht ab, wenn der Quelltext nicht mehr wie erwartet aussieht (Flexisip-Update: Patch pruefen).
"""
import sys
from pathlib import Path

src = Path(sys.argv[1]) / "src/b2bua/b2bua-server.cc"
s = src.read_text()

helper_anchor = "/**\n * Return if the call media direction is set to the provided direction."
helper = '''// liblinphone (cme-web-Patch, c-call-params.cpp): eigenen SIP-Header aus CallParams entfernen
extern "C" void cme_linphone_call_params_remove_custom_header(void *params, const char *name);

/**
 * CME (Cisco): Parameter fuer ein re-INVITE der Gegenseite. createCallParams(peerCall) behaelt die Bindung an die
 * Konferenz des B2BUA (frische Parameter verlieren den Audio-Stream: 488), kopiert aber die eigenen Header des
 * ersten INVITE mit - nach einem Uebernehmen "Replaces", das CME in re-INVITEs verwirft - und ein frueheres
 * "Call-Info". Beides entfernen und "Call-Info" mit remotecc aus der Anfrage dieser Seite neu setzen
 * (Halten/Fortsetzen auf der Shared Line).
 */
std::shared_ptr<linphone::CallParams> cmeUpdateParams(const std::shared_ptr<linphone::Core>& core,
                                                      const std::shared_ptr<linphone::Call>& peerCall,
                                                      const std::shared_ptr<linphone::Call>& call) {
	auto params = core->createCallParams(peerCall);
	void* cParams = linphone::Object::sharedPtrToCPtr(params);
	for (const char* name : {"Replaces", "Call-Info"}) {
		for (int i = 0; i < 8 && !params->getCustomHeader(name).empty(); ++i)
			cme_linphone_call_params_remove_custom_header(cParams, name);
	}
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

# UPDATE ohne SDP: CME schickt nach dem Annehmen/Uebernehmen ein UPDATE nur mit Remote-Party-ID/Call-Info. Ohne SDP
# meldet liblinphone Audio und Video der Gegenseite als "aus"; der B2BUA schaltete daraufhin das Audio der anderen
# Seite ab (re-INVITE mit m=audio 0). Das naechste Halten scheiterte dann mit 488 (kein Audio-Stream mehr).
# Deshalb Audio/Video nur umschalten, wenn die Anfrage ueberhaupt Medien beschreibt.
s = replace_once(s, """	if (remoteCallParams->videoEnabled() != callParams->videoEnabled()) {""",
                 """	// CME: UPDATE/re-INVITE ohne SDP aendert keine Medien
	const bool cmeRemoteHasMedia = remoteCallParams->audioEnabled() || remoteCallParams->videoEnabled();
	if (cmeRemoteHasMedia && remoteCallParams->videoEnabled() != callParams->videoEnabled()) {
		// CME: Konferenz ohne Video angelegt (Anruf nur mit Audio) - fuer nachtraeglich eingeschaltetes Video die
		// Konferenz erst video-faehig machen, sonst bietet der B2BUA der Gegenseite kein Video an
		if (remoteCallParams->videoEnabled()) {
			if (const auto conference = call->getConference()) {
				if (const auto confParams = conference->getCurrentParams(); confParams && !confParams->videoEnabled()) {
					auto newConfParams = confParams->clone();
					newConfParams->enableVideo(true);
					conference->updateParams(newConfParams);
					LOGD << "CME: Video in der Konferenz eingeschaltet";
				}
			}
		}""",
                 "Video-Umschaltung bei UPDATE")
s = replace_once(s, """	if (remoteCallParams->audioEnabled() != callParams->audioEnabled()) {""",
                 """	if (cmeRemoteHasMedia && remoteCallParams->audioEnabled() != callParams->audioEnabled()) {""",
                 "Audio-Umschaltung bei UPDATE")

src.write_text(s)
print(f"patch-b2bua-cisco: {src} angepasst ({n} re-INVITE-Stellen, Video, Apps-Domain, UPDATE ohne SDP)")

# liblinphone: CallSessionParams::removeCustomHeader() gibt es nur intern - als C-Funktion fuer den B2BUA freigeben
cp_src = Path(sys.argv[1]) / "linphone-sdk/liblinphone/src/c-wrapper/api/c-call-params.cpp"
cp = cp_src.read_text()
if "void linphone_call_params_add_custom_header(LinphoneCallParams *params," not in cp:
    sys.exit("patch-b2bua-cisco: c-call-params.cpp sieht anders aus als erwartet")
cp += """
// cme-web: eigenen SIP-Header entfernen (fuer den Flexisip-B2BUA, patch-b2bua-cisco.py)
extern "C" LINPHONE_PUBLIC void cme_linphone_call_params_remove_custom_header(void *params, const char *name) {
	L_GET_CPP_PTR_FROM_C_OBJECT(static_cast<LinphoneCallParams *>(params))->removeCustomHeader(name);
}
"""
cp_src.write_text(cp)
print(f"patch-b2bua-cisco: {cp_src} angepasst (removeCustomHeader)")

# Nur IPv4: sonst traegt liblinphone eine IPv6-Adresse als Contact ein (CME spricht nur IPv4).
# Audio-Codecs: G.722 ist in liblinphone standardmaessig aus. CME bietet G.722 zuerst an, der B2BUA nahm dann PCMA.
# Uebernimmt danach Mac oder 8845 mit G.722, kann CME ohne Transcoder nicht umsetzen und legt auf. Deshalb
# G.722 einschalten und vorn einreihen (danach PCMA, PCMU, Rest wie gehabt). Eine Option "audio-codec" wuerde
# nur genau einen Codec zulassen - fuer Anrufe nach draussen zu riskant.
core_src = Path(sys.argv[1]) / "src/b2bua/b2bua-core.cc"
c = core_src.read_text()
c = replace_once(c, '	core->setLabel("Flexisip B2BUA");\n',
                 '''	core->setLabel("Flexisip B2BUA");
	core->enableIpv6(false);
	{
		std::list<std::shared_ptr<linphone::PayloadType>> first, rest;
		for (const auto& mime : {"G722", "PCMA", "PCMU"}) {
			for (const auto& pt : core->getAudioPayloadTypes()) {
				if (string_utils::iequals(pt->getMimeType(), mime)) {
					pt->enable(true);
					first.push_back(pt);
				}
			}
		}
		for (const auto& pt : core->getAudioPayloadTypes()) {
			const auto m = pt->getMimeType();
			if (!string_utils::iequals(m, "G722") && !string_utils::iequals(m, "PCMA") &&
			    !string_utils::iequals(m, "PCMU"))
				rest.push_back(pt);
		}
		first.splice(first.end(), rest);
		core->setAudioPayloadTypes(first);
	}
''', "IPv6 abschalten, G.722 vorn")
core_src.write_text(c)
print(f"patch-b2bua-cisco: {core_src} angepasst (nur IPv4, G.722 vorn)")
