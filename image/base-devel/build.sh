#!/usr/bin/env bash
set -euo pipefail

repo=${KIMG_REPO:-/mnt/kashira/repos/v3}
output=${KIMG_OUT:-/mnt/kashira/scratch/kbimg}
tag=${IMAGE_TAG:-kashira:base-devel-v3-$(date +%Y%m%d)}
[[ -f $repo/kashira.db.tar.gz ]] || { printf 'error: no repo DB at %s\n' "$repo" >&2; exit 1; }

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

(( EUID == 0 )) || die 'run as root (sudo)'

if [[ ${1:-} == --inside ]]; then
    (( $# == 2 )) || die 'invalid internal invocation'
    work=$2
    root=$work/root
    [[ -d $work && ! -e $root ]] || die 'workspace was reused'
    mkdir -m 0755 -- "$root"
    cat > "$work/bootstrap.conf" <<EOF
[options]
Architecture = x86_64
SigLevel = Never

[kashira]
Server = file://$repo
EOF
    # The target root MUST be root-owned: systemd-tmpfiles --root refuses every
    # path when the root itself is foreign-owned. mkdir below runs as root.
    mount --make-rprivate /
    # No private /run here: gpg-agent needs a usable /run for its sockets, and
    # pacstrap -K must generate the master key before kashira-keyring populates
    # trust. Package scripts now target $root explicitly (systemd.install no
    # longer calls `systemctl preset-all`), so host /run is never consulted.
    # ccache and archlinux-keyring: kbuild's shared compiler cache, and signature checks for Arch stand-ins
    # (archlinux-keyring's install script populates the keyring pacstrap -K initialized)
    if ! pacstrap -C "$work/bootstrap.conf" -K -M "$root" base base-devel ccache archlinux-keyring 2>&1 | tee "$work/install.log"; then
        die "pacstrap failed; see $work/install.log"
    fi
    if grep -Eiq '(^|[[:space:]])(error:|failed to|failure:)|command failed to execute correctly|install scriptlet.*(fail|error)|hook.*(fail|error)' "$work/install.log"; then
        die "pacman reported an install-script or hook error; see $work/install.log"
    fi
    printf 'en_US.UTF-8 UTF-8\nC.UTF-8 UTF-8\n' > "$root/etc/locale.gen"
    chroot "$root" locale-gen >/dev/null || die 'locale-gen failed'
    printf '#include <stdio.h>\nint main(void) { puts("kashira-c-smoke"); }\n' |
        chroot "$root" /usr/bin/clang -x c -o /tmp/kashira-c-smoke - || die 'clang C smoke test failed'
    printf '#include <vector>\nint main(void) { std::vector<int> v{1, 2, 3}; return v.size() != 3; }\n' |
        chroot "$root" /usr/bin/clang++ -std=c++17 -x c++ -o /tmp/kashira-cxx-smoke - || die 'clang++ C++ smoke test failed'
    chroot "$root" /tmp/kashira-c-smoke || die 'clang C runtime smoke test failed'
    chroot "$root" /tmp/kashira-cxx-smoke || die 'clang++ C++ runtime smoke test failed'
    [[ ! -e $root/usr/bin/gcc ]] || die 'unexpected GCC compiler in base-devel image'
    rm -f "$root/tmp/kashira-c-smoke" "$root/tmp/kashira-cxx-smoke"
    for path in / /etc /usr /var /var/lib/pacman /root /tmp; do
        [[ $(stat -c '%u:%g' -- "$root$path") == 0:0 ]] || die "wrong ownership: $root$path"
    done
    pacman --root="$root" --dbpath="$root/var/lib/pacman" -Qql > "$work/package-paths"
    while read -r path; do
        [[ $path == */ && -d $root$path ]] || continue
        [[ $(stat -c '%u:%g' -- "$root$path") == 0:0 ]] || die "wrong package-owned directory: $path"
    done < "$work/package-paths"
    [[ -s $root/etc/ssl/certs/ca-certificates.crt ]] || die 'CA bundle is empty or missing'
    # /etc/resolv.conf is a tmpfiles symlink to systemd-resolved's stub (kashira-filesystem); docker mounts its own
    [[ -f $root/etc/pacman.conf ]] || die 'package-shipped pacman.conf is missing'
    [[ -s $root/usr/share/pacman/keyrings/kashira.gpg ]] || die 'kashira-keyring is missing'
    keydir=$root/etc/pacman.d/gnupg
    [[ -d $keydir && ! -L $keydir && ! -L $keydir/private-keys-v1.d ]] || die 'unexpected keyring path'
    rm -rf -- "$keydir/private-keys-v1.d"
    rm -f -- "$keydir/secring.gpg"
    systemd-tmpfiles --root="$root" --create --dry-run -E > "$work/tmpfiles.log" 2>&1 || die "offline tmpfiles check failed; see $work/tmpfiles.log"
    [[ ! -L $root/etc/machine-id ]] || die 'machine-id is a symlink'
    : > "$root/etc/machine-id"
    shopt -s nullglob
    packages=("$root"/var/cache/pacman/pkg/*.pkg.tar.*)
    if (( ${#packages[@]} )); then
        rm -f -- "${packages[@]}"
    fi
    exit 0
fi

(( $# <= 1 )) || die 'usage: sudo build.sh [NEW_WORKDIR] (IMAGE_TAG optionally sets the local tag)'
[[ -d $repo && -f $repo/kashira.db ]] || die "local repository is unavailable: $repo"
for cmd in unshare mount pacstrap systemd-tmpfiles docker tar stat findmnt realpath mktemp tee grep; do
    command -v "$cmd" > /dev/null || die "missing command: $cmd"
done

if (( $# )); then
    [[ $1 == /* && $1 != */ && $1 != *'/.' && $1 != *'/..' ]] || die 'NEW_WORKDIR must be an absolute non-root path'
    parent=$(realpath -e -- "$(dirname -- "$1")") || die 'workspace parent does not exist'
    [[ $parent == /mnt/kashira || $parent == /mnt/kashira/* ]] || die 'workspace must be inside /mnt/kashira'
    work=$parent/$(basename -- "$1")
    [[ ! -e $work && ! -L $work ]] || die "workspace already exists: $work"
    mkdir -m 0700 -- "$work" || die 'cannot create fresh workspace'
else
    mkdir -p -- "$output"
    work=$(mktemp -d "$output/b.XXXXXXXX")
fi
[[ $(stat -c '%u:%g' -- "$work") == 0:0 ]] || die "workspace is not owned by root: $work"
# gpg-agent creates its socket plus auxiliary entries (S.gpg-agent.<role>.<pid>)
# inside the target keyring. Those must all fit in sun_path (108 bytes), so a
# long workspace path makes gpg-agent exit 2 and pacstrap silently loses its
# master key. Measured threshold: ~61 chars for the workspace itself.
socket_dir="$work/root/etc/pacman.d/gnupg"
(( ${#socket_dir} + 12 <= 90 )) || die "workspace path too long for gpg-agent sockets (${#socket_dir} chars): use a shorter path under /mnt/kashira"
printf 'Workspace: %s\n' "$work"
unshare --mount --fork --kill-child --propagation private -- "$0" --inside "$work"
root=$work/root
if findmnt -rn -o TARGET | grep -Fq -- "$root/"; then
    die "mount remains under $root; refusing export"
fi
tar --numeric-owner --one-file-system -C "$root" -cf - . | docker import - "$tag"
printf 'Imported local image: %s\n' "$tag"
