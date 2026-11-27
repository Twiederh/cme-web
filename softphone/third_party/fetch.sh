#!/bin/zsh
# Lädt die Quellen für die SIP-Schicht der Apps nach third_party/src/ (flache Klone, feste Versionen).
#   re, baresip  BSD-3   github.com/baresip
#   libg722      CMU (unbeschränkt) / gemeinfrei   github.com/sippy/libg722
#   opus         BSD-3   github.com/xiph/opus
# Danach: zsh third_party/build.sh
set -eu
cd "${0:A:h}"
mkdir -p src
get() {   # name url tag commit
  if [ ! -d src/$1 ]; then
    git -c advice.detachedHead=false clone -q --depth 1 --branch $3 $2 src/$1
  fi
  local have=$(git -C src/$1 rev-parse --short=7 HEAD)
  [ "$have" = "$4" ] || { echo "FEHLER: src/$1 steht auf $have, erwartet $4 ($3)"; exit 1; }
  echo "$1 $3 ($have)"
}
get re       https://github.com/baresip/re.git      v4.13.0  b827bca
get baresip  https://github.com/baresip/baresip.git v4.13.0  8a4f18c
get libg722  https://github.com/sippy/libg722.git   v_1_2_0  64c49ac
get opus     https://github.com/xiph/opus.git       v1.6.1   22244de

# Eigene Erweiterungen (patches/): Contact-Parameter für Push/Gerätename, eigene Header im re-INVITE
for p in re baresip; do
  f=patches/$p-cmephone.patch
  if git -C src/$p apply --check ../../$f 2>/dev/null; then
    git -C src/$p apply ../../$f && echo "$p: Patch eingespielt"
  elif git -C src/$p apply --reverse --check ../../$f 2>/dev/null; then
    echo "$p: Patch schon drin"
  else
    echo "FEHLER: $f passt nicht zu src/$p"; exit 1
  fi
done
