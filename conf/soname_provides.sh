#!/bin/bash
#
#   soname_provides.sh - add Arch-style versioned soname provides
#   (libfoo.so=N-64 / libfoo.so=N-32) for libraries in LIB_DIRS
#
#   kashira: pacman 7's autodep framework emits "lib:libfoo.so.1" entries
#   which satisfy nothing in the Arch packaging ecosystem; Arch packages
#   carry the =N-64 form. Emit that form so vendored Arch PKGBUILDs with
#   soname depends (libsystemd.so=0-64 style) resolve against our repo.
#
#   Copyright (c) 2026 kashira project
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.

[[ -n "$LIBMAKEPKG_AUTODEP_SONAME_PROVIDES_SH" ]] && return
LIBMAKEPKG_AUTODEP_SONAME_PROVIDES_SH=1

MAKEPKG_LIBRARY=${MAKEPKG_LIBRARY:-'/usr/share/makepkg'}

autodep_functions+=('soname_provides')

soname_provides() {
	check_option "autodeps" "y" || return

	local lib dir suffix fn sofile base ver
	for lib in ${LIB_DIRS[@]}; do
		dir=${lib#*:}
		case ${lib%%:*} in
			lib)   suffix=64 ;;
			lib32) suffix=32 ;;
			*)     continue ;;
		esac
		[[ -d "$pkgdir/$dir" ]] || continue

		while IFS= read -r fn; do
			LC_ALL=C readelf -h "$fn" 2>/dev/null | grep -q '.*Type:.*DYN (Shared object file).*' || continue
			sofile=$(LC_ALL=C readelf -d "$fn" 2>/dev/null | sed -n 's/.*Library soname: \[\(.*\)\].*/\1/p')
			[[ -z "$sofile" ]] && continue
			if [[ $sofile =~ ^(.*\.so)\.([0-9]+) ]]; then
				provides+=("${BASH_REMATCH[1]}=${BASH_REMATCH[2]}-$suffix")
			else
				provides+=("$sofile")
			fi
		done < <(find "$pkgdir/$dir" -maxdepth 1 -type f | LC_ALL=C sort)
	done
}
