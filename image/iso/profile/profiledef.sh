#!/usr/bin/env bash
# shellcheck disable=SC2034
# Kashira GNOME live ISO profile, a fork of archiso v92 configs/releng (../archiso/UPSTREAM).
# Secure Boot is not supported yet: the medium is unsigned, like Arch releng.

iso_name="kashira-gnome"
iso_label="KASHIRA_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="Kashira Linux <https://kashiraproject.org>"
iso_application="Kashira Linux GNOME live medium"
iso_version="$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y.%m.%d)"
install_dir="kashira"
# UEFI only: Kashira targets UEFI x86-64-v3 machines; no BIOS/syslinux path.
bootmodes=('uefi.systemd-boot')
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-Xdict-size' '1M')
bootstrap_tarball_compression=('zstd' '-c' '-T0' '--auto-threads=logical' '--long' '-19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/etc/sudoers"]="0:0:440"
  ["/etc/sudoers.d/10-kashira-live"]="0:0:440"
)
