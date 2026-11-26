# AppImage

`build.sh` turns Porthole into `dist/Porthole-x86_64.AppImage` with [python-appimage](https://github.com/niess/python-appimage): a portable Python plus Porthole's own files, so it runs on any recent Linux without installing anything. It downloads a Python AppImage and appimagetool, so it runs in CI (`.github/workflows/linux.yml`), not on this PC. python-appimage builds and packs in one go, so `build.sh` then unpacks that AppImage (`--appimage-extract`), trims it (below) and packs it again with python-appimage's appimagetool (`python-appimage which appimagetool`, installed with `python-appimage install appimagetool` if missing, or `APPIMAGETOOL`).

```bash
pip install python-appimage==1.4.7   # the version pinned in the workflow
bash packaging/appimage/build.sh
```

The recipe that python-appimage reads is put together from these files:

| File | Becomes |
| --- | --- |
| `entrypoint.sh` | The AppImage's `AppRun`: the bundled Python runs `$APPDIR/start.py`, with `--launcher` when there is no terminal (a double-click), so start-up problems show on screen |
| `requirements.txt` | Nothing to install (standard library only) |
| `../linux/porthole.desktop` | The menu entry inside the AppImage |
| `../../assets/icon.png` | `io.github.bahrambakhtiyar.Porthole.png`, the icon (named by the app ID, as in the desktop file) |
| `../linux/io.github.bahrambakhtiyar.Porthole.metainfo.xml` | `usr/share/metainfo/…appdata.xml` (python-appimage only reads that older name) |
| `porthole/`, `web/`, `start.py`, `LICENSE`, `THIRD-PARTY-NOTICES.txt` | Bundled next to `AppRun` with `-x` |

**Trimming and notices.** The portable Python (a manylinux2014 build) brings libraries Porthole never uses, among them GNU Readline and GDBM, which are GPL-3.0 and would oblige a source-code offer. The trim doesn't end that: the AppImage runtime, which appimagetool puts at the start of the file, has libfuse (LGPL-2.1) linked in, and where its source comes from is still to be decided. `trim.py` takes out, before packing: Readline, GDBM, Tcl/Tk with libXft and libXrender, ncurses (with panel and tinfo), SQLite, libcrypt and libnsl from `usr/lib`; the Python modules that load them (`readline`, `_gdbm`, `_dbm`, `_tkinter`, `_curses`, `_curses_panel`, `_sqlite3`, `_crypt`, `nis`); the packages built on those (`tkinter`, `idlelib`, `turtle`, `turtledemo`, `sqlite3`, `curses`, `dbm`, `crypt`); Tcl/Tk's folders; and `ensurepip`, whose copy of pip has none of the licences of the libraries pip includes. OpenSSL 3, libffi, bzip2 and xz stay. It refuses, so the build fails, when a file that stays still needs a library it takes out (it reads each ELF file's NEEDED entries), when a library that stays in `usr/lib` isn't named in `THIRD-PARTY-NOTICES.txt` (a newer base image brought one: take it out in `trim.py`, or add it and its licence to the notices), or when the notices or Python's `LICENSE.txt` are missing. `THIRD-PARTY-NOTICES.txt` lists what is bundled and under which licence, with the texts those licences ask for, and points to Python's own `opt/python3.12/lib/python3.12/LICENSE.txt`; it also covers python-appimage's own files (`AppRun`'s first lines, `usr/bin/python3.12`, `sitecustomize.py`) and the AppImage runtime, by its commit. CI unpacks the built AppImage and runs `trim.py --check` on it, compares the notices, and checks that its Python imports `ssl`, `hmac`, `secrets`, `uuid`, `ctypes` and the rest, but not `tkinter`, `sqlite3`, `readline`, `curses`, `dbm.gnu` or `ensurepip`. It also checks that the AppImage's `--appimage-version` is the runtime commit the notices name: the newer appimagetool fetches the latest runtime at each build, so a new runtime fails CI until section 9 of the notices is updated for it.

To try the trim on an unpacked AppImage (a copy: it changes the folder): `python3 packaging/appimage/trim.py --dry-run squashfs-root`, then without `--dry-run`, then `--check`.

**Where `Exec=` comes from.** An AppImage is mounted in a new temporary folder on every run, so nothing inside it has a lasting path. Its menu entry says `Exec=porthole --launcher`; desktop-integration tools (AppImageLauncher, Gear Lever, appimaged) replace `porthole` with the path of the `.AppImage` file. `--install-launcher` run from the AppImage does the same itself: the AppImage runtime sets `$APPIMAGE` to that path, and Porthole writes `Exec="$APPIMAGE" --launcher` and keeps a copy of the icon (`web/icon-512.png`) in `~/.local/share/icons`. For the same reason, "Already running" compares `$APPIMAGE`, not the mount folder. Programs started from another AppImage (a terminal inside an editor, say) inherit its `$APPIMAGE` and `$APPDIR`, so Porthole uses `$APPIMAGE` only when its own files are inside `$APPDIR`.

To run the AppImage without FUSE (as CI does): `APPIMAGE_EXTRACT_AND_RUN=1 ./Porthole-x86_64.AppImage`.
