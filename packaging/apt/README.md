# Signed APT repository

`build-repo.sh` turns the packages in `dist/` into a signed APT repository (with [reprepro](https://salsa.debian.org/debian/reprepro)) that is published as plain files on GitHub Pages at `https://bahrambakhtiyar.github.io/Porthole/apt`.

- **Publishing:** `.github/workflows/apt.yml` runs when a GitHub release is published. It downloads that release's .deb, builds the repository with this script and the signing key, publishes it on GitHub Pages with `index.html` (a page for anyone who opens the address in a browser), and then runs the one-line setup below on a fresh machine, as a person would, and checks that the Porthole it installs starts and quits. The repository holds one version, so only the newest release is published: an older tag or a pre-release is refused. By hand (Actions, APT repository, Run workflow, from `main`, with the newest tag), it publishes a release that is already out.
- **The key stays locked away:** its three secrets belong to the environment `apt-signing`, not to the repository, and only `main` and `v*` tags may use that environment. Code pushed to any other branch never gets the key, even when someone starts the workflow from that branch.
- **Tested on every build:** the Linux workflow's deb job puts the .deb it built into a repository signed with a throwaway key made in that job, then installs it from there with apt, which checks the signature. So this script is tested long before a release needs it.

## Once: the maintainer's steps

### 1. Switch on GitHub Pages and the two environments

- Settings, Pages: set **Source** to **GitHub Actions**. This makes the environment `github-pages`.
- Settings, Environments, `github-pages`: under deployment branches and tags, allow tags matching `v*` as well as `main` (a published release runs `apt.yml` on its tag).
- Settings, Environments, **New environment** `apt-signing`: deployment branches and tags limited to `main` and tags matching `v*`.

### 2. Make the signing key and give it to the workflow

An Ed25519 key that signs with SHA-256. Run this on your own PC, in a new private folder outside the repository (so the key files can never be committed by mistake); gpg asks for a passphrase.

```bash
umask 077 && cd "$(mktemp -d)"

gpg --cert-digest-algo SHA256 --personal-digest-preferences SHA256 \
    --default-preference-list "SHA256 SHA384 SHA512 AES256 AES192 AES ZLIB BZIP2 ZIP Uncompressed" \
    --quick-generate-key "Porthole APT repository <bahrambakhtiyar@users.noreply.github.com>" ed25519 sign 5y

FPR="$(gpg --with-colons --list-secret-keys "Porthole APT repository" | awk -F: '$1 == "fpr" {print $10; exit}')"
echo "$FPR"                                                    # the fingerprint: 40 characters

gpg --armor --export-secret-keys "$FPR" > porthole-apt-private.asc   # private key: keep it secret
cp ~/.gnupg/openpgp-revocs.d/"$FPR".rev porthole-apt-revoke.rev       # revokes the key if it is ever lost

gh secret set APT_SIGNING_KEY --env apt-signing --repo bahrambakhtiyar/Porthole < porthole-apt-private.asc
gh secret set APT_SIGNING_KEY_ID --env apt-signing --repo bahrambakhtiyar/Porthole --body "$FPR"
gh secret set APT_SIGNING_PASSPHRASE --env apt-signing --repo bahrambakhtiyar/Porthole   # asks for it, hidden
```

Then back up `porthole-apt-private.asc`, `porthole-apt-revoke.rev` and the passphrase (in your password manager, or on an encrypted USB stick; the private key file is itself locked with the passphrase, so keep the passphrase apart from the file), and delete the folder: `shred -u porthole-apt-private.asc porthole-apt-revoke.rev && cd ~`. The key also stays in your own `~/.gnupg`, locked with the passphrase.

### 3. Publish the release that is already out

A release published before `apt.yml` existed never ran it. Run it once by hand: Actions, **APT repository**, **Run workflow**, from `main`, with the newest tag (for example `v0.1.0`). When its last job, `check`, is green, the setup below works. Later releases publish by themselves.

## Renewing the key (before it expires in 5 years)

```bash
FPR="$(gpg --with-colons --list-secret-keys "Porthole APT repository" | awk -F: '$1 == "fpr" {print $10; exit}')"
gpg --quick-set-expire "$FPR" 5y
umask 077 && cd "$(mktemp -d)" && gpg --armor --export-secret-keys "$FPR" > porthole-apt-private.asc
gh secret set APT_SIGNING_KEY --env apt-signing --repo bahrambakhtiyar/Porthole < porthole-apt-private.asc
```

Replace the backup with this new `porthole-apt-private.asc`, delete it here, and run `apt.yml` by hand with the newest tag, which publishes the renewed public key. Each person who added the repository keeps the copy of the key they downloaded, with its old date, so they need to run the setup's first two parts (the key download) again once; tell them in that release's notes.

## Build the repository by hand

From the repository folder, with the key from your own `~/.gnupg` (it never touches the disk here):

```bash
sudo apt install reprepro gnupg
bash packaging/deb/build.sh
APT_SIGNING_KEY="$(gpg --armor --export-secret-keys "$FPR")" APT_SIGNING_KEY_ID="$FPR" APT_SIGNING_PASSPHRASE='…' \
  bash packaging/apt/build-repo.sh apt-repo
```

`apt-repo/` then holds `dists/`, `pool/` and `porthole.asc`. The key is imported into a throwaway keyring, never into the `~/.gnupg` it came from.

## What users run (one line)

It uses `wget`, which Ubuntu, Debian and Mint have from the start (a fresh Ubuntu desktop has no `curl`).

```bash
sudo install -d -m 0755 /etc/apt/keyrings && sudo wget -qO /etc/apt/keyrings/porthole.asc https://bahrambakhtiyar.github.io/Porthole/apt/porthole.asc && echo "deb [signed-by=/etc/apt/keyrings/porthole.asc] https://bahrambakhtiyar.github.io/Porthole/apt stable main" | sudo tee /etc/apt/sources.list.d/porthole.list && sudo apt update && sudo apt install porthole
```
