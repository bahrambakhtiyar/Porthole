#!/usr/bin/env bash
# Build dist/Porthole-<arch>.AppImage with python-appimage. Meant for CI: python-appimage downloads a
# portable Python AppImage and appimagetool. Run from the repository root:  packaging/appimage/build.sh
#
# python-appimage builds and packs in one go, so its AppImage is unpacked, trimmed of what Porthole never
# uses (packaging/appimage/trim.py: the GPL libraries among it), and packed again with the appimagetool
# python-appimage uses. appimagetool puts the AppImage runtime at the start of the file: THIRD-PARTY-NOTICES.txt
# names it (section 9), and CI checks that the built AppImage has that one.
#
# Needs python-appimage (the version is pinned in .github/workflows/linux.yml) and python3.
# PYTHON_VERSION  the bundled Python (default 3.12)
# BASE_IMAGE      optional: a Python AppImage already downloaded, named like
#                 python3.12.15-cp312-cp312-manylinux2014_x86_64.AppImage (skips python-appimage's
#                 unauthenticated GitHub API lookup, which can hit the rate limit on shared runners)
# APPIMAGETOOL    optional: the appimagetool to pack with (default: python-appimage's own)
# OUT_DIR         where the AppImage goes (default: dist)
set -euo pipefail
umask 022

if [[ ! -f start.py || ! -d porthole || ! -d web || ! -f LICENSE ]]; then
  echo "Run this from the repository root: packaging/appimage/build.sh" >&2
  exit 1
fi

app_id="io.github.bahrambakhtiyar.Porthole"
python_version="${PYTHON_VERSION:-3.12}"
out_dir="${OUT_DIR:-dist}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The recipe. python-appimage names the AppImage after the desktop file's Name= (Porthole), finds
# the icon by its Icon= name (the app ID, .png) and picks up metainfo only under the older *.appdata.xml name.
recipe="$work/porthole"
mkdir -p "$recipe"
cp packaging/appimage/entrypoint.sh packaging/appimage/requirements.txt "$recipe/"
cp packaging/linux/porthole.desktop "$recipe/porthole.desktop"
cp assets/icon.png "$recipe/$app_id.png" # by the app ID, as in the desktop file's Icon=
cp "packaging/linux/$app_id.metainfo.xml" "$recipe/$app_id.appdata.xml"

# The app, bundled next to AppRun as $APPDIR/porthole, $APPDIR/web and $APPDIR/start.py, with the licence
# (GNU AGPL-3.0) as $APPDIR/LICENSE and the notices for the Python and libraries the AppImage brings as
# $APPDIR/THIRD-PARTY-NOTICES.txt.
app="$work/app"
mkdir -p "$app"
cp -R porthole web start.py LICENSE "$app/"
cp packaging/appimage/THIRD-PARTY-NOTICES.txt "$app/"
find "$app" \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) -prune -exec rm -rf {} +

args=(build app -p "$python_version" "$recipe")
if [[ -n "${BASE_IMAGE:-}" ]]; then
  # Passed by its bare name from the work folder: python-appimage reads the Python version from the name.
  name="$(basename "$BASE_IMAGE")"
  cp "$BASE_IMAGE" "$work/$name"
  chmod 0755 "$work/$name"
  args+=(--base-image "$name")
fi
args+=(-x "$app/porthole" "$app/web" "$app/start.py" "$app/LICENSE" "$app/THIRD-PARTY-NOTICES.txt")

(cd "$work" && python-appimage "${args[@]}")

built=("$work"/Porthole-*.AppImage)
if [[ ! -f "${built[0]}" ]]; then
  echo "python-appimage made no AppImage." >&2
  exit 1
fi
name="$(basename "${built[0]}")" # Porthole-<arch>.AppImage
arch="${name#Porthole-}"
arch="${arch%.AppImage}"

# Unpack it. The AppImage runtime does that itself (no FUSE needed), into squashfs-root, with every folder
# 0700: give them back the usual 0755.
(cd "$work" && env -u APPIMAGE_EXTRACT_AND_RUN "./$name" --appimage-extract >/dev/null)
appdir="$work/squashfs-root"
if [[ ! -f "$appdir/AppRun" ]]; then
  echo "Couldn't unpack $name with --appimage-extract." >&2
  exit 1
fi
find "$appdir" -type d -exec chmod 0755 {} +

# Take out what Porthole never uses. trim.py refuses (and so stops the build) if something that stays still
# needs a library it takes out, or a library that stays isn't in THIRD-PARTY-NOTICES.txt.
python3 packaging/appimage/trim.py "$appdir"

# Pack it again with python-appimage's appimagetool, as python-appimage does. The build has just used it, so
# `python-appimage which appimagetool` should name it (only the path counts, colours and labels aside);
# else it is looked for in python-appimage's cache, then installed.
find_tool() {
  local line cache
  line="$(python-appimage which appimagetool 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | tail -n 1 || true)"
  if [[ -n "$line" && -x "$line" ]]; then
    echo "$line"
    return
  fi
  if [[ -n "$line" && -x "${line##* }" ]]; then
    echo "${line##* }"
    return
  fi
  for cache in "${XDG_CACHE_HOME:-$HOME/.cache}/python-appimage" "$HOME/.cache/python-appimage"; do
    [[ -d "$cache" ]] || continue
    line="$(find "$cache" -type f -perm -u+x \( -path '*appimagetool*/AppRun' -o -name 'appimagetool*' \) | sort | head -n 1)"
    if [[ -n "$line" ]]; then
      echo "Using the appimagetool in python-appimage's cache: $line" >&2
      echo "$line"
      return 0
    fi
  done
  return 0
}
tool="${APPIMAGETOOL:-}"
if [[ -n "$tool" && ! -x "$tool" ]]; then
  echo "APPIMAGETOOL ($tool) isn't a program that can be run." >&2
  exit 1
fi
if [[ -z "$tool" ]]; then
  tool="$(find_tool)"
fi
if [[ -z "$tool" ]]; then
  python-appimage install appimagetool >&2 || true
  tool="$(find_tool)"
fi
if [[ -z "$tool" ]]; then
  echo "Couldn't find appimagetool to pack the trimmed AppImage: 'python-appimage which appimagetool' named none," \
    "even after 'python-appimage install appimagetool', and python-appimage's cache has none." \
    "Set APPIMAGETOOL to an appimagetool." >&2
  exit 1
fi
packed="$work/packed/$name"
mkdir -p "$work/packed"
ARCH="$arch" APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --no-appstream "$appdir" "$packed"
if [[ ! -f "$packed" ]]; then
  echo "appimagetool ($tool) made no AppImage." >&2
  exit 1
fi

mkdir -p "$out_dir"
cp "$packed" "$out_dir/$name"
chmod 0755 "$out_dir/$name"
echo "Built $out_dir/$name"
