#!/bin/bash
# Build one pkg and repo-add on success. Usage: C-buildone.sh <pkgbase>
set -u
p=$1
cd /mnt/lfs/src/kashira-build
if ./kbuild build /mnt/lfs/src/kashira-arch/pkgs/$p > logs/$p.log 2>&1; then
  shopt -s nullglob
  arts=(out/$p/*.pkg.tar.zst)
  if [ ${#arts[@]} -gt 0 ]; then
    cp "${arts[@]}" /mnt/lfs/var/cache/pacman/kashira/
    (cd /mnt/lfs/var/cache/pacman/kashira && flock /tmp/repo.lock -c "repo-add kashira.db.tar.gz /mnt/lfs/src/kashira-build/out/$p/*.pkg.tar.zst") >> logs/$p.log 2>&1 \
      && touch logs/$p.done && echo "OK $p" || { touch logs/$p.failed; echo "REPOADD-FAIL $p"; }
  else
    touch logs/$p.failed; echo "NOARTIFACTS $p"
  fi
else
  touch logs/$p.failed; echo "FAIL $p"
fi
