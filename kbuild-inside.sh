#!/bin/bash
# kbuild container entrypoint. Runs as root; the build itself runs as builder.
# Mounts: /pkg (ro, recipe) /sources (rw, SRCDEST cache) /out (rw, PKGDEST)
#         /repo (ro, local kashira repo) /kbuild (ro, this repo)
#         /ccache /cargo /gomod /npm (rw, shared compiler and package-manager caches)
# Env: KBUILD_STANDIN (1: Arch snapshot stand-ins via the host's kbuild-standin proxy), KBUILD_STANDIN_DB
#      (filtered|full), KBUILD_TIER (0 clang, 1 clang compat, 2 gcc), KBUILD_KNOBS (build.tune knobs, comma list)
set -euo pipefail

KBUILD_JOBS=${KBUILD_JOBS:-8}
[[ "$KBUILD_JOBS" =~ ^[1-9][0-9]*$ ]] && [ "$KBUILD_JOBS" -le 16 ] || { echo "invalid KBUILD_JOBS: $KBUILD_JOBS" >&2; exit 2; }
MAKEPKG_CONF=${KBUILD_MAKEPKG_CONF:-makepkg.conf}
case "$MAKEPKG_CONF" in
  */*|.|..) echo "invalid KBUILD_MAKEPKG_CONF: $MAKEPKG_CONF" >&2; exit 2 ;;
esac
[ -f "/kbuild/conf/$MAKEPKG_CONF" ] || { echo "missing makepkg config: $MAKEPKG_CONF" >&2; exit 2; }

export PACKAGER="kashira build service <repo@kashiraproject.org>"
export LC_ALL=C.UTF-8
# Arch's build chroots (devtools) generate en_US.UTF-8 and C.UTF-8; recipes rely on it (e.g. LC_ALL=en_US.UTF-8).
# The image generates them; older images get them here.
if ! locale -a 2>/dev/null | grep -qx 'en_US.utf8'; then
  printf 'en_US.UTF-8 UTF-8\nC.UTF-8 UTF-8\n' > /etc/locale.gen
  locale-gen >/dev/null
fi
KNOBS=",${KBUILD_KNOBS:-},"
TIER=${KBUILD_TIER:-0}
[[ "$TIER" =~ ^[012]$ ]] || { echo "invalid KBUILD_TIER: $TIER" >&2; exit 2; }
[[ "$KNOBS" != *,gnu,* ]] || TIER=2

# Our repo first; a dependency we have not built (or that failed) resolves to Arch's binary of the same
# version from the frozen snapshot. The proxy's DB never lists controlled or ABI-fragile packages.
PACMAN_CONF=/etc/kbuild-pacman.conf
sig="Required DatabaseNever"
pacman-key --list-keys pierre@archlinux.org &>/dev/null || sig=Never  # pre-keyring image: TLS-only trust
{
  # the proxy answers a cache miss only after fetching the whole file from the archive
  printf '[options]\nArchitecture = x86_64\nParallelDownloads = 8\nDisableDownloadTimeout\nSigLevel = %s\nLocalFileSigLevel = Never\n' "$sig"
  printf '[kashira]\nSigLevel = Never\nServer = file:///repo\n'
  if [ "${KBUILD_STANDIN:-1}" != 0 ]; then
    for r in core extra multilib; do
      printf '[arch-%s]\nServer = http://%s:7878/%s/%s\n' "$r" "${KBUILD_STANDIN_HOST:-kbuild-standin}" "${KBUILD_STANDIN_DB:-filtered}" "$r"
    done
  fi
} > "$PACMAN_CONF"

# merged-/usr: force libdir=lib for all cmake builds (see conf file).
# CMAKE_PROJECT_INCLUDE has no env-var form in cmake, so install a wrapper
# ahead of /usr/bin in PATH that injects it on configure runs.
install -Dm755 /kbuild/conf/cmake-wrapper.sh /usr/local/bin/cmake

# pacman occasionally hangs after a finished transaction (its forked child sits in futex_wait, 2026-10-22,
# glibc wave 0): bound every call and retry once; --needed makes the retry a no-op when the install finished.
pac() {
  timeout -k 10 1200 pacman --config "$PACMAN_CONF" "$@" && return 0
  rm -f /var/lib/pacman/db.lck
  timeout -k 10 1200 pacman --config "$PACMAN_CONF" "$@"
}

for p in ${KBUILD_REMOVE:-}; do pacman -Q "$p" &>/dev/null && pacman -Rdd --noconfirm "$p"; done
# shellcheck disable=SC2015  # deliberate A&&B||C: -Syu (default: installed packages must match the repo, e.g. lib32-glibc with glibc), or refresh only
[ "${KBUILD_SKIP_UPGRADE:-0}" = 1 ] && pac -Sy --noconfirm || pac -Syu --noconfirm --ask=4

# Deps come from the host (parsed via makepkg --printsrcinfo). Install ONLY
# from our repo: a failure here means a missing package in the distro.
# gcc is NOT force-installed any more: gcc-runtime carries the libstdc++
# headers and the GCC CRT objects clang needs for GCC-installation detection,
# so clang++ builds C++ in a base-devel image with no gcc present. Packages
# that genuinely need gcc declare it in makedepends, and that dep now decides.
# Overwrite stays scoped to KBUILD_OVERWRITE for packages whose recipe
# legitimately replaces a file (see note in the repo README).
# --ask=4: answer "remove the installed package?" with yes when a dep conflicts with an image package
# (the image's compat-only zlib-ng vs zlib-ng-compat); --noconfirm alone answers no and fails
pacman_args=(-S --needed --noconfirm --ask=4)
[ -n "${KBUILD_OVERWRITE:-}" ] && pacman_args+=(--overwrite "$KBUILD_OVERWRITE")
if [ -n "${KBUILD_DEPS:-}" ]; then
  # shellcheck disable=SC2086  # KBUILD_DEPS is a space-separated dep list
  assume_args=()
  for p in ${KBUILD_ASSUME:-}; do assume_args+=(--assume-installed "$p"); done
  # shellcheck disable=SC2086
  pac "${pacman_args[@]}" "${assume_args[@]}" $KBUILD_DEPS
fi
if [ -f /repo/.kbuild-arch-mixed ]; then
  pacman -Q > /out/.installed-packages
fi
# provenance: every installed package we did not build (a stand-in); wave P rebuilds whatever these touched
pacman -Qi | awk -F' *: ' -v us="$PACKAGER" '$1 == "Name" { n = $2 } $1 == "Version" { v = $2 }
  $1 == "Packager" && $2 != us { print n, v }' > /out/.standins

# our soname_provides drop-in wins over whatever libmakepkg-dropins version the deps installed
# (copied, not bind-mounted: a mount makes the packaged file conflict on install)
install -Dm644 /kbuild/conf/soname_provides.sh /usr/share/makepkg/autodep/soname_provides.sh
# LLVM-first: base-devel has no gcc, yet many Arch recipes call `gcc`/`g++` by name. As on macOS, those names
# run clang (the driver picks C/C++ mode from the name). A recipe that needs real GCC makedepends gcc, which wins.
# Wrappers, not symlinks: clang hands languages it lacks (Ada, Fortran) back to `gcc` from PATH, which would be
# this shim again (a fork bomb in binutils' configure). Those fail fast instead, like a gcc without that frontend.
if [ ! -e /usr/bin/gcc ] && [ "$TIER" -lt 2 ]; then
  for n in gcc:clang g++:clang++; do
    {
      printf '#!/bin/sh\n'
      printf '[ -z "$KBUILD_GCC_SHIM" ] || { echo "%s (clang shim): language not supported" >&2; exit 1; }\n' "${n%%:*}"
      printf 'for a; do case $a in ada|f77|f95|fortran|go|d|*.adb|*.ads|*.f|*.f90|*.f95) echo "%s (clang shim): no $a frontend" >&2; exit 1 ;; esac; done\n' "${n%%:*}"
      printf 'KBUILD_GCC_SHIM=1 exec /usr/bin/%s "$@"\n' "${n#*:}"
    } > "/usr/local/bin/${n%%:*}"
    chmod 755 "/usr/local/bin/${n%%:*}"
  done
fi

id builder &>/dev/null || useradd -m builder
# KBUILD_KEEP_SRC: build into /work (a mounted rw volume) so failed build
# trees survive for inspection
if [ -n "${KBUILD_KEEP_SRC:-}" ]; then
  BUILDDIR=/work
else
  BUILDDIR=/home/builder/build
fi
mkdir -p "$BUILDDIR"
cp -a /pkg/. "$BUILDDIR/"
chown -R builder:builder "$BUILDDIR"
for c in /ccache /cargo /gomod /npm; do [ ! -d "$c" ] || chown builder:builder "$c"; done

TEMP_CONF=$(mktemp)
trap 'rm -f "$TEMP_CONF"' EXIT
cat "/kbuild/conf/$MAKEPKG_CONF" >"$TEMP_CONF"
{
  printf 'MAKEFLAGS="-j%s"\n' "$KBUILD_JOBS"
  # shared caches: ccache (C/C++), the ThinLTO cache, cargo, go modules, npm
  if command -v ccache >/dev/null && [ -d /ccache ]; then
    printf 'export CCACHE_DIR=/ccache CCACHE_BASEDIR=%s CCACHE_MAXSIZE=150G\n' "$BUILDDIR"
    printf 'export CCACHE_SLOPPINESS=locale,time_macros,include_file_mtime,include_file_ctime\n'
  else
    printf 'BUILDENV=("${BUILDENV[@]/#ccache/!ccache}")\n'
  fi
  [ ! -d /cargo ] || printf 'export CARGO_HOME=/cargo\n'
  [ ! -d /gomod ] || printf 'export GOMODCACHE=/gomod\n'
  [ ! -d /npm ] || printf 'export npm_config_cache=/npm\n'
  # lld-only ThinLTO knobs ride the clang driver hook (gcc recipes link with GNU ld)
  printf 'CCC_OVERRIDE_OPTIONS+=" +-Wl,--thinlto-jobs=%s"\n' "$KBUILD_JOBS"
  [ ! -d /ccache ] || printf 'CCC_OVERRIDE_OPTIONS+=" +-Wl,--thinlto-cache-dir=/ccache/thinlto +-Wl,--thinlto-cache-policy=cache_size_bytes=30g"\n'
  # tier 1: clang with old-C defaults and no -Werror or LTO; tier 2: gcc (in the deps) with GNU ld, as Arch builds
  case $TIER in
    1) printf 'CFLAGS+=" -std=gnu17 -fcommon -Wno-error"\nCXXFLAGS+=" -Wno-error -Wno-register"\n'
       printf 'OPTIONS=("${OPTIONS[@]/#lto/!lto}")\n' ;;
    2) printf 'export CC=gcc CXX=g++ CPP="gcc -E" AR=gcc-ar NM=gcc-nm RANLIB=gcc-ranlib\n'
       printf 'LTOFLAGS="-flto=auto"\nunset LD CCC_OVERRIDE_OPTIONS\n' ;;
  esac
  [[ "$KNOBS" != *,o3,* ]] || printf 'CFLAGS=${CFLAGS/-O2/-O3}\nCXXFLAGS=${CXXFLAGS/-O2/-O3}\n'
  [[ "$KNOBS" != *,nolto,* ]] || printf 'OPTIONS=("${OPTIONS[@]/#lto/!lto}")\n'
  [[ "$KNOBS" != *,nodebug,* ]] || printf 'OPTIONS=("${OPTIONS[@]/#debug/!debug}")\n'
  if [[ "$KNOBS" == *,openssl3,* ]]; then  # build against the openssl-3 compat package
    mkdir -p /opt/openssl3
    for f in openssl libssl libcrypto; do ln -sf "/usr/lib/pkgconfig/${f}-3.pc" "/opt/openssl3/$f.pc"; done
    ln -sf /usr/lib/libssl.so.3 /opt/openssl3/libssl.so
    ln -sf /usr/lib/libcrypto.so.3 /opt/openssl3/libcrypto.so
    printf 'export PKG_CONFIG_PATH=/opt/openssl3${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}\n'
    printf 'CPPFLAGS="-I/usr/include/openssl-3 $CPPFLAGS"\nCFLAGS="-I/usr/include/openssl-3 $CFLAGS"\n'
    printf 'CXXFLAGS="-I/usr/include/openssl-3 $CXXFLAGS"\nLDFLAGS+=" -L/opt/openssl3"\n'
  fi
} >>"$TEMP_CONF"
chown builder:builder "$TEMP_CONF"

# skipinteg: Arch's own sums are stale for this source (re-rolled tarball, unsynced file); build.tune quotes the hash
INTEG=; [[ "$KNOBS" != *,skipinteg,* ]] || INTEG=--skipinteg
su builder -c "cd $BUILDDIR && \
  PKGDEST=/out SRCDEST=/sources BUILDDIR=$BUILDDIR \
  makepkg --config $TEMP_CONF -sf --noconfirm --skippgpcheck --nodeps $INTEG"
