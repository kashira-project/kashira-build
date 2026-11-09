#!/bin/bash
# Seed the local build repo with R2's databases only (no package files). Builds repo-add fresh packages on top;
# containers fetch any package file that is not on disk from R2 (kbuild-inside.sh, second Server line).
# Usage: tools/repo-seed.sh [repo dir]   (default $KBUILD_REPO or /mnt/kashira/repos/v3). Refuses to overwrite a db.
set -euo pipefail
repo=${1:-${KBUILD_REPO:-${KASHIRA_ROOT:-/mnt/kashira}/repos/v3}}
r2=https://repo.kashiraproject.org
[ ! -e "$repo/kashira.db.tar.gz" ] || { echo "error: $repo already has a db; move it aside first" >&2; exit 1; }
mkdir -p "$repo"
# local name <- R2 prefix/name, as kbuild-publish names them
for pair in kashira:x86_64-v3/kashira-v3 kashira-multilib:x86_64-v3-multilib/kashira-v3-multilib; do
  local_name=${pair%%:*} remote=${pair#*:}
  for kind in db files; do
    curl -fsS -o "$repo/$local_name.$kind.tar.gz" "$r2/$remote.$kind.tar.gz"
    ln -sf "$local_name.$kind.tar.gz" "$repo/$local_name.$kind"
  done
done
echo "seeded $repo: $(bsdtar -tf "$repo/kashira.db.tar.gz" | grep -c '/desc$') + $(bsdtar -tf "$repo/kashira-multilib.db.tar.gz" | grep -c '/desc$') multilib entries"
