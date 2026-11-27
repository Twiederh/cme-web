#!/bin/zsh
# Baut libre (re), libg722 und baresip statisch für macOS, iOS-Gerät und iOS-Simulator
# und packt sie zusammen mit der C-Schicht (cmesip/) in CMESIP.xcframework.
#
#   zsh third_party/build.sh              alle Plattformen
#   SLICES="macos" zsh third_party/build.sh   nur eine (macos | ios | iossim)
#
# Quellen: third_party/fetch.sh (re, baresip v4.13.0, libg722 v_1_2_0, opus v1.6.1) nach third_party/src/.
# Ergebnis: third_party/build/CMESIP.xcframework (Swift: import CMESIP)
set -eu
cd "${0:A:h}"
TP=$PWD
SRC=$TP/src
OUT=$TP/build
SLICES=(${=SLICES:-macos ios iossim})
IOS_MIN=17.0
MAC_MIN=14.0
JOBS=$(sysctl -n hw.ncpu)

# Xcode statt Command Line Tools (wie build.command)
if [ -z "${DEVELOPER_DIR:-}" ] && ! xcrun --sdk iphoneos --show-sdk-path >/dev/null 2>&1; then
  for c in /Applications/Xcode.app /Applications/Xcode-beta.app; do
    [ -d "$c" ] && export DEVELOPER_DIR="$c/Contents/Developer" && break
  done
fi

for d in re baresip libg722 opus; do
  [ -d "$SRC/$d" ] || { echo "FEHLER: $SRC/$d fehlt – erst third_party/fetch.sh"; exit 1; }
done

build_slice() {
  local slice=$1 sdk sys arch min mods
  case $slice in
    macos)  sdk=macosx;          sys=Darwin; arch=arm64; min=$MAC_MIN; mods="g711;libg722;opus;srtp;coreaudio" ;;
    ios)    sdk=iphoneos;        sys=iOS;    arch=arm64; min=$IOS_MIN; mods="g711;libg722;opus;srtp;audiounit" ;;
    iossim) sdk=iphonesimulator; sys=iOS;    arch=arm64; min=$IOS_MIN; mods="g711;libg722;opus;srtp;audiounit" ;;
  esac
  local B=$OUT/$slice P=$OUT/$slice/prefix
  local sysroot=$(xcrun --sdk $sdk --show-sdk-path)
  local cc="xcrun --sdk $sdk clang"
  local minflag
  case $sdk in
    macosx) minflag="-mmacosx-version-min=$min" ;;
    iphoneos) minflag="-miphoneos-version-min=$min" ;;
    iphonesimulator) minflag="-mios-simulator-version-min=$min" ;;
  esac
  rm -rf $B && mkdir -p $P/lib $P/include
  echo "=== $slice ($sdk, $arch)"

  # libg722: zwei C-Dateien, direkt übersetzen
  mkdir -p $B/g722
  for f in g722_encode g722_decode; do
    ${=cc} -arch $arch -isysroot $sysroot $minflag -O2 -I$SRC/libg722 -c $SRC/libg722/$f.c -o $B/g722/$f.o
  done
  xcrun libtool -static -o $P/lib/libg722.a $B/g722/*.o 2>/dev/null
  cp $SRC/libg722/g722*.h $P/include/

  local common=(-G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME=$sys
    -DCMAKE_OSX_SYSROOT=$sysroot -DCMAKE_OSX_ARCHITECTURES=$arch -DCMAKE_OSX_DEPLOYMENT_TARGET=$min
    -DCMAKE_INSTALL_PREFIX=$P -DCMAKE_PREFIX_PATH=$P -DCMAKE_FIND_ROOT_PATH=$P
    -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY -DCMAKE_POSITION_INDEPENDENT_CODE=ON
    -DCMAKE_MACOSX_BUNDLE=OFF)   # iOS: sonst will CMake das baresip-Programm als Bundle installieren

  # libopus (Opus-Codec)
  cmake -S $SRC/opus -B $B/opus ${common[@]} -DOPUS_BUILD_PROGRAMS=OFF -DOPUS_BUILD_TESTING=OFF \
    -DOPUS_BUILD_SHARED_LIBRARY=OFF > $B/opus.log 2>&1 || { tail -30 $B/opus.log; exit 1; }
  cmake --build $B/opus -j $JOBS >> $B/opus.log 2>&1 || { grep -E "error" $B/opus.log | head -20; exit 1; }
  cmake --install $B/opus >> $B/opus.log 2>&1

  # libre: ohne OpenSSL – AES/HMAC über CommonCrypto (SRTP); TLS kommt später.
  # Die OPENSSL_*-Platzhalter braucht nur das Konfigurieren der Testsuite (retest, wird nicht gebaut);
  # im iOS-SDK gibt es kein OpenSSL, CMake bricht sonst ab.
  cmake -S $SRC/re -B $B/re ${common[@]} -DLIBRE_BUILD_SHARED=OFF -DLIBRE_BUILD_STATIC=ON \
    -DUSE_OPENSSL=OFF -DUSE_MBEDTLS=OFF \
    -DOPENSSL_INCLUDE_DIR=$P/include -DOPENSSL_CRYPTO_LIBRARY=$P/lib/libg722.a -DOPENSSL_SSL_LIBRARY=$P/lib/libg722.a \
    > $B/re.log 2>&1 || { tail -30 $B/re.log; exit 1; }
  cmake --build $B/re -j $JOBS >> $B/re.log 2>&1 || { grep -E "error" $B/re.log | head -20; exit 1; }
  cmake --install $B/re >> $B/re.log 2>&1

  # baresip statisch mit den Modulen im Programm (static.c)
  cmake -S $SRC/baresip -B $B/baresip ${common[@]} -DSTATIC=ON -DMODULES="$mods" -DUSE_OPENSSL=OFF \
    -DLIBG722_INCLUDE_DIR=$P/include -DLIBG722_LIBRARY=$P/lib/libg722.a \
    -DRE_INCLUDE_DIR=$P/include/re -DRE_LIBRARY=$P/lib/libre.a \
    -DOPUS_INCLUDE_DIR=$P/include -DOPUS_LIBRARY=$P/lib/libopus.a \
    -DOPENSSL_INCLUDE_DIR=$P/include -DOPENSSL_CRYPTO_LIBRARY=$P/lib/libg722.a -DOPENSSL_SSL_LIBRARY=$P/lib/libg722.a > $B/baresip.log 2>&1 \
    || { tail -30 $B/baresip.log; exit 1; }
  grep -E "MODULES_DETECTED" $B/baresip.log || true
  cmake --build $B/baresip -j $JOBS --target baresip >> $B/baresip.log 2>&1 \
    || { grep -E "error" $B/baresip.log | head -20; exit 1; }

  local d1=$(sed -n 's/^C_DEFINES = //p' $B/re/CMakeFiles/re-objs.dir/flags.make | tr ' ' '\n' | grep -E "^-DHAVE_|^-DUSE_" | sort)
  local d2=$(sed -n 's/^C_DEFINES = //p' $B/baresip/CMakeFiles/baresip.dir/flags.make | tr ' ' '\n' | grep -E "^-DHAVE_|^-DUSE_" | sort)
  if [ "$d1" != "$d2" ]; then
    echo "FEHLER: libre und baresip mit unterschiedlichen Definitionen:"; diff <(echo "$d1") <(echo "$d2"); exit 1
  fi

  # C-Schicht der App – mit denselben Definitionen wie libre (sonst stimmt z.B. struct sa nicht überein)
  local redefs=(${=$(sed -n 's/^C_DEFINES = //p' $B/re/CMakeFiles/re-objs.dir/flags.make | tr ' ' '\n' | grep -v '"')})
  if [ -d $TP/cmesip ]; then
    ${=cc} -arch $arch -isysroot $sysroot $minflag -O2 -std=c11 \
      -I$P/include/re -I$P/include -I$SRC/baresip/include ${redefs[@]} \
      -c $TP/cmesip/cmesip.c -o $B/cmesip.o
    ${=cc} -arch $arch -isysroot $sysroot $minflag -O2 -std=c11 \
      -I$P/include/re -I$P/include ${redefs[@]} -c $TP/cmesip/tls_stub.c -o $B/tls_stub.o
  fi

  # alles in eine Bibliothek
  local libs=($P/lib/libre.a $P/lib/libg722.a $P/lib/libopus.a $B/baresip/libbaresip.a)
  libs+=($(find $B/baresip/modules -name '*.a'))
  [ -f $B/cmesip.o ] && libs+=($B/cmesip.o $B/tls_stub.o)
  xcrun libtool -static -o $B/libcmesip.a ${libs[@]} 2>/dev/null
  echo "    $(du -h $B/libcmesip.a | cut -f1) $B/libcmesip.a"
}

for s in ${SLICES[@]}; do build_slice $s; done

# xcframework mit Header + Modul-Map (nur die C-Schicht ist für Swift sichtbar)
HDR=$OUT/headers
rm -rf $HDR && mkdir -p $HDR
if [ -f $TP/cmesip/cmesip.h ]; then
  cp $TP/cmesip/cmesip.h $HDR/
  printf 'module CMESIP {\n  header "cmesip.h"\n  export *\n}\n' > $HDR/module.modulemap
fi
args=()
for s in ${SLICES[@]}; do args+=(-library $OUT/$s/libcmesip.a -headers $HDR); done
rm -rf $OUT/CMESIP.xcframework
xcodebuild -create-xcframework ${args[@]} -output $OUT/CMESIP.xcframework >/dev/null
echo "fertig: $OUT/CMESIP.xcframework (${SLICES[*]})"
