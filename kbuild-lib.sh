# shellcheck shell=bash disable=SC2034
# kbuild-lib.sh: shared defaults and build primitives for kbuild and kbuild-run.
# Source it after setting HERE to the kashira-build checkout.
#
# Host layout (override via env; see kashira kb/05-paths.md):
#   KASHIRA_ROOT   workspace root      (/mnt/kashira)
#   KBUILD_IMAGE   container image     (kashira:base-devel-takeover-20261003)
#   KBUILD_REPO    local repo dir      ($KASHIRA_ROOT/repos/candidate)
#   KBUILD_SOURCES source cache        ($KASHIRA_ROOT/cache/sources)
#   KBUILD_OUT     artifacts root      ($KASHIRA_ROOT/build/out)
#   KBUILD_LOGS    logs root           ($KASHIRA_ROOT/build/logs)
KASHIRA_ROOT=${KASHIRA_ROOT:-/mnt/kashira}
IMAGE=${KBUILD_IMAGE:-kashira:base-devel-v3-20261023}
REPO=${KBUILD_REPO:-$KASHIRA_ROOT/repos/v3}
SOURCES=${KBUILD_SOURCES:-$KASHIRA_ROOT/cache/sources}
KBUILD_CACHE=${KBUILD_CACHE:-$KASHIRA_ROOT/cache}
OUT=${KBUILD_OUT:-$KASHIRA_ROOT/build/out}
LOGS=${KBUILD_LOGS:-$KASHIRA_ROOT/build/logs}
# capped so a heavy C++ build (abseil-cpp at -j16) OOMs inside its own cgroup, not the 30 GB host
KBUILD_MEMORY=${KBUILD_MEMORY:-22g}
KBUILD_MEMORY_SWAP=${KBUILD_MEMORY_SWAP:-30g}
KBUILD_CPUS=${KBUILD_CPUS:-16}
KBUILD_JOBS=${KBUILD_JOBS:-16}

build_repo_guard() {
  local canonical resolved state
  canonical=$(realpath -e "$KASHIRA_ROOT/repos/canonical") || return 1
  resolved=$(realpath -e "$REPO") || return 1
  state=$("$HERE/kbuild-arch-bootstrap" state "$resolved") || return 1
  KBUILD_MIXED_REPO=0
  if [ "$state" = mixed ]; then
    [ "$resolved" != "$canonical" ] || { echo "mixed canonical repository forbidden" >&2; return 1; }
    KBUILD_MIXED_REPO=1
    mixed_path_ok "$OUT" "build output" || return 1
  fi
}

# Mixed (Arch-bootstrap) output and logs stay in build/ or scratch/, never in a git tree.
mixed_path_ok() {
  case "$(realpath -m "$1")/" in
    "$(realpath -m "$KASHIRA_ROOT/build")/"*|"$(realpath -m "$KASHIRA_ROOT/scratch")/"*) ;;
    *) echo "mixed $2 must stay under $KASHIRA_ROOT/build or $KASHIRA_ROOT/scratch" >&2; return 1 ;;
  esac
}

mixed_repo() {
  [ "${KBUILD_MIXED_REPO:-0}" = 1 ]
}

validate_build_settings() {
  awk -v c="$KBUILD_CPUS" 'BEGIN { exit !(c ~ /^[0-9]+(\.[0-9]+)?$/ && c > 0 && c <= 16) }' || { echo "invalid KBUILD_CPUS: $KBUILD_CPUS" >&2; return 1; }
  [[ "$KBUILD_JOBS" =~ ^[1-9][0-9]*$ ]] && [ "$KBUILD_JOBS" -le 16 ] || { echo "invalid KBUILD_JOBS: $KBUILD_JOBS" >&2; return 1; }
  if [ "$KBUILD_MEMORY" != unlimited ]; then
  [[ "$KBUILD_MEMORY" =~ ^[1-9][0-9]*[mMgG]$ ]] || { echo "invalid KBUILD_MEMORY: $KBUILD_MEMORY" >&2; return 1; }
  [[ "$KBUILD_MEMORY_SWAP" =~ ^[1-9][0-9]*[mMgG]$ ]] || { echo "invalid KBUILD_MEMORY_SWAP: $KBUILD_MEMORY_SWAP" >&2; return 1; }
  awk -v m="$KBUILD_MEMORY" -v s="$KBUILD_MEMORY_SWAP" 'function bytes(v, n, u) { n=v; sub(/[mMgG]$/, "", n); u=substr(v, length(v), 1); return n * (u ~ /[gG]/ ? 1024 : 1) } BEGIN { exit !(bytes(s) >= bytes(m)) }' || { echo "KBUILD_MEMORY_SWAP must be at least KBUILD_MEMORY" >&2; return 1; }
  fi
}

bootstrap_demoted() {
  local owner expr
  while IFS=$'\t' read -r owner expr; do
    [ "$owner" = "$1" ] && [ "$expr" = "$2" ] && return 0
  done <<<"${KBUILD_BOOTSTRAP_DEMOTED:-}"
  return 1
}

pkg_deps() {
  local info entries line field expr name base
  local -A own=() provided=() seen=()
  local scratch
  scratch=$(mktemp -d) || return 1
  info=$(cd "$1" && BUILDDIR="$scratch" PKGDEST="$scratch" SRCDEST="$scratch" SRCPKGDEST="$scratch" LOGDEST="$scratch" makepkg --printsrcinfo)
  local rc=$?
  rm -rf -- "$scratch"
  [ "$rc" -eq 0 ] || return "$rc"
  entries=$(bsdtar -tf "$REPO/kashira.db.tar.gz") || return 1
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([a-z_0-9-]+)[[:space:]]+=[[:space:]]+(.*)$ ]] || continue
    field=${BASH_REMATCH[1]} expr=${BASH_REMATCH[2]}
    [ -n "$expr" ] || continue
    case "$field" in
      pkgbase) base=$expr ;;
      pkgname) own[$expr]=1 ;;
      provides|provides_x86_64)
        name=${expr%%[\<\>\=]*}; provided[$name]=1 ;;
    esac
  done <<<"$info"
  local selfhost=0
  [[ " ${KBUILD_SELF_DEPS-rust git ghc java-openjdk java21-openjdk maven python-installer python-build python-flit-core python-wheel python-setuptools python-pyproject-hooks python-packaging} " != *" $base "* ]] || selfhost=1
  while IFS= read -r line; do
    # checkdepends too, as makechrootpkg installs them: configure steps look for test tools even with !check
    # (re2: find_package(benchmark); pyalpm: "Program 'pytest' not found")
    [[ "$line" =~ ^[[:space:]]*(depends|makedepends|checkdepends|depends_x86_64|makedepends_x86_64|checkdepends_x86_64)[[:space:]]+=[[:space:]]+(.*)$ ]] || continue
    field=${BASH_REMATCH[1]}
    expr=${BASH_REMATCH[2]}
    [ -n "$expr" ] || continue
    # demoted: runtime bootstrap-policy edges, and stage-1 cuts (depends or makedepends) inside a build cycle
    if bootstrap_demoted "$base" "$expr"; then
      continue
    fi
    name=${expr%%[\<\>\=]*}
    name=${name%:any}; name=${name%:x86_64}
    # a self-hosting base keeps deps it provides itself (java-environment=21 for jdk21): the seed or the previous
    # release in the repo satisfies them
    [ -z "${provided[$name]+set}" ] || [ "$selfhost" = 1 ] || continue
    if [ -n "${own[$name]+set}" ]; then
      # Never ask pacman to install the pkgbase we are building: that would pull
      # the previous release of this very package (stale metadata, and it can
      # drag in deps we are about to replace). Sibling splits are different —
      # gcc-runtime legitimately provides the libstdc++ headers and CRT objects
      # clang++ needs to build the compiler, so keep those when the repo has
      # them, unversioned so the version we are producing cannot leak in.
      # Exception: self-hosted compilers (KBUILD_SELF_DEPS, default: rust git ghc java maven, bootstrapped from Arch stand-ins; git builds from a git+https source) need their
      # own previous release installed as the stage0 toolchain.
      [ "$name" = "$base" ] && [ "$selfhost" = 0 ] && continue
      if awk -v n="$name" 'index($0, n "-") == 1 && /\/desc$/ { found=1; exit } END { exit !found }' <<<"$entries"; then
        expr=$name
      elif [ "$selfhost" = 1 ]; then
        expr=$name                      # not built yet: the Arch stand-in provides it
      else
        continue
      fi
    fi
    [ -z "${seen[$expr]+set}" ] || continue
    seen[$expr]=1
    printf '%s ' "$expr"
  done <<<"$info"
}

expected_outputs() {
  local info line key value pkgbase='' pkgver='' pkgrel='' epoch='' pkgname='' version
  local base_arch='' current_arch='' output_arch
  local -A packages=() architectures=()
  info=$(cd "$1" && makepkg --printsrcinfo) || return 1
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([a-z_0-9]+)[[:space:]]+=[[:space:]]+(.*)$ ]] || continue
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    case "$key" in
      pkgbase) pkgbase=$value ;;
      pkgver) pkgver=$value ;;
      pkgrel) pkgrel=$value ;;
      epoch) epoch=$value ;;
      pkgname)
        pkgname=$value
        [ -z "${packages[$pkgname]+set}" ] || { echo "duplicate SRCINFO pkgname: $pkgname" >&2; return 1; }
        packages[$pkgname]=1
        ;;
      arch)
        case "$pkgname" in
          '') base_arch+=" $value" ;;
          *) case "$value" in x86_64|any) architectures[$pkgname]+=" $value" ;; esac ;;
        esac
        ;;
    esac
  done <<<"$info"
  [[ "$pkgbase" && "$pkgver" && "$pkgrel" && ${#packages[@]} -gt 0 ]] || { echo "incomplete SRCINFO output metadata" >&2; return 1; }
  version=${epoch:+$epoch:}$pkgver-$pkgrel
  for pkgname in "${!packages[@]}"; do
    current_arch=${architectures[$pkgname]:-$base_arch}
    output_arch=
    for value in $current_arch; do
      case "$value" in x86_64) output_arch=x86_64 ;; any) [ -n "$output_arch" ] || output_arch=any ;; esac
    done
    [ -n "$output_arch" ] || { echo "no supported architecture for $pkgname" >&2; return 1; }
    printf '%s\t%s\t%s\t%s\n' "$pkgname" "$pkgbase" "$version" "$output_arch"
  done | sort
}

artifact_list() {
  local dir=$1 pkgdir=$2 pkgbase=$3 f info name base version arch expected key
  local expected_file actual_file
  local -A expected=() actual=()
  expected_file=$(mktemp) || return 1
  actual_file=$(mktemp) || { rm -f "$expected_file"; return 1; }
  if ! expected_outputs "$pkgdir" >"$expected_file"; then rm -f "$expected_file" "$actual_file"; return 1; fi
  [ "$(awk -F '\t' 'NR == 1 { print $2 }' "$expected_file")" = "$pkgbase" ] || { rm -f "$expected_file" "$actual_file"; echo "pkgbase mismatch for $pkgdir" >&2; return 1; }
  while IFS=$'\t' read -r name base version arch; do expected["$name|$base|$version|$arch"]=1; done <"$expected_file"
  shopt -s nullglob
  local -a files=("$dir"/*.pkg.tar.zst)
  shopt -u nullglob
  [ ${#files[@]} -gt 0 ] || { rm -f "$expected_file" "$actual_file"; echo "no package archives in $dir" >&2; return 1; }
  for f in "${files[@]}"; do
    [ -s "$f" ] || { rm -f "$expected_file" "$actual_file"; return 1; }
    info=$(bsdtar -xOf "$f" .PKGINFO 2>/dev/null) || { rm -f "$expected_file" "$actual_file"; return 1; }
    name=$(awk -F ' = ' '$1 == "pkgname" { print $2; exit }' <<<"$info")
    base=$(awk -F ' = ' '$1 == "pkgbase" { print $2; exit }' <<<"$info")
    version=$(awk -F ' = ' '$1 == "pkgver" { print $2; exit }' <<<"$info")
    arch=$(awk -F ' = ' '$1 == "arch" { print $2; exit }' <<<"$info")
    key="$name|$base|$version|$arch"
    [ "${f##*/}" = "$name-$version-$arch.pkg.tar.zst" ] || { rm -f "$expected_file" "$actual_file"; echo "unexpected archive filename: $f" >&2; return 1; }
    # makepkg's debug split (OPTIONS debug): optional, one per pkgbase, published to x86_64-v3-debug
    if [ "$name" = "$pkgbase-debug" ] && [ "$base" = "$pkgbase" ]; then
      printf '%s\n' "$f" >>"$actual_file"
      continue
    fi
    [ -n "${expected[$key]+set}" ] || { rm -f "$expected_file" "$actual_file"; echo "unexpected or stale archive: $f" >&2; return 1; }
    [ -z "${actual[$key]+set}" ] || { rm -f "$expected_file" "$actual_file"; echo "duplicate output archive: $key" >&2; return 1; }
    actual[$key]=1
    printf '%s\n' "$f" >>"$actual_file"
  done
  [ ${#actual[@]} -eq ${#expected[@]} ] || { rm -f "$expected_file" "$actual_file"; echo "incomplete split outputs for $pkgbase" >&2; return 1; }
  while IFS= read -r expected; do
    [ -n "${actual[$expected]+set}" ] || { rm -f "$expected_file" "$actual_file"; echo "missing output: $expected" >&2; return 1; }
  done < <(awk -F '\t' '{ print $1 "|" $2 "|" $3 "|" $4 }' "$expected_file")
  sort -o "$actual_file" "$actual_file"
  cat "$actual_file"
  rm -f "$expected_file" "$actual_file"
}

pkgbase_for() {
  (cd "$1" && makepkg --printsrcinfo) | awk -F ' = ' '$1 == "pkgbase" { print $2; exit }'
}

snapshot_additive() {
  python3 - "$1" "$2" <<'PY'
import re
import sys

def read_snapshot(path):
    with open(path, encoding='utf-8') as stream:
        text = stream.read()
    if text and not text.endswith('\n'):
        raise ValueError(f'{path}: missing final newline')
    rows = {}
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split('\t')
        if (len(fields) != 3 or not all(fields) or
                not re.fullmatch(r'[a-z0-9@._+-]+', fields[0]) or
                fields[2] not in ('native', 'arch')):
            raise ValueError(f'{path}:{number}: malformed snapshot row')
        name, version, origin = fields
        if name in rows:
            raise ValueError(f'{path}:{number}: duplicate package name {name}')
        rows[name] = (version, origin)
    return rows

try:
    before = read_snapshot(sys.argv[1])
    after = read_snapshot(sys.argv[2])
    # arch-origin rows are immutable; native rows may be superseded by a sibling build of the
    # same level (new version of a native package) but never removed.
    for name, identity in before.items():
        if identity[1] == 'arch' and after.get(name) != identity:
            raise ValueError(f'{name}: removed or changed in repository snapshot')
        if name not in after:
            raise ValueError(f'{name}: removed from repository snapshot')
except (OSError, UnicodeError, ValueError) as error:
    print(error, file=sys.stderr)
    sys.exit(1)
PY
}

repo_add() {
  local outdir=$1 pkgdir=$2 pkgbase=$3 pkg destination
  local -a inrepo=()
  local validated
  build_repo_guard || return 1
  validated=$(artifact_list "$outdir" "$pkgdir" "$pkgbase") || return 1
  (
    flock 9 || exit 1
    for pkg in $validated; do
      destination=$REPO/$(basename "$pkg")
      cp -f -- "$pkg" "$destination" || exit 1
      cmp -s -- "$pkg" "$destination" || exit 1
      inrepo+=("$destination")
    done
    repo-add "$REPO/kashira.db.tar.gz" "${inrepo[@]}" || exit 1
    if mixed_repo; then
      for pkg in "${inrepo[@]}"; do
        "$HERE/kbuild-arch-bootstrap" record "$REPO" "$pkg" || exit 1
        "$HERE/kbuild-arch-bootstrap" retire "$REPO" "$pkg" 2>/dev/null || true
      done
    fi
  ) 9>"$REPO/.kbuild.lock"
}

# tune_knobs <pkgbase>: the package's build.tune knobs (comma list), empty if none.
tune_knobs() {
  awk -F'\t' -v b="$1" '$1 == b { print $2; exit }' "${KBUILD_PKGS:-$HERE/../kashira-pkgs}/build.tune" 2>/dev/null
}

# build_one <pkgbase> <pkgdir> <outdir>: build one pkgbase in a fresh container.
# Callers validate artifacts, record mixed-repo packages and repo-add.
build_one() {
  local pkgbase=$1 pkgdir=$2 outdir=$3 deps
  validate_build_settings && build_repo_guard || return 1
  [ -f "$pkgdir/PKGBUILD" ] || { echo "SKIP $pkgdir (no PKGBUILD)"; return 1; }
  deps=$(pkg_deps "$pkgdir") || return 1
  mkdir -p "$outdir" || return 1
  [ -z "${KBUILD_KEEP_SRC:-}" ] || mkdir -p "$outdir/src" || return 1
  if mixed_repo; then
    "$HERE/kbuild-arch-bootstrap" snapshot "$REPO" > "$outdir/.origin-snapshot" || return 1
  fi
  # bootstrap.assume: provides pacman may treat as installed while resolving this package's deps
  # (an already-published dependency carries a stale provide, e.g. rust needing libLLVM.so=23-64).
  local assume=""
  [ ! -f "$pkgdir/bootstrap.assume" ] || assume=$(sed 's/#.*//' "$pkgdir/bootstrap.assume" | tr -s ' \t\n' ' ')
  # bootstrap.remove: installed packages dropped (-Rdd) before deps install, for a stale pin of the package
  # being replaced (e.g. systemd-sysvcompat=261.2-5 blocks installing systemd 262 while building its successor).
  local drop=""
  [ ! -f "$pkgdir/bootstrap.remove" ] || drop=$(sed 's/#.*//' "$pkgdir/bootstrap.remove" | tr -s ' \t\n' ' ')
  # build.tune (kashira-pkgs) holds pool resource hints and the stand-in DB choice only; toolchain and flags
  # come from makepkg.conf and the PKGBUILD
  local knobs=${KBUILD_KNOBS-$(tune_knobs "$pkgbase")}
  local standin_db=filtered
  [[ ",$knobs," != *,standin=full,* ]] || standin_db=full
  # Arch stand-ins link libssl.so.3 (ours is openssl 4): ruby-rdoc: "libssl.so.3: cannot open shared object file"
  [ "${KBUILD_STANDIN:-1}" = 0 ] || [ "$pkgbase" = openssl-3 ] || deps+="openssl-3 "
  echo "== build $pkgbase (deps: ${deps:-none}${assume:+; assume: $assume}${drop:+; remove: $drop}${knobs:+; tune: $knobs})"
  # KBUILD_MEMORY=unlimited: no cgroup memory cap (all RAM and swap).
  local mem=()
  [ "$KBUILD_MEMORY" = unlimited ] || mem=(--memory "$KBUILD_MEMORY" --memory-swap "$KBUILD_MEMORY_SWAP")
  # the proxy by address: a container DNS lookup of kbuild-standin can leave pacman's download child hung in
  # curl_multi_cleanup (threaded resolver) after a finished transaction
  local standin_host
  standin_host=$(sudo docker inspect -f '{{(index .NetworkSettings.Networks "kbuild").IPAddress}}' kbuild-standin 2>/dev/null) || standin_host=kbuild-standin
  local caches=() c
  for c in ccache cargo gomod npm; do
    mkdir -p "$KBUILD_CACHE/$c" && caches+=(-v "$KBUILD_CACHE/$c:/$c")
  done
  sudo docker run --rm \
    --name "kb-${RUN_ID:-manual}-${pkgbase//[^A-Za-z0-9_.-]/_}" \
    --network kbuild \
    "${caches[@]}" \
    "${mem[@]}" \
    --cpus "$KBUILD_CPUS" \
    ${KBUILD_CPUSET:+--cpuset-cpus "$KBUILD_CPUSET"} \
    -e KBUILD_DEPS="$deps" \
    -e KBUILD_STANDIN="${KBUILD_STANDIN:-1}" \
    -e KBUILD_STANDIN_DB="$standin_db" \
    -e KBUILD_STANDIN_HOST="${standin_host:-kbuild-standin}" \
    -e KBUILD_ASSUME="$assume" \
    -e KBUILD_REMOVE="$drop" \
    -e KBUILD_BOOTSTRAP_EDGES="${KBUILD_BOOTSTRAP_EDGES:-}" \
    -e KBUILD_OVERWRITE="${KBUILD_OVERWRITE:-}" \
    -e KBUILD_SKIP_UPGRADE="${KBUILD_SKIP_UPGRADE:-0}" \
    -e KBUILD_KEEP_SRC="${KBUILD_KEEP_SRC:-}" \
    -e KBUILD_MAKEPKG_CONF="${KBUILD_MAKEPKG_CONF:-}" \
    -e KBUILD_JOBS="$KBUILD_JOBS" \
    -v "$pkgdir":/pkg:ro \
    -v "$SOURCES":/sources \
    -v "$outdir":/out \
    -v "$REPO":/repo:ro \
    -v "$HERE":/kbuild:ro \
    ${KBUILD_KEEP_SRC:+-v "$outdir/src":/work} \
    "$IMAGE" /kbuild/kbuild-inside.sh || return 1
  if mixed_repo; then
    [ -s "$outdir/.installed-packages" ] || return 1
    "$HERE/kbuild-arch-bootstrap" snapshot "$REPO" > "$outdir/.origin-snapshot.after" || return 1
    snapshot_additive "$outdir/.origin-snapshot" "$outdir/.origin-snapshot.after" || return 1
  fi
}
