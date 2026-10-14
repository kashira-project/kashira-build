#!/bin/sh
# Audit the real packages required by mkosi's Arch initrd profile.
set -eu

repo=${KASHIRA_REPO:-/mnt/lfs/var/cache/pacman/kashira}
python3 - "$repo" <<'PY'
import hashlib
import pathlib
import subprocess
import sys
import tempfile

repo = pathlib.Path(sys.argv[1])
required = ("btrfs-progs", "xfsprogs", "dosfstools", "libfido2")
db = repo / "kashira.db"
if not repo.is_dir() or not db.is_file():
    raise SystemExit(f"missing repository or database: {repo}")

with tempfile.TemporaryDirectory() as directory:
    subprocess.run(["bsdtar", "-xf", str(db), "-C", directory], check=True)
    records = {}
    for desc in pathlib.Path(directory).glob("*/desc"):
        fields = {}
        lines = desc.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("%") and line.endswith("%") and index + 1 < len(lines):
                fields[line] = lines[index + 1]
        if fields.get("%NAME%") in required:
            records[fields["%NAME%"]] = fields

missing = 0
for name in required:
    record = records.get(name)
    filename = record.get("%FILENAME%") if record else None
    archive = repo / filename if filename else None
    valid = bool(record and archive and archive.is_file() and archive.stat().st_size > 0)
    if valid:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        valid = digest == record.get("%SHA256SUM%")
    if valid:
        listing = subprocess.run(["bsdtar", "-tf", str(archive)], check=True,
                                 capture_output=True, text=True).stdout.splitlines()
        pkginfo = next((entry for entry in listing if entry == ".PKGINFO" or entry.endswith("/.PKGINFO")), None)
        if not pkginfo:
            valid = False
        else:
            metadata = subprocess.run(["bsdtar", "-xOf", str(archive), pkginfo],
                                      check=True, capture_output=True, text=True).stdout
            valid = any(line == f"pkgname = {name}" for line in metadata.splitlines())
    if valid:
        print(f"present: {name} ({filename})")
    else:
        print(f"missing or invalid: {name}", file=sys.stderr)
        missing = 1
if missing:
    raise SystemExit("headless: build blocked until required real packages are present, valid, and indexed")
PY
