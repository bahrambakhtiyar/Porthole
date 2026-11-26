#! /bin/bash
# python-appimage puts these lines into the AppImage's AppRun. The bundled Python runs start.py, which
# sits next to AppRun. -s and -E keep the user's own Python packages and settings out of it.
# Started without a terminal (a double-click in the file manager), --launcher shows start-up problems on screen.
# Output piped or redirected from a terminal (`--help | less`) still has the terminal as its input.
if [ ! -t 0 ] && [ ! -t 1 ]; then set -- --launcher "$@"; fi
exec "{{ python-executable }}" -s -E "${APPDIR}/start.py" "$@"
