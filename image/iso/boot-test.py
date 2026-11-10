#!/usr/bin/env python3
"""Boot a Kashira live ISO in QEMU/OVMF and probe it over the serial console.

  image/iso/boot-test.py <iso> <outdir> [--wait-graphical SECONDS]
                          [--secureboot VARS.fd] [--screenshot-after SECONDS] [--media usb]
                          [--install SERVER_URL] [--probe NAME=COMMAND] [--disk SIZE]
                          [--recipe RECIPE.json --secrets SECRETS.json --share INSTALLER_CHECKOUT]

--secureboot boots the Secure Boot OVMF build (SMM) with the given enrolled
variable store. --screenshot-after only takes a screenshot after SECONDS and
exits (for boots that stop before a login prompt, e.g. MokManager).
--media usb attaches the ISO as a USB stick instead of a CD-ROM, like a flashed
drive (archiso then copies the root image to RAM).
--recipe installs with kashira-install from the --share checkout (virtio-9p) onto a blank 40 GiB disk, then
boots that disk without the ISO and checks it (POST_PROBES). The harness adds a VM-only serial root autologin.
--install adds a blank 16 GiB virtio disk and a final `install` probe: a standard Arch install
(sgdisk, mkfs, pacstrap base linux, genfstab, bootctl install) from SERVER_URL, signatures required
(e.g. https://repo.kashiraproject.org/x86_64-v3).

Logs in as root on ttyS0 (live media only: root has no password), runs the
probe commands below (including a suspend, QMP wake and lock check), saves their output
to <outdir>/probes.txt and takes QMP
screendumps (<outdir>/screen-*.ppm). Uses KVM when /dev/kvm is usable, else TCG
with -cpu max (x86-64-v3 needs AVX2; a TCG boot takes ~10 minutes). /dev/kvm is root:kvm here and the build
user is not in that group, so for a KVM run start the whole script as root:
  sudo python3 image/iso/boot-test.py <iso> <outdir> [...]
Output files are chowned back to $SUDO_UID afterwards. Exit status 0 only if every probe in
REQUIRED passed.
"""
import json, os, shutil, socket, subprocess, sys, time

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
# --install URL: a standard Arch install onto a blank virtio disk from the repo at URL (signed): partition, mkfs, pacstrap base linux, genfstab, bootctl install, pacman inside the chroot
INSTALL = ("install",
           "sgdisk -Z /dev/vda >/dev/null && sgdisk -n1:0:+512M -t1:ef00 -n2:0:0 -t2:8304 /dev/vda >/dev/null && "
           "mkfs.fat -F32 /dev/vda1 >/dev/null && mkfs.ext4 -q /dev/vda2 && mount /dev/vda2 /mnt && "
           "mount --mkdir /dev/vda1 /mnt/boot && "
           "printf '[options]\\nArchitecture = x86_64\\nSigLevel = Required DatabaseRequired\\n[kashira-v3]\\nServer = @SERVER@\\n' > /tmp/inst.conf && "
           "pacstrap -C /tmp/inst.conf -K /mnt base linux > /tmp/pacstrap.log 2>&1 && echo PACSTRAP_OK; "
           "tail -n 3 /tmp/pacstrap.log; genfstab -U /mnt >> /mnt/etc/fstab; "
           "arch-chroot /mnt bootctl install > /tmp/bootctl.log 2>&1; "
           "test -f /mnt/boot/EFI/systemd/systemd-bootx64.efi && echo BOOTCTL_OK; "
           "arch-chroot /mnt pacman -Q pacman && echo CHROOT_OK")
INSTALL_TIMEOUT = 1800
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
    out = []
    for cmd in [{"execute": "qmp_capabilities"}, *commands]:
        f.write(json.dumps(cmd) + "\n")
        f.flush()
        while True:                                      # skip async events until the reply
            r = json.loads(f.readline() or '{"error": "eof"}')
            if "event" not in r:
                break
        out.append(r)
    s.close()
    return out[1:]


def wake_when_suspended(mon, con, timeout=600):
    """system_wakeup only works once the guest is in S3, and a too-early wakeup is silently dropped, so poll the
    run state instead of waking after a fixed delay. Keep reading the serial socket while polling: if the host stops
    draining it, the guest's console TX stalls and the 8250 console suspend never completes (guest never reaches S3)."""
    end = time.time() + timeout
    while time.time() < end:
        con.read_until("@@never@@", 1)
        if qmp(mon, {"execute": "query-status"})[0].get("return", {}).get("status") == "suspended":
            return "error" not in qmp(mon, {"execute": "system_wakeup"})[0]
    return False


# --recipe: phase 2 boots the installed disk (no ISO) and checks the result.
POST_PROBES = [
    ("root-fs", "echo fstype=$(findmnt -no FSTYPE /); findmnt -no OPTIONS /"),
    ("blocks", "lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS"),
    ("zram", "cat /sys/block/zram0/comp_algorithm; zramctl --noheadings --output NAME,DISKSIZE,ALGORITHM"),
    ("tier", "cat /etc/kashira/tier"),
    ("dns", "ls /etc/systemd/resolved.conf.d/ /etc/NetworkManager/conf.d/ 2>&1; resolvectl status | sed -n '1,6p'"),
    ("boot", "bootctl status 2>/dev/null | grep -E 'Product|Current Entry|Default Entry'; ls /boot/EFI/Linux"),
    ("gdm", "systemctl is-active gdm.service"),
    ("graphical-target", "systemctl is-active graphical.target"),
    ("user", "getent passwd @USER@; id @USER@; sudo -l -U @USER@ | tail -n 2; passwd -S root"),
    ("locale", "localectl status | sed -n '1,3p'; timedatectl show -p Timezone"),
    ("pacman", "pacman -Q pacman linux; pacman -Qq | wc -l"),
    ("failed-units", "systemctl --failed --no-legend; echo FAILED_UNITS_END"),
    ("journal-errors", "journalctl -b -p err --no-pager | tail -n 25"),
]
SHARE_TAG = "kinst"


def start_qemu(out, tag, accel, machine, code, vars_fd, media, disk, share=None):
    ser, mon = os.path.join(out, f"serial-{tag}.sock"), os.path.join(out, f"qmp-{tag}.sock")
    extra = ["-virtfs", f"local,path={share},mount_tag={SHARE_TAG},security_model=none,readonly=on"] if share else []
    qemu = subprocess.Popen([
        "qemu-system-x86_64", *accel, *machine, "-m", "8192", "-smp", "8",
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        *media, *disk, *extra, "-vga", "std", "-display", "none",
        "-nic", "user,model=virtio-net-pci",
        "-audiodev", "none,id=snd0", "-device", "intel-hda", "-device", "hda-output,audiodev=snd0",
        "-serial", f"unix:{ser},server,nowait", "-qmp", f"unix:{mon},server,nowait",
    ], stdout=open(os.path.join(out, f"qemu-{tag}.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(2)
    return qemu, ser, mon


def stop_qemu(qemu, mon=None):
    """ACPI power-down first (a hard stop leaves the guest's ESP with its dirty bit set), then terminate."""
    if mon and qemu.poll() is None:
        try:
            qmp(mon, {"execute": "system_powerdown"})
            qemu.wait(90)
        except (OSError, subprocess.TimeoutExpired):
            pass
    qemu.terminate()
    try:
        qemu.wait(30)
    except subprocess.TimeoutExpired:
        qemu.kill()


def type_text(mon, text):
    """Type into the VM's keyboard (QMP send-key): the LUKS prompt on the console."""
    keys = {" ": "spc", "-": "minus", ".": "dot", "_": "shift-minus"}
    for ch in text:
        k = keys.get(ch, ch.lower())
        if ch.isupper():
            k = "shift-" + k
        qmp(mon, {"execute": "human-monitor-command", "arguments": {"command-line": f"sendkey {k}"}})
        time.sleep(0.05)
    qmp(mon, {"execute": "human-monitor-command", "arguments": {"command-line": "sendkey ret"}})


def run_probes(con, mon, out, probes, log, timeouts=None):
    results = {}
    for i, (name, cmd) in enumerate(probes):
        marker = f"@@END{i}@@"
        con.buf = b""
        # printf keeps the literal markers out of the terminal's echo of this line
        con.send(f"printf '@@%s@@\\n' BEGIN{i}; {cmd}; printf '@@%s@@\\n' END{i}\n")
        if name == "suspend":                              # wake the guest the way a key press would
            if not wake_when_suspended(mon, con):
                print("suspend: guest never reached S3 or wakeup failed")
        if name == "resume-unlocked":
            qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-resume.ppm")}})
        con.read_until(marker + "\r\n", (timeouts or {}).get(name, 120)) or con.read_until(marker, 5)
        text = con.buf.decode(errors="replace")
        results[name] = text.split(f"@@BEGIN{i}@@", 2)[-1].split(marker)[0].strip()
        log.write(con.buf); log.flush()
    return results


def login(con, log, timeout=2400):
    ok = con.read_until("login:", timeout)
    log.write(con.buf); log.flush()
    if ok:
        if b"automatic login" not in con.buf:
            con.send("root\n")
        con.read_until("#", 60)
        con.send("export SYSTEMD_PAGER= TERM=dumb; stty cols 200\n")
        con.read_until("#", 10)
    return ok


def recipe_mode(out, iso, opts, accel, machine, code, vars_fd):
    """Phase 1: install from the live ISO with the shared checkout; phase 2: boot the disk and probe it."""
    recipe = json.load(open(opts["--recipe"]))
    secrets = json.load(open(opts["--secrets"]))
    share = os.path.join(out, "share")
    shutil.rmtree(share, ignore_errors=True)
    shutil.copytree(opts["--share"], share, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    json.dump(recipe, open(os.path.join(share, "recipe.json"), "w"))
    json.dump(secrets, open(os.path.join(share, "secrets.json"), "w"))
    img = os.path.join(out, "disk.qcow2")
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", img, "40G"], check=True)
    disk = ["-drive", f"file={img},if=virtio,format=qcow2"]
    fs, luks, user = recipe["disk"]["filesystem"], recipe["disk"].get("encrypt"), recipe["user"]["name"]
    harness = ("set -e; dev=/dev/vda2; "
               + (f"printf %s '{secrets['luks_passphrase']}' | cryptsetup open --key-file - $dev troot; dev=/dev/mapper/troot; " if luks else "")
               + f"mount {'-o subvol=@ ' if fs == 'btrfs' else ''}$dev /mnt; "
               "d=/mnt/etc/systemd/system/serial-getty@ttyS0.service.d; mkdir -p $d /mnt/etc/systemd/system/getty.target.wants; "
               "printf '[Unit]\\nConditionVirtualization=vm\\n[Service]\\nExecStart=\\nExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,57600,38400,9600 - $TERM\\n' > $d/kashira-test.conf; "
               "ln -sf /usr/lib/systemd/system/serial-getty@.service /mnt/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service; "
               "umount /mnt; " + ("cryptsetup close troot; " if luks else "") + "echo HARNESS_OK")
    phase1 = [
        ("mount-share", f"modprobe 9pnet_virtio; mkdir -p /run/kinst; mount -t 9p -o trans=virtio,version=9p2000.L {SHARE_TAG} /run/kinst && echo SHARE_OK"),
        ("install", "cd /run/kinst && python3 -m kinst.cli --recipe /run/kinst/recipe.json --log /tmp/kinst.log < /run/kinst/secrets.json | tail -n 4; "
                    "grep -aE '^(\\$ |== |FAILED)|==> (ERROR|WARNING)|rror|Building|UKI' /tmp/kinst.log | tail -n 60"),
        ("harness", harness),
    ]
    results = {}
    log = open(os.path.join(out, "serial.log"), "wb")
    qemu, ser, mon = start_qemu(out, "install", accel, machine, code, vars_fd, ["-cdrom", iso], disk, share)
    try:
        con = Serial(ser)
        if not login(con, log):
            print("FAIL: live ISO gave no login prompt"); return 1
        results.update(run_probes(con, mon, out, phase1, log, {"install": INSTALL_TIMEOUT}))
        con.send("umount /run/kinst; sync\n")
        con.read_until("#", 30)
    finally:
        stop_qemu(qemu, mon)
    if '"type": "done"' not in results.get("install", "") or "HARNESS_OK" not in results.get("harness", ""):
        write_results(out, results)
        print("FAIL: install phase:", results.get("install", "")[-600:]); return 1
    qemu, ser, mon = start_qemu(out, "installed", accel, machine, code, vars_fd, [], disk)
    try:
        con = Serial(ser)
        if luks:                                           # the passphrase prompt is on the console
            for _ in range(3):
                time.sleep(25)
                type_text(mon, secrets["luks_passphrase"])
                if con.read_until("login:", 60):
                    break
        if not login(con, log, 600):
            qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-installed.ppm")}})
            print("FAIL: installed system gave no login prompt"); return 1
        time.sleep(20)                                     # let gdm come up
        probes = [(n, c.replace("@USER@", user)) for n, c in POST_PROBES]
        results.update(run_probes(con, mon, out, probes, log))
        qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-installed.ppm")}})
    finally:
        stop_qemu(qemu, mon)
    write_results(out, results)
    # 8 GiB VM: zstd is the expected zram algorithm (D4: lz4 only from 16 GiB)
    required = {"gdm": "active", "graphical-target": "active", "root-fs": f"fstype={fs}", "zram": "[zstd]",
                "tier": "x86_64-v3", "user": "(ALL : ALL) ALL", "boot": "kashira-linux.efi"}
    if luks:
        required["blocks"] = "crypt"
    failed = [n for n, needle in required.items() if needle not in results.get(n, "")]
    if results.get("failed-units", "").split("FAILED_UNITS_END")[0].strip():
        failed.append("failed-units")
    print("accel:", accel[0], "| recipe:", fs, "luks" if luks else "plain", "| failed required probes:", failed or "none")
    return 1 if failed else 0


def write_results(out, results):
    with open(os.path.join(out, "probes.txt"), "w") as f:
        for name, body in results.items():
            f.write(f"== {name}\n{body}\n")


def main():
    iso, out = sys.argv[1], sys.argv[2]
    opts = dict(zip(sys.argv[3::2], sys.argv[4::2]))
    wait_graphical = int(opts.get("--wait-graphical", 0))
    sb_vars = opts.get("--secureboot")
    shot_after = opts.get("--screenshot-after")
    install_url = opts.get("--install")
    os.makedirs(out, exist_ok=True)
    vars_fd = os.path.join(out, "vars.fd")
    subprocess.run(["cp", sb_vars or OVMF_VARS, vars_fd], check=True)
    code = OVMF_CODE_SB if sb_vars else OVMF_CODE
    machine = ["-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on"] \
        if sb_vars else ["-machine", "q35"]
    machine += ["-global", "ICH9-LPC.disable_s3=0"]           # S3, so the suspend probe really suspends
    accel = ["-enable-kvm", "-cpu", "host"] if os.access("/dev/kvm", os.R_OK | os.W_OK) and \
        subprocess.run(["qemu-system-x86_64", "-enable-kvm", "-machine", "none", "-display", "none",
                        "-S", "-qmp", "stdio"], input=b'{"execute":"qmp_capabilities"}{"execute":"quit"}', capture_output=True,
                       timeout=20).returncode == 0 else ["-accel", "tcg,thread=multi", "-cpu", "max"]
    try:
        if "--recipe" in opts:
            return recipe_mode(out, iso, opts, accel, machine, code, vars_fd)
        probes, disk = list(PROBES), []
        if "--probe" in opts:                                   # one extra ad-hoc probe, run before install
            name, _, cmd = opts["--probe"].partition("=")
            probes.append((name, cmd))
        if install_url:
            probes.append((INSTALL[0], INSTALL[1].replace("@SERVER@", install_url)))
            for needle in ("PACSTRAP_OK", "BOOTCTL_OK", "CHROOT_OK"):
                REQUIRED[f"install:{needle}"] = needle
            img = os.path.join(out, "disk.qcow2")
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", img, "16G"], check=True)
            disk = ["-drive", f"file={img},if=virtio,format=qcow2"]
        elif "--disk" in opts:                                  # a blank disk for the live GUI's checks
            img = os.path.join(out, "disk.qcow2")
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", img, opts["--disk"]], check=True)
            disk = ["-drive", f"file={img},if=virtio,format=qcow2"]
        media = ["-drive", f"if=none,id=stick,format=raw,readonly=on,file={iso}",
                 "-device", "qemu-xhci", "-device", "usb-storage,drive=stick,removable=on"] \
            if opts.get("--media") == "usb" else ["-cdrom", iso]
        qemu, ser, mon = start_qemu(out, "live", accel, machine, code, vars_fd, media, disk)
        log = open(os.path.join(out, "serial.log"), "wb")
        try:
            if qemu.poll() is not None:
                print("FAIL: qemu exited:", open(os.path.join(out, "qemu-live.log")).read().strip()); return 1
            con = Serial(ser)
            if shot_after:
                con.read_until("@@never@@", int(shot_after))
                log.write(con.buf); log.flush()
                qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-1.ppm")}})
                print("screenshot taken; serial bytes:", len(con.buf))
                return 0
            if not login(con, log):
                print("FAIL: no login prompt"); return 1
            time.sleep(wait_graphical)
            qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-1.ppm")}})
            results = run_probes(con, mon, out, probes, log, {"install": INSTALL_TIMEOUT})
            time.sleep(5)
            qmp(mon, {"execute": "screendump", "arguments": {"filename": os.path.join(out, "screen-2.ppm")}})
        finally:
            stop_qemu(qemu)
        write_results(out, results)
        failed = [n for n, needle in REQUIRED.items() if needle not in results.get(n.split(":")[0], "")]
        print("accel:", accel[0], "| failed required probes:", failed or "none")
        return 1 if failed else 0
    finally:
        if os.environ.get("SUDO_UID"):                     # sudo run for KVM: hand the output back to the invoker
            subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ.get('SUDO_GID', '')}", out])


if __name__ == "__main__":
    sys.exit(main())
