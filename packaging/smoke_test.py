#!/usr/bin/env python3
"""Start a built Porthole, wait until it answers, ask it to quit, and check that it stops.

    python3 packaging/smoke_test.py [--port 8790] [--timeout 60] -- COMMAND [ARGS...]

COMMAND starts Porthole (for example /snap/bin/porthole or ./dist/Porthole-x86_64.AppImage);
`--no-browser --port <port> --no-discovery` is added to it. Exits 0 when Porthole answered GET /api/ping,
served the sharer page and its script (so a build that left out web/ fails), and stopped after
POST /api/quit. Python standard library only, so it runs wherever Porthole does.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request

QUIT_SECONDS = 15
# Bundled files that must load: (path, text the answer must contain).
PAGES = (("/share", b"Start sharing"), ("/static/share.js", b"import"), ("/static/style.css", b"{"))


def run(command: list[str], port: int, timeout: float) -> tuple[bool, str]:
    base = f"http://127.0.0.1:{port}"
    output: list[str] = []
    proc = subprocess.Popen(
        [*command, "--no-browser", "--port", str(port), "--no-discovery"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    reader = threading.Thread(target=lambda: output.extend(proc.stdout), daemon=True)
    reader.start()
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy

    def fail(message: str) -> tuple[bool, str]:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(5)
        return False, f"{message}\n--- Porthole's output ---\n{''.join(output)}"

    info = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return fail(f"Porthole stopped (exit code {proc.returncode}) before it answered.")
        try:
            with direct.open(f"{base}/api/ping", timeout=2) as resp:
                info = json.loads(resp.read())
            break
        except (OSError, ValueError):
            time.sleep(0.5)
    if info is None:
        return fail(f"Porthole never answered on {base}/api/ping within {timeout:g} s.")
    if not isinstance(info, dict) or info.get("app") != "porthole":
        return fail(f"Something else answered on port {port}: {info!r}")
    for path, wanted in PAGES:
        try:
            with direct.open(f"{base}{path}", timeout=5) as resp:
                status, body = resp.status, resp.read()
        except OSError as err:  # urllib's HTTPError (a 404, say) is an OSError too
            return fail(f"GET {path} failed: {err}")
        if status != 200 or wanted not in body:
            return fail(f"GET {path} answered {status} without the expected content.")

    quit_request = urllib.request.Request(f"{base}/api/quit", data=b"", method="POST", headers={"Origin": base})
    try:
        with direct.open(quit_request, timeout=5) as resp:
            status = resp.status
    except OSError as err:
        return fail(f"POST /api/quit failed: {err}")
    if status != 204:
        return fail(f"POST /api/quit answered {status}, not 204.")
    try:
        code = proc.wait(timeout=QUIT_SECONDS)
    except subprocess.TimeoutExpired:
        return fail(f"Porthole was still running {QUIT_SECONDS} s after /api/quit.")
    reader.join(5)
    if code != 0:
        return fail(f"Porthole stopped with exit code {code}.")
    return True, "".join(output)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser(description="Smoke-test a built Porthole.")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for the first answer")
    args = parser.parse_args(argv[:split])
    command = argv[split + 1 :]
    if not command:
        parser.error("give the command that starts Porthole after --")
    ok, details = run(command, args.port, args.timeout)
    if not ok:
        print(details, file=sys.stderr)
        return 1
    print(details, end="" if details.endswith("\n") else "\n")
    print("Porthole answered and quit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
