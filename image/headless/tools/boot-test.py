#!/usr/bin/env python3
"""Authoritative serial acceptance test for a kashira headless image."""
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_IMAGE = Path(__file__).resolve().parent.parent / "kashira-headless.raw"
DEFAULT_FIRMWARE = Path("/usr/share/edk2/x64/OVMF_CODE.4m.fd")
SECURE_FIRMWARE = Path("/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd")
VARS_TEMPLATE = Path("/usr/share/edk2/x64/OVMF_VARS.4m.fd")
ROOT_GUID = "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709"


class Failure(Exception):
    pass


def run(command, *, check=True, capture=True):
    result = subprocess.run(command, check=False, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise Failure(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def load_config(path, secure_boot):
    config = {"packages": {"linux": None, "systemd": None, "pacman": None},
              "required_units": ["systemd-networkd.service",
                                  "systemd-resolved.service"]}
    if path:
        with open(path, encoding="utf-8") as stream:
            supplied = json.load(stream)
        if not isinstance(supplied, dict):
            raise Failure("--config must contain a JSON object")
        for key in ("packages", "required_units"):
            if key in supplied:
                config[key] = supplied[key]
        expected = supplied.get("secure_boot", secure_boot)
        if expected != secure_boot:
            raise Failure(f"config secure_boot={expected!r} disagrees with --secure-boot={secure_boot!r}")
    if not isinstance(config["packages"], dict) or not isinstance(config["required_units"], list):
        raise Failure("config packages must be an object and required_units must be a list")
    for name, version in config["packages"].items():
        if not re.fullmatch(r"[A-Za-z0-9@._+:-]+", name):
            raise Failure(f"unsafe package name in config: {name!r}")
        if version is not None and not re.fullmatch(r"[A-Za-z0-9.+_:~-]+", str(version)):
            raise Failure(f"unsafe package version in config: {version!r}")
    for unit in config["required_units"]:
        if not re.fullmatch(r"[A-Za-z0-9@._%:+-]+", unit):
            raise Failure(f"unsafe unit name in config: {unit!r}")
    return config


def root_offset(image):
    data = json.loads(run(["sfdisk", "--json", str(image)]).stdout)
    for partition in data["partitiontable"]["partitions"]:
        if partition.get("type", "").upper() == ROOT_GUID or partition.get("name") == "root-x86-64":
            return int(partition["start"]) * int(data["partitiontable"]["sectorsize"])
    raise Failure("image has no root-x86-64 GPT partition")


def make_debug_overlay(image, workdir):
    overlay = workdir / "debug-overlay.raw"
    run(["cp", "--reflink=auto", "--sparse=always", str(image), str(overlay)], capture=False)
    mountpoint = workdir / "root"
    mountpoint.mkdir()
    mounted = False
    try:
        run(["sudo", "-n", "mount", "-o", f"loop,offset={root_offset(overlay)}",
             str(overlay), str(mountpoint)], capture=False)
        mounted = True
        dropin = workdir / "debug-overlay.conf"
        dropin.write_text(
            "[Service]\nExecStart=\nExecStart=-/usr/bin/agetty --autologin root --noclear %I $TERM\n",
            encoding="ascii")
        target = "/etc/systemd/system/serial-getty@ttyS0.service.d/debug-overlay.conf"
        run(["sudo", "-n", "mkdir", "-p", f"{mountpoint}/etc/systemd/system/serial-getty@ttyS0.service.d"], capture=False)
        run(["sudo", "-n", "cp", str(dropin), f"{mountpoint}{target}"], capture=False)
        run(["sync"], capture=False)
    finally:
        if mounted:
            run(["sudo", "-n", "umount", str(mountpoint)], capture=False)
    return overlay


def clean_serial(data):
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def probes(config, secure_boot, repo, repo_sync):
    state = "enabled" if secure_boot == "on" else "disabled"
    commands = [
        ("boot-policy", f"bootctl status --no-pager 2>&1 | grep -Fq 'Secure Boot: {state}'"),
        ("root-locked", "set -o pipefail; passwd -S root | awk '$2 == \"L\" {found=1} END {exit !found}'"),
        ("no-autologin", "test ! -e /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf"),
        ("runtime-pacman", r"grep -Fxq '[kashira-v3]' /etc/pacman.conf && grep -Fxq 'SigLevel = Required DatabaseRequired' /etc/pacman.conf && grep -Fxq 'Server = https://repo.kashiraproject.org/x86_64-v3' /etc/pacman.conf && ! grep -Eq '^Server = file://|^\[kashira\]$|^\[kashira-mkosi-stubs\]$' /etc/pacman.conf"),
        ("no-failed-units", "systemctl --failed --no-legend >/tmp/kashira-failed-units; rc=$?; test \"$rc\" -eq 0 && test ! -s /tmp/kashira-failed-units"),
        ("system-running", "test \"$(systemctl is-system-running)\" = running"),
        ("preset-resolution", "test -s /usr/lib/systemd/system-preset/90-kashira-headless.preset && systemctl is-enabled --quiet systemd-networkd.service && systemctl is-enabled --quiet systemd-resolved.service && systemctl is-enabled --quiet serial-getty@ttyS0.service"),
        ("network-ipv4", "ip -4 addr show scope global | grep -Eq 'inet [0-9]+\\.'"),
        ("network-default-route", "ip -4 route show default | grep -Eq '^default via [0-9]+\\.'"),
        ("network-dns", "resolvectl query repo.kashiraproject.org >/dev/null"),
    ]
    for name, version in config["packages"].items():
        check = f"pacman -Q {shlex.quote(name)} >/dev/null"
        if version is not None:
            check += f" && test \"$(pacman -Q {shlex.quote(name)} | awk '{{print $2}}')\" = {shlex.quote(str(version))}"
        commands.append((f"package:{name}", check))
    for unit in config["required_units"]:
        commands.append((f"unit:{unit}", f"systemctl is-enabled --quiet {shlex.quote(unit)}"))
    if repo:
        commands.append(("repo-readonly", "mkdir -p /run/kashira-repo && mount -t 9p -o trans=virtio,version=9p2000.L,ro krepo /run/kashira-repo && test -r /run/kashira-repo/kashira.db"))
    if repo_sync:
        commands.append(("repo-sync", "pacman -Sy --noconfirm"))
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--secure-boot", choices=("off", "on"), default="off")
    parser.add_argument("--debug-overlay", action="store_true", help="temporary root autologin overlay; disposable VM testing only")
    parser.add_argument("--repo", type=Path, help="read-only local repository to expose as 9p")
    parser.add_argument("--repo-sync", action="store_true", help="also run pacman -Sy in the guest")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    try:
        if args.timeout < 120:
            raise Failure("--timeout must be at least 120 seconds")
        image = args.image.resolve(strict=True)
        if not image.is_file() or image.suffix != ".raw":
            raise Failure("--image must be an existing .raw file")
        firmware = (args.firmware or (SECURE_FIRMWARE if args.secure_boot == "on" else DEFAULT_FIRMWARE)).resolve(strict=True)
        if not firmware.is_file() or "VARS" in firmware.name:
            raise Failure("--firmware must be an OVMF code file, not a vars file")
        if not VARS_TEMPLATE.is_file():
            raise Failure(f"missing public OVMF vars template: {VARS_TEMPLATE}")
        if args.repo_sync and not args.repo:
            raise Failure("--repo-sync requires --repo")
        repo = args.repo.resolve() if args.repo else None
        if repo and (not repo.is_dir() or os.path.commonpath((str(repo), "/mnt/lfs/var/cache/pacman")) != "/mnt/lfs/var/cache/pacman"):
            raise Failure("--repo must be a directory below /mnt/lfs/var/cache/pacman")
        config = load_config(args.config, args.secure_boot)
        parent = (args.workdir or Path(tempfile.gettempdir())).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kashira-boot-", dir=parent) as temp:
            workdir = Path(temp)
            log_path = parent / f"kashira-boot-{workdir.name}.log"
            source = make_debug_overlay(image, workdir) if args.debug_overlay else image
            vars_file = workdir / "OVMF_VARS.fd"
            shutil.copyfile(VARS_TEMPLATE, vars_file)
            vars_file.chmod(0o600)
            serial = workdir / "serial.sock"
            qemu_log = log_path
            qemu_cmd = ["qemu-system-x86_64", "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", "host", "-m", "4096", "-smp", "2"]
            if args.secure_boot == "on":
                qemu_cmd += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
            qemu_cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={firmware}", "-drive", f"if=pflash,format=raw,file={vars_file}", "-drive", f"file={source},format=raw,if=none,id=disk0,snapshot=on", "-device", "virtio-blk-pci,drive=disk0", "-device", "virtio-rng-pci", "-nic", "user,model=virtio", "-display", "none", "-monitor", "none", "-serial", f"unix:{serial},server=on,wait=off"]
            if repo:
                qemu_cmd += ["-virtfs", f"local,path={repo},mount_tag=krepo,readonly=on,security_model=none"]
            with qemu_log.open("wb") as log:
                qemu = subprocess.Popen(qemu_cmd, stdout=subprocess.DEVNULL, stderr=log)
                sock = None
                buf = b""
                deadline = time.monotonic() + args.timeout
                try:
                    while time.monotonic() < deadline and sock is None:
                        if qemu.poll() is not None:
                            raise Failure(f"qemu exited before serial connection ({qemu.returncode})")
                        try:
                            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            sock.connect(str(serial))
                        except OSError:
                            if sock:
                                sock.close()
                            sock = None
                            time.sleep(0.2)
                    if sock is None:
                        raise Failure("timed out waiting for QEMU serial connection")
                    sock.settimeout(0.2)
                    if not args.debug_overlay:
                        login_seen = False
                        while time.monotonic() < deadline:
                            try:
                                chunk = sock.recv(65536)
                                if not chunk:
                                    raise Failure("serial connection closed before login prompt")
                                buf += chunk
                                log.write(chunk)
                                log.flush()
                            except socket.timeout:
                                pass
                            text = clean_serial(buf)
                            if re.search(r"(?mi)^.*login:\s*$", text):
                                if re.search(r"(?mi)login:\s+\S+\s+\(automatic login\)", text):
                                    raise Failure("default boot unexpectedly auto-logged in")
                                if re.search(r"(?mi)(?:bash|zsh|fish|dash)-[0-9][^\n]*[#\$]", text):
                                    raise Failure("default boot unexpectedly reached a shell")
                                login_seen = True
                                break
                        if not login_seen:
                            raise Failure("timed out waiting for login prompt")
                        print("boot-login: ok")
                        print("mode: login-boot (administrative probes skipped; use --debug-overlay)")
                        sock.sendall(b"poweroff\n")
                    else:
                        ready_sent = False
                        commands = probes(config, args.secure_boot, repo, args.repo_sync)
                        next_probe = 0
                        probe_sent = False
                        done_sent = False
                    while args.debug_overlay and time.monotonic() < deadline:
                        try:
                            chunk = sock.recv(65536)
                            if not chunk:
                                raise Failure("serial connection closed before acceptance completed")
                            buf += chunk
                            log.write(chunk)
                            log.flush()
                        except socket.timeout:
                            pass
                        text = clean_serial(buf)
                        if not ready_sent and ("Startup finished in userspace" in text or time.monotonic() > deadline - args.timeout / 2):
                            sock.sendall(b"printf '\\n__KASHIRA_READY__\\n'\n")
                            ready_sent = True
                        if ready_sent and not probe_sent and next_probe < len(commands) and "__KASHIRA_READY__" in text:
                            command = commands[next_probe][1]
                            sock.sendall(f"printf '__KASHIRA_BEGIN_{next_probe}__\\n'; {command}; rc=$?; printf '__KASHIRA_EXIT_{next_probe}__=%s\\n' \"$rc\"\n".encode())
                            probe_sent = True
                        match = re.search(rf"(?m)(?:^|\n)__KASHIRA_EXIT_{next_probe}__=(\d+)(?:\n|$)", text) if probe_sent else None
                        if match:
                            next_probe += 1
                            probe_sent = False
                            if next_probe == len(commands):
                                sock.sendall(b"printf '__KASHIRA_DONE__\\n'\n")
                                done_sent = True
                        if done_sent and re.search(r"(?m)(?:^|\n)__KASHIRA_DONE__(?:\n|$)", text):
                            break
                    if args.debug_overlay and not done_sent:
                        raise Failure("timed out waiting for authoritative guest probes")
                    if args.debug_overlay:
                        text = clean_serial(buf)
                        results = []
                        for index, (name, command) in enumerate(commands):
                            end = re.search(rf"(?m)(?:^|\n)__KASHIRA_EXIT_{index}__=(\d+)(?:\n|$)", text)
                            if not end:
                                raise Failure(f"missing exit sentinel for {name}")
                            results.append((name, int(end.group(1))))
                        failed = [(name, status) for name, status in results if status]
                        for name, status in results:
                            print(f"{name}: {'ok' if status == 0 else 'FAIL'}")
                        if failed:
                            raise Failure("guest probes failed: " + ", ".join(f"{name}={status}" for name, status in failed))
                        print("mode: administrative-checks (debug overlay only)")
                    if args.debug_overlay:
                        sock.sendall(b"poweroff\n")
                finally:
                    if sock:
                        sock.close()
                    if qemu.poll() is None:
                        qemu.terminate()
                        try:
                            qemu.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            qemu.kill()
                            qemu.wait()
            print(f"acceptance: PASS (secure-boot={args.secure_boot}, log={qemu_log})")
    except (Failure, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"acceptance: FAIL: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
