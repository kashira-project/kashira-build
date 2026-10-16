#!/bin/bash
# Probe which auto-enabled meson features of a pkgbase lack dependencies.
# Runs `makepkg -o`, then repeats only the meson configure step of build(),
# adding -D <subproject>:<dir>=disabled for each "Dependency ... not found"
# raised under subprojects/<subproject>/{ext,sys}/<dir>/meson.build.
# Prints the options it had to disable. Read-only for the repo.
#   tools/meson-feature-probe.sh <pkgbase> [max-iterations]
set -euo pipefail
base=$1 max=${2:-40}
here=$(cd -- "$(dirname -- "$0")/.." && pwd)
root=${KASHIRA_ROOT:-/mnt/kashira}
repo=${KBUILD_REPO:-$root/repos/candidate}
pkgdir=$root/src/kashira-pkgs/${base:0:1}/$base
[[ -f $pkgdir/PKGBUILD ]] || { echo "no recipe: $base" >&2; exit 2; }

docker run --rm -i \
    -v "$repo:/r:ro" -v "$pkgdir:/recipe:ro" -v "$root/cache/sources:/sources" \
    -v "$here/conf/makepkg.conf:/kconf/makepkg.conf:ro" \
    -e MAX="$max" kashira:base-devel-takeover-20261003 bash -s <<'IN'
set -uo pipefail
printf '[options]\nArchitecture=x86_64\nSigLevel=Never\n[kashira]\nServer=file:///r\n' > /etc/pacman.conf
pacman -Sy --noconfirm >/dev/null 2>&1
id builder &>/dev/null || useradd -m builder
cp -r /recipe /home/builder/probe && chown -R builder:builder /home/builder/probe
cd /home/builder/probe
su builder -c 'makepkg --printsrcinfo' > /tmp/srcinfo
own=$(awk '/^pkgname = /{print $3}' /tmp/srcinfo)
deps=$(awk '/^\t(make)?depends = /{print $3}' /tmp/srcinfo | sed 's/[<>=].*//' | sort -u | grep -vxF "$own")
pacman -S --noconfirm --needed $deps >/tmp/deps.log 2>&1 || { echo "deps failed:"; grep -E 'error|not found' /tmp/deps.log | head -n 10; exit 1; }
su builder -c 'SRCDEST=/sources makepkg --config /kconf/makepkg.conf -o --nodeps --skippgpcheck' >/tmp/o.log 2>&1 || { tail -n 5 /tmp/o.log; exit 1; }
cat > /tmp/probe.sh <<'EOF'
set -uo pipefail
source /kconf/makepkg.conf; CARCH=${CARCH:-x86_64}
cd /home/builder/probe; srcdir=$PWD/src; source ./PKGBUILD
EXTRA=()
arch-meson() { command arch-meson "$@" "${EXTRA[@]}"; }
meson() { [[ $1 == compile || $1 == test ]] && return 0; command meson "$@"; }
for ((i=1; i<=MAX; i++)); do
  rm -rf "$srcdir/build"
  ( cd "$srcdir" && build ) >/tmp/cfg.log 2>&1 && { echo "CONFIGURED after $((i-1)) additions"; break; }
  line=$(grep -m1 -E 'meson.build:[0-9]+:[0-9]+: ERROR:' /tmp/cfg.log || true)
  opt=$(sed -nE 's#.*subprojects/([^/]+)/(ext|sys)/([^/]+)/meson.build:.*#\1:\3#p' <<<"$line")
  if [[ -z $opt ]]; then echo "STOP: $line"; break; fi
  echo "-D $opt=disabled   # $(sed -E 's/.*ERROR: //' <<<"$line" | cut -c1-90)"
  EXTRA+=(-D "$opt=disabled")
done
EOF
su builder -c "MAX=$MAX bash /tmp/probe.sh"
IN
