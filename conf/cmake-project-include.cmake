# kbuild: merged-/usr — GNUInstallDirs must never pick lib64 (kashira's
# /usr/lib64 is a symlink owned by kashira-filesystem; a real lib64 dir in a
# package causes pacman file conflicts).
set(CMAKE_INSTALL_LIBDIR lib CACHE PATH "kbuild: merged-/usr libdir" FORCE)
