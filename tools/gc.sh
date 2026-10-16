#!/bin/bash
# gc.sh — report (default) or fix (--apply) workspace retention violations.
#   scratch/            entries older than 7 days are deleted
#   images/iso/         keep the newest release (YYYY.MM.DD-rN) and the newest build
#   build/logs/runs/    runs older than 30 days are archived to archive/, then deleted
#   src/*               untracked or ignored files inside recipe dirs are reported only
set -uo pipefail
ROOT=${KASHIRA_ROOT:-/mnt/kashira}
apply=0; [ "${1:-}" = --apply ] && apply=1
n=0

hit() { # hit <reason> <path>: report, and with --apply delete
  n=$((n + 1)); printf '%-10s %s\n' "$1" "$2"
  [ "$apply" = 1 ] && [ "$1" != recipe ] && sudo rm -rf -- "$2"
}

while IFS= read -r p; do hit scratch "$p"; done < <(find "$ROOT/scratch" -mindepth 1 -maxdepth 1 -mtime +7)

release=$(find "$ROOT/images/iso" -mindepth 1 -maxdepth 1 -type d -regextype egrep -regex '.*/[0-9]{4}\.[0-9]{2}\.[0-9]{2}-r[0-9]+' | sort | tail -n 1)
latest=$(find "$ROOT/images/iso" -mindepth 1 -maxdepth 1 -type d ! -path "$release" -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-)
while IFS= read -r p; do
  [ "$p" = "$release" ] || [ "$p" = "$latest" ] || hit iso "$p"
done < <(find "$ROOT/images/iso" -mindepth 1 -maxdepth 1 -type d)

while IFS= read -r p; do
  if [ "$apply" = 1 ]; then
    tar -C "$ROOT/build/logs/runs" -cf - "$(basename "$p")" | zstd -q -T0 -19 -o "$ROOT/archive/run-$(basename "$p").tar.zst" \
      && zstd -q -t "$ROOT/archive/run-$(basename "$p").tar.zst" || { echo "archive failed: $p" >&2; continue; }
  fi
  hit run "$p"
done < <(find "$ROOT/build/logs/runs" -mindepth 1 -maxdepth 1 -type d -mtime +30 2>/dev/null)

for r in "$ROOT"/src/*/; do
  git -C "$r" status --porcelain --ignored -- pkgs bootstrap 2>/dev/null | grep -E '^(\?\?|!!)' | grep -v __pycache__ \
    | while read -r _ p; do echo "recipe     $r$p"; done
done

[ -e /mnt/lfs ] && { n=$((n + 1)); echo "retired    /mnt/lfs still exists"; }
echo "gc: $n violation(s)$([ "$apply" = 1 ] && echo ', fixed except recipe/retired')"
