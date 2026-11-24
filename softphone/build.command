#!/bin/zsh
# CME Phone – beide Projekte erzeugen und bauen (iOS + macOS).
# Doppelklick im Finder oder: ./build.command
#   nur iOS:   ONLY=ios zsh build.command   (die laufende Mac-App bleibt unberührt)
#   nur Mac:   ONLY=mac zsh build.command
# Die Mac-App wird nach dem Bauen nach /Applications/CMEPhone.app kopiert und neu gestartet
# (INSTALL=0 schaltet das ab). Starte sie immer von dort – nicht aus build/DerivedData: dort ersetzt jeder
# Build das Bundle, und macOS beendet die laufende App dabei.
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


# Build-Nummer aus Git: Anzahl Commits (steigt mit jedem Commit), Hash, "+" bei uncommitteten Änderungen, Zeit
BUILD_NO=$(git rev-list --count HEAD 2>/dev/null || echo 0)
BUILD_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
git diff --quiet HEAD -- . 2>/dev/null || BUILD_HASH="$BUILD_HASH+"
BUILD_INFO="Build $BUILD_NO · $BUILD_HASH · $(date '+%d.%m. %H:%M')"
say "$BUILD_INFO"

say "--- Projekte erzeugen"
# iOS-Spec ohne bzw. mit Local Push (Blöcke "# >>> LOCALPUSH" … "# <<< LOCALPUSH" in project.yml):
# ohne Freigabe von Apple muss die Local-Push-Extension ganz raus, sonst lehnt TestFlight den Upload ab.
if [ "${LOCALPUSH:-0}" = "1" ]; then
  grep -v -E '^[[:space:]]*# (>>>|<<<) LOCALPUSH' project.yml > project.gen.yml
else
  awk '/# >>> LOCALPUSH/{skip=1;next} /# <<< LOCALPUSH/{skip=0;next} !skip' project.yml > project.gen.yml
fi
# TestFlight-Build-Nummer = Build-Nummer aus Git (steigt mit jedem Commit) – gilt auch fürs Archivieren in Xcode
sed -i '' -E "s/^( *CURRENT_PROJECT_VERSION: ).*/\1\"$BUILD_NO\"/" project.gen.yml
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
  say "Signierung iOS: an (Team 83BLV56527)"
else
  SIGNARGS=(CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="")
  say "Signierung iOS: aus (nur Kompilieren) – Mac-App wird immer signiert"
fi

MAC=-; IOS=-
# Mac-App immer signiert bauen (Team-Zertifikat aus Xcode): Nur so läuft sie in der Sandbox und liest ihre
# Einstellungen aus ~/Library/Containers/de.wiederhol.cmephone.mac. Unsigniert hätte sie eigene, leere
# Einstellungen (kein Gerätename/Passwort -> 401 Unauthorized). MACSIGN=0 baut unsigniert (nur zum Kompilieren).
if [ "${MACSIGN:-1}" = "1" ]; then
  MACSIGNARGS=(-allowProvisioningUpdates)
else
  MACSIGNARGS=("${SIGNARGS[@]}")
fi
if [ "${ONLY:-}" != "ios" ]; then
  say "--- macOS bauen"
  xcodebuild -project CMEPhoneMac.xcodeproj -scheme CMEPhoneMac -configuration Debug \
    -derivedDataPath "$DD" "${MACSIGNARGS[@]}" \
    CURRENT_PROJECT_VERSION="$BUILD_NO" CME_BUILD_INFO="$BUILD_INFO" build > build-logs/build-mac.log 2>&1
  MAC=$?
  say "macOS: $([ $MAC -eq 0 ] && echo ERFOLG || echo "FEHLER ($MAC)")"
  [ $MAC -ne 0 ] && [ "${MACSIGN:-1}" = "1" ] && grep -q -i -E "error:.*(sign|provision|certificate|team)" build-logs/build-mac.log && \
    say "Hinweis: Signieren fehlgeschlagen – in Xcode unter Settings → Accounts anmelden (Team 83BLV56527)"
  grep -E "error:|warning: .*deprecated" build-logs/build-mac.log | sort -u | head -60 >> "$LOG"
fi

if [ "${ONLY:-}" != "mac" ]; then
  say "--- iOS bauen (generisches Gerät)"
  xcodebuild -project CMEPhone.xcodeproj -scheme CMEPhone -configuration Debug \
    -destination 'generic/platform=iOS' -derivedDataPath "$DD" "${SIGNARGS[@]}" \
    CME_BUILD_INFO="$BUILD_INFO" build > build-logs/build-ios.log 2>&1
  IOS=$?
  say "iOS: $([ $IOS -eq 0 ] && echo ERFOLG || echo "FEHLER ($IOS)")"
  grep -E "error:" build-logs/build-ios.log | sort -u | head -60 >> "$LOG"
fi

# Mac-App installieren: laufende App sauber beenden (meldet sich an der CME ab), nach /Applications kopieren,
# neu starten. So läuft immer eine feste Kopie, die ein neuer Build nicht unter der laufenden App ersetzt.
if [ "$MAC" = "0" ] && [ "${INSTALL:-1}" = "1" ] && [ "${MACSIGN:-1}" = "1" ]; then
  BID=de.wiederhol.cmephone.mac
  SRC="$DD/Build/Products/Debug/CMEPhone.app"
  DEST=/Applications/CMEPhone.app
  if pgrep -f "/CMEPhone.app/Contents/MacOS/" >/dev/null 2>&1; then
    say "Mac-App beenden …"
    osascript -e "tell application id \"$BID\" to quit" >/dev/null 2>&1
    for i in {1..10}; do pgrep -f "/CMEPhone.app/Contents/MacOS/" >/dev/null 2>&1 || break; sleep 0.5; done
  fi
  if ditto "$SRC" "$DEST.new" 2>>"$LOG" && rm -rf "$DEST" && mv "$DEST.new" "$DEST"; then
    say "Mac-App installiert: $DEST (Build $BUILD_NO)"
    open "$DEST"
  else
    rm -rf "$DEST.new"
    say "FEHLER: konnte $DEST nicht ersetzen – Mac-App liegt in $SRC"
  fi
fi

say "=== fertig: macOS=$MAC iOS=$IOS ==="
