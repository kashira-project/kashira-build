#!/usr/bin/env python3
"""Boot kashira-headless.raw (SB keys already enrolled), 9p-mount the local
kashira repo into the guest, verify pacman works against it."""
import os, re, socket, subprocess, time

IMG = "/mnt/lfs/src/kashira-build/image/headless/kashira-headless.raw"
OVMF_CODE = "/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd"
VARS = "/tmp/kashira-secboot-vars.fd"
SOCK = "/tmp/kashira-serial3.sock"
LOG = "/tmp/kashira-qemu3.log"
REPO = "/mnt/lfs/var/cache/pacman/kashira"

CMDS = [
    "bootctl status | grep -i 'secure boot'",
    "mkdir -p /var/cache/pacman/kashira && mount -t 9p -o trans=virtio,version=9p2000.L,ro krepo /var/cache/pacman/kashira && echo MOUNT-OK",
    "tail -6 /etc/pacman.conf",
    "pacman -Sy 2>&1 | tail -3",
    "pacman -Q linux systemd | tr '\\n' ' '; echo",
    "pacman -Si bash | head -4",
    "systemctl --failed --no-legend",
    "systemctl is-system-running",
]

def main():
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    qemu = subprocess.Popen([
        "qemu-system-x86_64",
        "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", "host",
        "-m", "4096", "-smp", "2",
        "-global", "driver=cfi.pflash01,property=secure,value=on",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={VARS}",
        "-drive", f"file={IMG},format=raw,if=none,id=disk0,snapshot=on",
        "-device", "virtio-blk-pci,drive=disk0",
        "-device", "virtio-rng-pci",
        "-virtfs", f"local,path={REPO},mount_tag=krepo,readonly=on,security_model=none",
        "-display", "none", "-monitor", "none",
        "-serial", f"unix:{SOCK},server=on,wait=off",
    ])
    try:
        s = None
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK)
                break
            except OSError:
                time.sleep(0.2)
        s.settimeout(1.0)
        buf = b""
        sent = False
        with open(LOG, "wb") as log:
            while time.time() < deadline:
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    log.write(chunk)
                except socket.timeout:
                    pass
                clean = re.sub(rb"\x1b\][^\x1b]*\x1b\\\\", b"", buf)
                if not sent and re.search(rb"bash-5\.3#\s*$", clean):
                    sent = True
                    for i, c in enumerate(CMDS):
                        s.sendall(f"echo __R{i}__; {c}\n".encode())
                    s.sendall(b"echo __DONE__\n")
                if sent and b"__DONE__" in buf and buf.rstrip().endswith(b"#"):
                    break
                time.sleep(0.05)
        text = buf.decode("utf-8", "replace")
        text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
        text = re.sub(r"\x1b\][^\x1b\x07]*(\x1b\\\\|\x07)", "", text)
        for i, c in enumerate(CMDS):
            m = re.search(rf"__R{i}__\r?\n(.*?)(?=__R{i+1}__|echo __DONE__)", text, re.S)
            print(f"$ {c}\n{m.group(1).strip()[:700] if m else '<no output>'}\n")
        if sent:
            s.sendall(b"poweroff\n")
            time.sleep(3)
        print(f"shell_reached={sent}")
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            qemu.kill()

if __name__ == "__main__":
    main()
