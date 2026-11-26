# Signed APT repository

`build-repo.sh` turns the packages in `dist/` into a signed APT repository (with [reprepro](https://salsa.debian.org/debian/reprepro)) that can be published as plain files, for example on GitHub Pages at `https://bahrambakhtiyar.github.io/Porthole/apt`. Not used yet.

## Once: create the signing key (the maintainer)

An Ed25519 key that signs with SHA-256. Run on your own PC; gpg asks for a passphrase.

```bash
gpg --cert-digest-algo SHA256 --personal-digest-preferences SHA256 \
    --default-preference-list "SHA256 SHA384 SHA512 AES256 AES192 AES ZLIB BZIP2 ZIP Uncompressed" \
    --quick-generate-key "Porthole APT repository <bahrambakhtiyar@users.noreply.github.com>" ed25519 sign 5y

FPR="$(gpg --with-colons --list-secret-keys "Porthole APT repository" | awk -F: '$1 == "fpr" {print $10; exit}')"
echo "$FPR"                                                    # the fingerprint: 40 characters

gpg --armor --export "$FPR" > porthole.asc                     # public key: safe to share
gpg --armor --export-secret-keys "$FPR" > porthole-apt-private.asc   # private key: keep it secret
cp ~/.gnupg/openpgp-revocs.d/"$FPR".rev porthole-apt-revoke.rev       # revokes the key if it is ever lost
```

Back up `porthole-apt-private.asc`, `porthole-apt-revoke.rev` and the passphrase in your password manager (or on an encrypted USB stick), then delete the two files: `shred -u porthole-apt-private.asc porthole-apt-revoke.rev`. The key expires in 5 years; extend it before then with `gpg --quick-set-expire "$FPR" 5y` and publish the new `porthole.asc`.

For CI, add three repository secrets: `APT_SIGNING_KEY` (the whole of `porthole-apt-private.asc`), `APT_SIGNING_KEY_ID` (the fingerprint) and `APT_SIGNING_PASSPHRASE`.

## Build the repository

```bash
sudo apt install reprepro gnupg
bash packaging/deb/build.sh
APT_SIGNING_KEY="$(cat porthole-apt-private.asc)" APT_SIGNING_KEY_ID="$FPR" APT_SIGNING_PASSPHRASE='…' \
  bash packaging/apt/build-repo.sh apt-repo
```

`apt-repo/` then holds `dists/`, `pool/` and `porthole.asc`. Publish the whole folder as `/apt` on the website. The key is imported into a throwaway keyring, never into `~/.gnupg`.

## What users run (one line)

```bash
sudo install -d -m 0755 /etc/apt/keyrings && sudo curl -fsSLo /etc/apt/keyrings/porthole.asc https://bahrambakhtiyar.github.io/Porthole/apt/porthole.asc && echo "deb [signed-by=/etc/apt/keyrings/porthole.asc] https://bahrambakhtiyar.github.io/Porthole/apt stable main" | sudo tee /etc/apt/sources.list.d/porthole.list && sudo apt update && sudo apt install porthole
```
