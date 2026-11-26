#!/usr/bin/env bash
# Build dist/porthole_<version>_all.deb with plain dpkg-deb (no root or fakeroot needed).
# Run from the repository root:  packaging/deb/build.sh
#
# PORTHOLE_VERSION  the version (default: the vX.Y.Z git tag on this commit, else 0.1.0)
# OUT_DIR           where the .deb goes (default: dist)
set -euo pipefail
umask 022

if [[ ! -f start.py || ! -d porthole || ! -d web || ! -f LICENSE ]]; then
  echo "Run this from the repository root: packaging/deb/build.sh" >&2
  exit 1
fi

version="${PORTHOLE_VERSION:-}"
if [[ -z "$version" ]]; then
  tag="$(git describe --tags --exact-match --match 'v[0-9]*' 2>/dev/null || true)"
  version="${tag#v}"
fi
version="${version:-0.1.0}"
# No hyphen: this is a native package, so there is no Debian revision.
if [[ ! "$version" =~ ^[0-9][0-9A-Za-z.+~]*$ ]]; then
  echo "Not a usable package version: $version" >&2
  exit 1
fi

app_id="io.github.bahrambakhtiyar.Porthole"
maintainer="Bahram Bakhtiyar <bahrambakhtiyar@users.noreply.github.com>"
out_dir="${OUT_DIR:-dist}"
# dpkg-deb uses this for the file times, so the same commit builds the same package.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
export SOURCE_DATE_EPOCH

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
root="$work/root"
share="$root/usr/share"

install -d -m 0755 "$root" "$root/DEBIAN" "$root/usr/bin" "$share/porthole" "$share/applications" \
  "$share/icons/hicolor/256x256/apps" "$share/icons/hicolor/512x512/apps" "$share/metainfo" "$share/doc/porthole"

# The app: the Python package and the web pages, never caches.
cp -R porthole web "$share/porthole/"
find "$share/porthole" \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) -prune -exec rm -rf {} +
sed '1s|^#!/usr/bin/env python3$|#!/usr/bin/python3|' start.py >"$share/porthole/start.py"
find "$share/porthole" -type d -exec chmod 0755 {} +
find "$share/porthole" -type f -exec chmod 0644 {} +
chmod 0755 "$share/porthole/start.py"

cat >"$root/usr/bin/porthole" <<'WRAPPER'
#!/bin/sh
# PORTHOLE_COMMAND tells Porthole how it is started, for its hints ("Try: porthole --port 8766").
export PORTHOLE_COMMAND=porthole
# The system Python the package depends on, not whichever python3 comes first on PATH.
# -s -E: the user's own Python packages and PYTHON* settings stay out (PYTHONSAFEPATH, PYTHONPATH).
exec /usr/bin/python3 -s -E /usr/share/porthole/start.py "$@"
WRAPPER
chmod 0755 "$root/usr/bin/porthole"

install -m 0644 packaging/linux/porthole.desktop "$share/applications/porthole.desktop"
# The icons go by the app ID, not "porthole": icon themes are searched first, and some have an icon of that name
# (for an old package manager), which would show instead of Porthole's.
install -m 0644 assets/icon.png "$share/icons/hicolor/256x256/apps/$app_id.png"
install -m 0644 web/icon-512.png "$share/icons/hicolor/512x512/apps/$app_id.png"
install -m 0644 "packaging/linux/$app_id.metainfo.xml" "$share/metainfo/$app_id.metainfo.xml"
# The copyright file: packaging/deb/copyright, then the full text of LICENSE as its last stanza's body (Debian
# Policy 12.5 wants the licence itself, and AGPL-3.0 isn't in /usr/share/common-licenses). Made from LICENSE at
# every build, so it can't drift from it.
{
  cat packaging/deb/copyright
  bash packaging/deb/dep5_text.sh LICENSE
} >"$share/doc/porthole/copyright"
chmod 0644 "$share/doc/porthole/copyright"
{
  echo "porthole ($version) unstable; urgency=medium"
  echo
  echo "  * Porthole $version: https://github.com/bahrambakhtiyar/Porthole/releases"
  echo
  echo " -- $maintainer  $(LC_ALL=C date -u -R -d "@$SOURCE_DATE_EPOCH")"
} | gzip -9n >"$share/doc/porthole/changelog.gz"
chmod 0644 "$share/doc/porthole/changelog.gz"

installed_size="$(du -s -k "$root/usr" | cut -f1)"
cat >"$root/DEBIAN/control" <<CONTROL
Package: porthole
Version: $version
Architecture: all
Maintainer: $maintainer
Installed-Size: $installed_size
Depends: python3 (>= 3.10)
Recommends: libnotify-bin, xdg-utils
Section: net
Priority: optional
Homepage: https://github.com/bahrambakhtiyar/Porthole
Description: Share your screen with phones on the same Wi-Fi
 Porthole shows this computer's screen on phones, tablets and other
 computers on the same Wi-Fi or network. People watching open a link or
 scan a QR code in their browser and install nothing.
 .
 The picture goes straight from this computer to theirs over the local
 network and works without internet. The sharer page opens in its own
 Chrome, Edge or Chromium window when one is installed, otherwise in the
 default browser.
CONTROL
chmod 0644 "$root/DEBIAN/control"

mkdir -p "$out_dir"
deb="$out_dir/porthole_${version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$deb" >/dev/null
echo "Built $deb"
