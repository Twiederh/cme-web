#!/bin/zsh
# CME Phone – beide Projekte erzeugen und bauen (iOS + macOS).
# Doppelklick im Finder oder: ./build.command
# Logs: build-logs/*.log
cd "$(dirname "$0")"
mkdir -p build-logs
LOG=build-logs/summary.log
: > "$LOG"
say() { echo "$@" | tee -a "$LOG"; }

say "=== $(date) ==="
if ! command -v xcodegen >/dev/null 2>&1; then
  say "XcodeGen fehlt – installiere über Homebrew …"
  if command -v brew >/dev/null 2>&1; then brew install xcodegen 2>&1 | tail -3 | tee -a "$LOG"
  else say "FEHLER: Homebrew nicht gefunden"; exit 1; fi
fi
say "xcodegen $(xcodegen --version 2>/dev/null)"

# xcode-select zeigt evtl. auf die Command Line Tools – dann Xcode.app suchen und nur für diesen Lauf nutzen
if ! xcodebuild -version >/dev/null 2>&1; then
  XC=""
  for c in /Applications/Xcode.app /Applications/Xcode-beta.app $(mdfind "kMDItemCFBundleIdentifier == 'com.apple.dt.Xcode'" 2>/dev/null); do
    [ -d "$c/Contents/Developer" ] && { XC="$c"; break; }
  done
  if [ -z "$XC" ]; then say "FEHLER: Xcode.app nicht gefunden"; exit 1; fi
  export DEVELOPER_DIR="$XC/Contents/Developer"
  say "nutze $XC (DEVELOPER_DIR) – dauerhaft: sudo xcode-select -s $DEVELOPER_DIR"
fi
say "$(xcodebuild -version | head -1)"


say "--- Projekte erzeugen"
# iOS-Spec ohne bzw. mit Local Push (Blöcke "# >>> LOCALPUSH" … "# <<< LOCALPUSH" in project.yml):
# ohne Freigabe von Apple muss die Local-Push-Extension ganz raus, sonst lehnt TestFlight den Upload ab.
if [ "${LOCALPUSH:-0}" = "1" ]; then
  grep -v -E '^[[:space:]]*# (>>>|<<<) LOCALPUSH' project.yml > project.gen.yml
else
  awk '/# >>> LOCALPUSH/{skip=1;next} /# <<< LOCALPUSH/{skip=0;next} !skip' project.yml > project.gen.yml
fi
xcodegen generate --spec project.gen.yml > build-logs/xcodegen-ios.log 2>&1 && say "iOS-Projekt OK"  || say "iOS-Projekt FEHLER (siehe xcodegen-ios.log)"
xcodegen generate --spec project-mac.yml > build-logs/xcodegen-mac.log 2>&1 && say "Mac-Projekt OK"  || say "Mac-Projekt FEHLER (siehe xcodegen-mac.log)"

# Local Push (NetworkExtension app-push-provider) vergibt Apple nur auf Antrag. Erst nach der Freigabe:
#   LOCALPUSH=1 zsh build.command   (trägt das Entitlement in die erzeugten Entitlements ein)
if [ "${LOCALPUSH:-0}" = "1" ]; then
  for f in CMEPhone/Resources/CMEPhone.entitlements LocalPushProvider/LocalPushProvider.entitlements; do
    [ -f "$f" ] || continue
    /usr/libexec/PlistBuddy -c "Delete :com.apple.developer.networking.networkextension" "$f" >/dev/null 2>&1
    /usr/libexec/PlistBuddy -c "Add :com.apple.developer.networking.networkextension array" \
      -c "Add :com.apple.developer.networking.networkextension:0 string app-push-provider" "$f"
  done
  say "Local Push: an"
else
  say "Local Push: aus (Entitlement nicht freigegeben; LOCALPUSH=1 schaltet es ein)"
fi

DD="$PWD/build/DerivedData"

# Standard: ohne Signierung kompilieren (prüft den Code, braucht keinen Apple-Account in Xcode).
# Signiert bauen (Profile automatisch anlegen):  SIGN=1 zsh build.command
if [ "${SIGN:-0}" = "1" ]; then
  # -allowProvisioningDeviceRegistration: trägt diesen Mac selbst im Developer-Account ein
  SIGNARGS=(-allowProvisioningUpdates -allowProvisioningDeviceRegistration)
  say "Signierung: an (Team 83BLV56527)"
else
  SIGNARGS=(CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="")
  say "Signierung: aus (nur Kompilieren)"
fi

say "--- macOS bauen"
xcodebuild -project CMEPhoneMac.xcodeproj -scheme CMEPhoneMac -configuration Debug \
  -derivedDataPath "$DD" "${SIGNARGS[@]}" build > build-logs/build-mac.log 2>&1
MAC=$?
say "macOS: $([ $MAC -eq 0 ] && echo ERFOLG || echo "FEHLER ($MAC)")"
grep -E "error:|warning: .*deprecated" build-logs/build-mac.log | sort -u | head -60 >> "$LOG"

say "--- iOS bauen (generisches Gerät)"
xcodebuild -project CMEPhone.xcodeproj -scheme CMEPhone -configuration Debug \
  -destination 'generic/platform=iOS' -derivedDataPath "$DD" "${SIGNARGS[@]}" build > build-logs/build-ios.log 2>&1
IOS=$?
say "iOS: $([ $IOS -eq 0 ] && echo ERFOLG || echo "FEHLER ($IOS)")"
grep -E "error:" build-logs/build-ios.log | sort -u | head -60 >> "$LOG"

say "=== fertig: macOS=$MAC iOS=$IOS ==="
[ $MAC -eq 0 ] && say "Mac-App: $DD/Build/Products/Debug/CMEPhone.app"
