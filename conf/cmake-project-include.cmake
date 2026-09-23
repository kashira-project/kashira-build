# kbuild: merged-/usr — GNUInstallDirs must never pick lib64 (kashira's
# /usr/lib64 is a symlink owned by kashira-filesystem; a real lib64 dir in a
# package causes pacman file conflicts).
# STRING, not PATH: a PATH-typed cache entry is absolutized against the cwd
# when it re-types an existing (e.g. CLI -D) entry, which installs files into
# $srcdir/lib instead of $prefix/lib (broke freeglut).
set(CMAKE_INSTALL_LIBDIR lib CACHE STRING "kbuild: merged-/usr libdir" FORCE)
