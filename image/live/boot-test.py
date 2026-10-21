#!/usr/bin/env python3
"""Boot a Kashira live ISO in QEMU/OVMF and probe it over the serial console.

  image/live/boot-test.py <iso> <outdir> [--wait-graphical SECONDS]
                          [--secureboot VARS.fd] [--screenshot-after SECONDS] [--media usb]

--secureboot boots the Secure Boot OVMF build (SMM) with the given enrolled
variable store. --screenshot-after only takes a screenshot after SECONDS and
exits (for boots that stop before a login prompt, e.g. MokManager).
--media usb attaches the ISO as a USB stick instead of a CD-ROM, like a flashed
drive (archiso then copies the root image to RAM).

Logs in as root on ttyS0 (live media only: root has no password), runs the
probe commands below (including a suspend, QMP wake and lock check), saves their output
to <outdir>/probes.txt and takes QMP
screendumps (<outdir>/screen-*.ppm). Uses KVM when /dev/kvm is usable, else TCG
with -cpu max (x86-64-v3 needs AVX2). Exit status 0 only if every probe in
REQUIRED passed.
"""
import json, os, socket, subprocess, sys, time

OVMF_CODE = "/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd"
OVMF_VARS = "/usr/share/edk2-ovmf/x64/OVMF_VARS.4m.fd"
OVMF_CODE_SB = "/usr/share/edk2-ovmf/x64/OVMF_CODE.secboot.4m.fd"

PROBES = [
    ("secureboot", "od -An -tu1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c | awk '{print \"SecureBoot=\" $5}'; journalctl -k -b --no-pager | grep -iE 'secure ?boot|lockdown' | head -n 3"),
    ("graphical-target", "systemctl is-active graphical.target"),
    ("gdm", "systemctl is-active gdm.service"),
    ("networkmanager", "systemctl is-active NetworkManager.service"),
    ("iwd", "systemctl is-active iwd.service"),
    ("sessions", "loginctl list-sessions --no-legend"),
    ("session-type", "for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do loginctl show-session $s -p Name -p Type -p Class -p State; done"),
    ("gnome-shell", "pgrep -a gnome-shell | head -n 3"),
    ("user-services", "systemctl --user -M kashira@ is-active pipewire.service wireplumber.service xdg-desktop-portal.service xdg-desktop-portal-gnome.service 2>&1"),
    ("nm-connectivity", "nmcli -t -f STATE,CONNECTIVITY general"),
    ("dns", "resolvectl query kashiraproject.org 2>&1 | head -n 2"),
    ("failed-units", "systemctl --failed --no-legend"),
    ("journal-errors", "journalctl -b -p err --no-pager | tail -n 25"),
    ("gdm-journal", "journalctl -b -u gdm --no-pager | tail -n 20"),
    ("shell-journal", "journalctl -b --no-pager _COMM=gnome-shell | tail -n 40"),
    ("loops", "losetup -l -O NAME,BACK-FILE,RO; findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS | grep -E 'loop|sr0|vfat'"),
    ("loop-units", "systemctl list-units --all --no-legend '*loop*' '*sr0*' '*udisks*' | head -n 20"),
    ("loop-journal", "journalctl -b --no-pager | grep -iE 'loop1|udisks|vfat|FAT-fs|mounted' | head -n 30"),
    ("drm", "ls /dev/dri; cat /sys/class/drm/card*/device/uevent 2>/dev/null | grep DRIVER"),
    # live-session fixes: sudo without a password, an empty (not locked) password, no lock on resume
    ("sudo", "runuser -u kashira -- sudo -n true && echo SUDO_OK"),
    ("live-password", "passwd -S kashira"),
    ("lock-settings", "runuser -u kashira -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u kashira)/bus gsettings get org.gnome.desktop.screensaver lock-enabled"),
    ("suspend", "systemctl suspend; sleep 8; journalctl -k -b --no-pager | grep -q 'PM: suspend exit' && echo RESUMED"),
    ("resume-unlocked", "for s in $(loginctl list-sessions --no-legend | awk '$3==\"kashira\"{print $1}'); do "
     "loginctl show-session $s -p LockedHint -p State; done; runuser -u kashira -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u kashira)/bus gdbus call --session "
     "--dest org.gnome.ScreenSaver --object-path /org/gnome/ScreenSaver --method org.gnome.ScreenSaver.GetActive"),
]
# probe name -> substring that must appear in its output
REQUIRED = {
    "gdm": "active",
    "networkmanager": "active",
    "session-type": "Type=wayland",
    "gnome-shell": "gnome-shell",
    "sudo": "SUDO_OK",
    "live-password": "kashira NP",
    "lock-settings": "false",
    "suspend": "RESUMED",
    "resume-unlocked": "LockedHint=no",
}


class Serial:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        for _ in range(100):
            try:
                self.s.connect(path)
                break
            except OSError:
                time.sleep(0.2)
        self.s.settimeout(1)
        self.buf = b""

    def read_until(self, token, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if token.encode() in self.buf:
                return True
            try:
                chunk = self.s.recv(65536)
                if chunk:
                    self.buf += chunk
            except socket.timeout:
                pass
        return token.encode() in self.buf

    def send(self, text):
        self.s.sendall(text.encode())


def qmp(path, *commands):
    s = socket.socket(socket.AF_UNIX)
    s.connect(path)
    f = s.makefile("rw")
    f.readline()
    for cmd in [{"execute": "qmp_capabilities"}, *commands]:
        f.write(json.dumps(cmd) + "\n")
        f.flush()
        f.readline()
    s.close()


def main():
    iso, out = sys.argv[1], sys.argv[2]
    opts = dict(zip(sys.argv[3::2], sys.argv[4::2]))
    wait_graphical = int(opts.get("--wait-graphical", 0))
    sb_vars = opts.get("--secureboot")
    shot_after = opts.get("--screenshot-after")
    media = ["-drive", f"if=none,id=stick,format=raw,readonly=on,file={iso}",
             "-device", "qemu-xhci", "-device", "usb-storage,drive=stick,removable=on"] \
        if opts.get("--media") == "usb" else ["-cdrom", iso]
    os.makedirs(out, exist_ok=True)
    vars_fd = os.path.join(out, "vars.fd")
    subprocess.run(["cp", sb_vars or OVMF_VARS, vars_fd], check=True)
    code = OVMF_CODE_SB if sb_vars else OVMF_CODE
    machine = ["-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on"] \
        if sb_vars else ["-machine", "q35"]
    machine += ["-global", "ICH9-LPC.disable_s3=0"]           # S3, so the suspend probe really suspends
    ser, mon = os.path.join(out, "serial.sock"), os.path.join(out, "qmp.sock")
    accel = ["-enable-kvm", "-cpu", "host"] if os.access("/dev/kvm", os.R_OK | os.W_OK) and \
        subprocess.run(["qemu-system-x86_64", "-enable-kvm", "-machine", "none", "-display", "none",
                        "-S", "-qmp", "stdio"], input=b'{"execute":"quit"}', capture_output=True,
                       timeout=20).returncode == 0 else ["-accel", "tcg,thread=multi", "-cpu", "max"]
    qemu = subprocess.Popen([
        "qemu-system-x86_64", *accel, *machine, "-m", "8192", "-smp", "8",
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        *media, "-vga", "std", "-display", "none",
        "-nic", "user,model=virtio-net-pci",
        "-audiodev", "none,id=snd0", "-device", "intel-hda", "-device", "hda-output,audiodev=snd0",
        "-serial", f"unix:{ser},server,nowait", "-qmp", f"unix:{mon},server,nowait",
    ], stdout=open(os.path.join(out, "qemu.log"), "w"), stderr=subprocess.STDOUT)
    log = open(os.path.join(out, "serial.log"), "wb")
    try:
        time.sleep(2)
        if qemu.poll() is not None:
            print("FAIL: qemu exited:", open(os.path.join(out, "qemu.log")).read().strip()); return 1
        con = Serial(ser)
        if shot_after:
            con.read_until("@@never@@", int(shot_after))
            log.write(con.buf); log.flush()
            qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-1.ppm")}})
            print("screenshot taken; serial bytes:", len(con.buf))
            return 0
        ok = con.read_until("login:", 2400)
        log.write(con.buf); log.flush()
        if not ok:
            print("FAIL: no login prompt"); return 1
        con.send("root\n")
        con.read_until("#", 60)
        con.send("export SYSTEMD_PAGER= TERM=dumb; stty cols 200\n")
        time.sleep(wait_graphical)
        qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-1.ppm")}})
        results = {}
        for i, (name, cmd) in enumerate(PROBES):
            marker = f"@@END{i}@@"
            con.buf = b""
            # printf keeps the literal markers out of the terminal's echo of this line
            con.send(f"printf '@@%s@@\\n' BEGIN{i}; {cmd}; printf '@@%s@@\\n' END{i}\n")
            if name == "suspend":                              # wake the guest the way a key press would
                time.sleep(5)
                qmp(mon, {"execute": "system_wakeup"})
            if name == "resume-unlocked":
                qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-resume.ppm")}})
            con.read_until(marker + "\r\n", 120) or con.read_until(marker, 5)
            text = con.buf.decode(errors="replace")
            body = text.split(f"@@BEGIN{i}@@", 2)[-1].split(marker)[0].strip()
            results[name] = body
            log.write(con.buf); log.flush()
        time.sleep(5)
        qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-2.ppm")}})
        with open(os.path.join(out, "probes.txt"), "w") as f:
            for name, body in results.items():
                f.write(f"== {name}\n{body}\n")
        failed = [n for n, needle in REQUIRED.items() if needle not in results.get(n, "")]
        print("accel:", accel[0], "| failed required probes:", failed or "none")
        return 1 if failed else 0
    finally:
        qemu.terminate()
        try:
            qemu.wait(30)
        except subprocess.TimeoutExpired:
            qemu.kill()


if __name__ == "__main__":
    sys.exit(main())
