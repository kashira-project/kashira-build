#!/bin/bash
# kbuild: merged-/usr — cmake has no environment-variable form of
# CMAKE_PROJECT_INCLUDE (variable only, no entry in cmake-env-variables(7)),
# so the old `export CMAKE_PROJECT_INCLUDE=...` in kbuild-inside.sh was a
# no-op and packages still installed into /usr/lib64. Wrap the cmake binary
# instead and inject conf/cmake-project-include.cmake on configure runs.
# Non-configure modes pass through untouched.
for arg in "$@"; do
  case "$arg" in
    --build|--install|-E|-P|--find-package|--open|--version|--help*|--graphviz*|--system-information|--workflow)
      exec /usr/bin/cmake "$@" ;;
  esac
done
exec /usr/bin/cmake -DCMAKE_PROJECT_INCLUDE=/kbuild/conf/cmake-project-include.cmake "$@"
