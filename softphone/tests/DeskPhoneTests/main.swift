import Foundation
var fails = 0
func check(_ c: Bool, _ m: String) { print(c ? "✓" : "✗", m); if !c { fails += 1 } }

// Execute XML
let xml = CiscoXML.executeXML([.dial("0711 123\"<x>"), .key("Speaker"), .sendDigits("12#", line: 1), .raw("Key:Hold")])
print(xml)
check(xml.contains("URL=\"Dial:0711123x\""), "Dial bereinigt (keine Quotes/Klammern)")
check(xml.contains("URL=\"Key:Speaker\""), "Key-Präfix ergänzt")
check(xml.contains("SendDigits:12#:1"), "SendDigits")
check(!xml.contains("Key:Hold"), "max. 3 Items")
let body = String(decoding: CiscoXML.formBody([.dial("10")]), as: UTF8.self)
check(body.hasPrefix("XML=%3C%3Fxml") && !body.contains("<"), "Formular-Kodierung")

// Responses
let ok = CiscoXML.parseResponse(Data(#"<?xml version="1.0" encoding="utf-8"?><CiscoIPPhoneResponse><ResponseItem Status="0" Data="Success" URL="Dial:10"/></CiscoIPPhoneResponse>"#.utf8))
check(ok.isOK, "Response OK")
let err = CiscoXML.parseResponse(Data(#"<CiscoIPPhoneError Number="4" />"#.utf8))
check(err == .error(number: 4, text: CiscoXML.errorText(4)), "Error 4 erkannt: \(err.message)")
let bad = CiscoXML.parseResponse(Data(#"<CiscoIPPhoneResponse><ResponseItem Status="6" Data="" URL="Key:Speaker"/></CiscoIPPhoneResponse>"#.utf8))
check(!bad.isOK, "Status != 0 -> nicht OK: \(bad.message)")
check(!CiscoXML.parseResponse(Data("<html>404</html>".utf8)).isOK, "HTML -> unexpected")

// Dialog-info
let ringing = """
<?xml version="1.0"?>
<dialog-info xmlns="urn:ietf:params:xml:ns:dialog-info" version="3" state="full" entity="sip:10@192.168.11.4">
 <dialog id="a1" direction="recipient"><state>early</state>
  <remote><identity display="Max Muster">sip:0711999@192.168.11.4;user=phone</identity></remote>
 </dialog>
</dialog-info>
"""
let r = DialogInfoParser.parse(ringing)
check(r.state == .ringing && r.remoteNumber == "0711999" && r.remoteName == "Max Muster", "dialog early/recipient -> klingelt \(r)")
let busy = ringing.replacingOccurrences(of: "<state>early</state>", with: "<state>confirmed</state>")
check(DialogInfoParser.parse(busy).state == .busy, "confirmed -> Gespräch")
let out = ringing.replacingOccurrences(of: "recipient", with: "initiator")
check(DialogInfoParser.parse(out).state == .dialing, "early/initiator -> wählt")
check(DialogInfoParser.parse(#"<dialog-info xmlns="urn:ietf:params:xml:ns:dialog-info" version="4" state="full" entity="sip:10@x"/>"#).state == .idle, "leer -> frei")
let term = ringing.replacingOccurrences(of: "<state>early</state>", with: "<state>terminated</state>")
check(DialogInfoParser.parse(term).state == .idle, "terminated -> frei")
let pidfBusy = """
<presence xmlns="urn:ietf:params:xml:ns:pidf" xmlns:rpid="urn:ietf:params:xml:ns:pidf:rpid" entity="sip:10@x">
<tuple id="t1"><status><basic>open</basic></status></tuple>
<dm:person xmlns:dm="urn:ietf:params:xml:ns:pidf:data-model"><rpid:activities><rpid:on-the-phone/></rpid:activities></dm:person>
</presence>
"""
check(DialogInfoParser.parse(pidfBusy).state == .busy, "PIDF on-the-phone -> Gespräch")
check(DialogInfoParser.parse(#"<presence xmlns="urn:ietf:params:xml:ns:pidf"><tuple id="t"><status><basic>open</basic></status></tuple></presence>"#).state == .idle, "PIDF open -> frei")
check(DialogInfoParser.parse("kaputt<").state == .unknown, "Müll -> unbekannt")
print(fails == 0 ? "ALLE TESTS OK" : "\(fails) FEHLER"); exit(fails == 0 ? 0 : 1)
