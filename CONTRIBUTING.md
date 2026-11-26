# Contributing to Porthole

Thanks for your interest in Porthole. It is small on purpose, so please read this page before opening a pull request.

[The rules](#the-rules) · [Run it](#run-it) · [Tests](#tests) · [Pull requests](#pull-requests) · [The CLA](#the-contributor-licence-agreement) · [Security](#security) · [Conduct](#conduct)

## The rules

Porthole shows your screen to phones and PCs on the same Wi-Fi. Viewers install nothing; they open a link or scan a QR code. The project's rules follow from that:

- **The server uses the Python standard library only.** No packages from PyPI at runtime. Packaging tools (snapcraft, python-appimage and so on) are fine, because they're only used to build.
- **The pages are plain HTML, CSS and JavaScript modules.** No frameworks and no build step.
- **Viewers never install anything.**
- **Local network only.** Sharing over the internet would need a relay server, which is outside the free app.
- **Plain words in the interface.** Short labels, no technical terms, nothing a regular person has to look up.

## Run it

```bash
python3 start.py        # py start.py on Windows
```

To try it without sharing your real screen, open `http://127.0.0.1:8765/share?test=1`: it shares a moving test picture instead. How the parts fit together, the files and every option are in [Using Porthole in detail](docs/design.md#using-porthole-in-detail).

## Tests

Every change comes with tests, and the full suite must pass:

```bash
python3 -m unittest discover -s tests
```

- **Write the test first,** watch it fail, then make it pass.
- **The browser-code tests** need Node 22 or newer; they're skipped without it.
- **The headless-Chrome tests** need Chrome or Chromium. Set `REQUIRE_BROWSER=1` to make a missing browser a failure.
- **Real phones and screens** can't be tested automatically: [docs/manual-tests.md](docs/manual-tests.md) lists the checks a person does.

## Pull requests

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
2. **One change per pull request,** with a clear title in the imperative mood ("Fix…", "Add…").
3. **Agree to the CLA** on your first pull request (below).

## The contributor licence agreement

Before your first contribution can be merged, you agree to the [contributor licence agreement](CLA.md) once:

1. Open your pull request.
2. The CLA assistant ([cla-assistant.io](https://cla-assistant.io)) comments with a link.
3. Open the link, read the agreement and sign in with GitHub to agree. The "license/cla" check on your pull request turns green, and stays signed for your later pull requests.

If the check doesn't update, comment `recheck` on the pull request.

You keep the copyright in your work; the agreement lets the author also offer Porthole under other terms. The CLA explains why.

## Security

Don't open a public issue for a security problem. Report it privately, as [SECURITY.md](SECURITY.md) explains.

## Conduct

Be kind and assume good intent. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
