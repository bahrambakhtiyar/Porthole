#!/usr/bin/env bash
# Build a signed APT repository from dist/*.deb into a folder (default: apt-repo/), ready to publish as
# plain files (GitHub Pages). Uses reprepro and gpg. Run from the repository root:
#   bash packaging/apt/build-repo.sh [folder]
#
# APT_SIGNING_KEY         the private signing key, ASCII-armoured (a secret; in CI, a repository secret)
# APT_SIGNING_KEY_ID      its full fingerprint (40 hex characters)
# APT_SIGNING_PASSPHRASE  its passphrase, if it has one
# APT_SUITE               the suite (default: stable)
#
# The key is imported into a throwaway keyring that is deleted afterwards; ~/.gnupg is never touched.
set -euo pipefail
umask 022

out="${1:-apt-repo}"
suite="${APT_SUITE:-stable}"
: "${APT_SIGNING_KEY:?Set APT_SIGNING_KEY to the ASCII-armoured private key}"
: "${APT_SIGNING_KEY_ID:?Set APT_SIGNING_KEY_ID to the key fingerprint}"
if [[ ! "$APT_SIGNING_KEY_ID" =~ ^[0-9A-Fa-f]{40}$ ]]; then
  echo "APT_SIGNING_KEY_ID must be the full 40-character fingerprint." >&2
  exit 1
fi
if [[ ! "$suite" =~ ^[a-z0-9][a-z0-9.-]*$ ]]; then
  echo "Not a usable suite name: $suite" >&2
  exit 1
fi
for tool in reprepro gpg gpgconf gpg-connect-agent; do
  if ! command -v "$tool" >/dev/null; then
    echo "Needs $tool: sudo apt install reprepro gnupg" >&2
    exit 1
  fi
done
shopt -s nullglob
debs=(dist/*.deb)
if ((${#debs[@]} == 0)); then
  echo "No packages in dist/. Build one first: bash packaging/deb/build.sh" >&2
  exit 1
fi

work="$(mktemp -d)"
export GNUPGHOME="$work/gnupg"
cleanup() {
  gpgconf --kill all >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

install -d -m 0700 "$GNUPGHOME"
cat >"$GNUPGHOME/gpg.conf" <<'CONF'
personal-digest-preferences SHA256
digest-algo SHA256
cert-digest-algo SHA256
CONF
echo "allow-preset-passphrase" >"$GNUPGHOME/gpg-agent.conf"
printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import
if [[ -n "${APT_SIGNING_PASSPHRASE:-}" ]]; then
  # reprepro signs through gpg-agent, which can't ask anyone here: give it the passphrase in advance.
  gpg-connect-agent /bye >/dev/null
  preset="$(gpgconf --list-dirs libexecdir)/gpg-preset-passphrase"
  while read -r grip; do
    printf '%s' "$APT_SIGNING_PASSPHRASE" | "$preset" --preset "$grip"
  done < <(gpg --batch --with-colons --with-keygrip --list-secret-keys "$APT_SIGNING_KEY_ID" | awk -F: '$1 == "grp" {print $10}')
fi

conf="$work/conf"
mkdir -p "$conf"
cat >"$conf/distributions" <<CONF
Origin: Porthole
Label: Porthole
Suite: $suite
Codename: $suite
Architectures: amd64 arm64 armhf i386 riscv64
Components: main
Description: Porthole: share your screen with phones on the same Wi-Fi
SignWith: $APT_SIGNING_KEY_ID
CONF

mkdir -p "$out"
out="$(cd "$out" && pwd)"
rm -rf "$out/dists" "$out/pool" # built fresh from dist/ every time
reprepro --basedir "$work" --confdir "$conf" --dbdir "$work/db" --outdir "$out" includedeb "$suite" "${debs[@]}"
gpg --armor --export "$APT_SIGNING_KEY_ID" >"$out/porthole.asc"
gpg --batch --verify "$out/dists/$suite/InRelease" 2>/dev/null
echo "Signed APT repository in $out (suite $suite). Publish the whole folder, porthole.asc included."
