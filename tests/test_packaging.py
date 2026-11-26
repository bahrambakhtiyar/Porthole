"""Linux packaging: desktop files, AppStream metainfo, the .deb, the snap, the AppImage recipe, the build version
and CI."""

import configparser
import hashlib
import importlib.util
import json
import ntpath
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from tests.private_docs import GOING_PUBLIC, read_private_doc

ROOT = Path(__file__).resolve().parent.parent
LINUX = ROOT / "packaging" / "linux"
APP_ID = "io.github.bahrambakhtiyar.Porthole"
METAINFO = LINUX / f"{APP_ID}.metainfo.xml"
NOREPLY = "bahrambakhtiyar@users.noreply.github.com"


def find_bash(platform=None, which=shutil.which, environ=None, isfile=os.path.isfile) -> str | None:
    """bash, if there is a usable one. On Windows that is Git's bash: System32\\bash.exe and the Store's
    WindowsApps\\bash.exe only start WSL, which fails without a Linux installed."""
    platform = platform or sys.platform
    found = which("bash")
    if platform != "win32":
        return found
    env = {key.upper(): value for key, value in (os.environ if environ is None else environ).items()}
    system = ntpath.normcase(env.get("SYSTEMROOT") or r"C:\Windows")
    candidates = [found] + [
        ntpath.join(folder, "Git", "bin", "bash.exe")
        for folder in (env.get("PROGRAMFILES") or r"C:\Program Files", env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA"))
        if folder
    ]
    for candidate in candidates:
        if not candidate or not isfile(candidate):
            continue
        normal = ntpath.normcase(candidate)
        if normal.startswith(system + "\\") or "\\windowsapps\\" in normal:
            continue  # the WSL launcher
        return candidate
    return None


BASH = find_bash()

try:
    import yaml  # PyYAML: only for checking the YAML files, never needed by the app
except ImportError:
    yaml = None


def desktop_entry(path: Path) -> dict:
    parser = configparser.RawConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # keys are case-sensitive
    parser.read_string(path.read_text(encoding="utf-8"))
    return dict(parser["Desktop Entry"])


def validate_desktop(test: unittest.TestCase, path: Path) -> None:
    tool = shutil.which("desktop-file-validate")
    if not tool:
        return
    result = subprocess.run([tool, str(path)], capture_output=True, text=True, timeout=30)
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    test.assertNotIn("error", (result.stdout + result.stderr).lower())


def shell_scripts() -> list[Path]:
    return sorted(p for p in (ROOT / "packaging").rglob("*.sh")) + [ROOT / "snap" / "local" / "porthole"]


FAKE_PYTHON_ON_PATH = """#!{python}
import json, os, sys
json.dump({{"args": sys.argv[1:], "command": os.environ.get("PORTHOLE_COMMAND")}}, open(os.environ["FAKE_RECORD"], "w"))
"""


def run_wrapper(test: unittest.TestCase, wrapper: Path, env: dict, *args: str, interpreter: str | None = None) -> dict:
    """Run a start script with a fake Python; what it was given and saw. The fake is `python3` first on PATH, or,
    for a script that names its interpreter (`interpreter`, an absolute path), put in that path's place in a copy of
    the script, while the `python3` on PATH fails."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / ("fake-python" if interpreter else "python3")
        fake.write_text(FAKE_PYTHON_ON_PATH.format(python=sys.executable))
        fake.chmod(0o755)
        script = wrapper
        if interpreter:
            script = Path(tmp) / "wrapper"
            script.write_text(wrapper.read_text().replace(interpreter, str(fake)))
            wrong = Path(tmp) / "python3"  # an older Python first on PATH (pyenv, conda)
            wrong.write_text("#!/bin/sh\necho 'the python3 on PATH ran' >&2\nexit 1\n")
            wrong.chmod(0o755)
        record = Path(tmp) / "record.json"
        full_env = {**os.environ, **env, "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_RECORD": str(record)}
        full_env.pop("PORTHOLE_COMMAND", None)
        result = subprocess.run(["/bin/sh", str(script), *args], env=full_env, capture_output=True, text=True, timeout=60)
        test.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(record.read_text())


class FindBashTest(unittest.TestCase):
    GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
    ENV = {"SystemRoot": r"C:\WINDOWS", "ProgramFiles": r"C:\Program Files"}

    def find(self, on_path, present, environ=None):
        return find_bash(
            platform="win32", which=lambda name: on_path, environ=self.ENV if environ is None else environ,
            isfile=lambda path: path in present,
        )

    def test_linux_uses_the_path(self):
        self.assertEqual(find_bash(platform="linux", which=lambda name: "/usr/bin/bash"), "/usr/bin/bash")
        self.assertIsNone(find_bash(platform="linux", which=lambda name: None))

    def test_windows_skips_the_wsl_launcher(self):
        wsl = r"C:\WINDOWS\system32\bash.exe"
        self.assertEqual(self.find(wsl, [wsl, self.GIT_BASH]), self.GIT_BASH)
        self.assertIsNone(self.find(wsl, [wsl]))
        store = r"C:\Users\runner\AppData\Local\Microsoft\WindowsApps\bash.exe"
        self.assertIsNone(self.find(store, [store]))

    def test_windows_git_bash_on_the_path_or_in_its_folder(self):
        self.assertEqual(self.find(self.GIT_BASH, [self.GIT_BASH]), self.GIT_BASH)
        self.assertEqual(self.find(None, [self.GIT_BASH]), self.GIT_BASH)
        self.assertEqual(self.find(None, [self.GIT_BASH], environ={}), self.GIT_BASH)  # the usual folder
        self.assertIsNone(self.find(None, []))


class GitAttributesTest(unittest.TestCase):
    """Git on Windows turns LF into CRLF on checkout unless told otherwise."""

    def rules(self) -> dict:
        found = {}
        for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and not parts[0].startswith("#"):
                found[parts[0]] = parts[1:]
        return found

    def test_text_files_keep_lf_everywhere(self):
        self.assertEqual(self.rules().get("*"), ["text=auto", "eol=lf"])

    def test_images_are_binary(self):
        for pattern in ("*.png", "*.ico", "*.gif"):  # *.gif: the README's demo (docs/screenshots/)
            self.assertIn("binary", self.rules().get(pattern, []), pattern)


BUILDINFO = ROOT / "packaging" / "buildinfo.py"


def load_buildinfo():
    spec = importlib.util.spec_from_file_location("porthole_buildinfo", BUILDINFO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


buildinfo = load_buildinfo()


class BuildVersionTest(unittest.TestCase):
    """packaging/buildinfo.py: the version CI gives a tagged build (linux.yml uses it for the snap)."""

    def version(self, env=None, tag=None):
        return buildinfo.porthole_version(environ=env or {}, tag=lambda: tag)

    def test_from_the_environment(self):
        self.assertEqual(self.version({"PORTHOLE_VERSION": "1.2.3"}), "1.2.3")
        self.assertEqual(self.version({"PORTHOLE_VERSION": "v1.2.3"}), "1.2.3")

    def test_from_the_git_tag(self):
        self.assertEqual(self.version(tag="v0.3.1"), "0.3.1")

    def test_default(self):
        self.assertEqual(self.version(), "0.1.0")
        self.assertEqual(self.version({"PORTHOLE_VERSION": ""}), "0.1.0")

    def test_the_environment_wins_over_the_tag(self):
        self.assertEqual(self.version({"PORTHOLE_VERSION": "2.0.0"}, tag="v0.3.1"), "2.0.0")

    def test_unusable_versions_are_refused(self):
        # Three numbers of at most 65535, the same for every system (the Windows and Mac builds need that).
        for bad in ("1.2", "1", "1.2.3.4", "1.2.3.4.5", "1.2.3-rc1", "1.2.3; del x", "70000.0.0", "a.b.c", "1..3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.version({"PORTHOLE_VERSION": bad})

    def test_git_tag_reader_never_raises(self):
        with mock.patch.object(buildinfo.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(buildinfo.git_tag())

    def test_prints_the_version(self):
        env = {**os.environ, "PORTHOLE_VERSION": "4.5.6"}
        result = subprocess.run(
            [sys.executable, str(BUILDINFO)], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertEqual((result.returncode, result.stdout), (0, "4.5.6\n"), result.stderr)

    def test_a_bad_version_stops_with_a_message(self):
        env = {**os.environ, "PORTHOLE_VERSION": "1.2.3-beta"}
        result = subprocess.run(
            [sys.executable, str(BUILDINFO)], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Not a usable version", result.stderr)

    def test_the_newest_release_in_the_metainfo(self):
        # AppStream lists releases newest first; software centres show the first as the version and its notes.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.metainfo.xml"
            path.write_text(
                '<component><releases><release version="0.2.0" date="2027-01-02"/>'
                '<release version="0.1.0" date="2026-11-22"/></releases></component>',
                encoding="utf-8",
            )
            self.assertEqual(buildinfo.newest_release(path), "0.2.0")
        self.assertIsNotNone(buildinfo.newest_release())

    def check_metainfo(self, version):
        env = {**os.environ, "PORTHOLE_VERSION": version}
        return subprocess.run(
            [sys.executable, str(BUILDINFO), "--check-metainfo"], capture_output=True, text=True, env=env, timeout=60
        )

    def test_a_tagged_build_needs_its_release_in_the_metainfo(self):
        # Otherwise the .deb and the AppImage would tell software centres the newest release is an older one.
        newest = buildinfo.newest_release()
        self.assertEqual(self.check_metainfo(newest).returncode, 0)
        result = self.check_metainfo("65535.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"says the newest release is {newest}", result.stderr)
        self.assertIn('<release version="65535.0.0"', result.stderr)



class DesktopFileTest(unittest.TestCase):
    def test_fields(self):
        entry = desktop_entry(LINUX / "porthole.desktop")
        expected = {
            "Type": "Application",
            "Name": "Porthole",
            "Exec": "porthole --launcher",
            "Icon": APP_ID,
            "Terminal": "false",
            "Categories": "Network;RemoteAccess;",
            "StartupWMClass": "Porthole",
        }
        for key, value in expected.items():
            self.assertEqual(entry.get(key), value, key)

    def test_icon_name_no_icon_theme_takes_over(self):
        # Icon themes are searched before hicolor, and some ship an icon called "porthole" for an old package
        # manager of that name (WhiteSur maps it to its software-installer icon), so the plain name showed a
        # stranger's icon in the menu. The app ID is unique, and it is what Flathub and AppStream expect.
        self.assertEqual(desktop_entry(LINUX / "porthole.desktop")["Icon"], APP_ID)
        self.assertNotIn("Icon=porthole\n", (LINUX / "porthole.desktop").read_text(encoding="utf-8"))

    def test_window_class_matches_the_app_window(self):
        from porthole import appwindow

        self.assertEqual(desktop_entry(LINUX / "porthole.desktop")["StartupWMClass"], appwindow.WM_CLASS)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate not installed")
    def test_passes_desktop_file_validate(self):
        validate_desktop(self, LINUX / "porthole.desktop")


class MetainfoTest(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(METAINFO).getroot()

    def text(self, path):
        node = self.root.find(path)
        self.assertIsNotNone(node, path)
        return (node.text or "").strip()

    def test_fields(self):
        self.assertEqual(self.root.tag, "component")
        self.assertEqual(self.root.get("type"), "desktop-application")
        self.assertEqual(self.text("id"), APP_ID)
        self.assertEqual(self.text("name"), "Porthole")
        self.assertEqual(self.text("summary"), "Share your screen with phones on the same Wi-Fi")
        self.assertEqual(self.text("metadata_license"), "CC0-1.0")
        self.assertEqual(self.text("project_license"), "AGPL-3.0-only")
        self.assertEqual(self.text("developer/name"), "Bahram Bakhtiyar")
        self.assertEqual(self.text("url[@type='homepage']"), "https://github.com/bahrambakhtiyar/Porthole")
        self.assertEqual(self.text("launchable[@type='desktop-id']"), "porthole.desktop")
        self.assertIsNotNone(self.root.find("content_rating[@type='oars-1.1']"))
        release = self.root.find("releases/release")
        self.assertEqual((release.get("version"), release.get("date")), ("0.1.0", "2026-11-26"))
        self.assertTrue(self.root.findall("description/p"))

    def test_launchable_is_the_installed_desktop_file(self):
        self.assertTrue((LINUX / self.text("launchable[@type='desktop-id']")).is_file())

    def test_no_email_addresses(self):
        self.assertNotIn("@", METAINFO.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("appstreamcli"), "appstreamcli not installed")
    def test_passes_appstreamcli(self):
        result = subprocess.run(
            ["appstreamcli", "validate", "--no-net", str(METAINFO)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class IconFilesTest(unittest.TestCase):
    def test_snap_icon_is_the_app_icon(self):
        self.assertEqual((ROOT / "snap" / "gui" / "porthole.png").read_bytes(), (ROOT / "assets" / "icon.png").read_bytes())


class SnapTest(unittest.TestCase):
    YAML = ROOT / "snap" / "snapcraft.yaml"

    def test_desktop_file(self):
        path = ROOT / "snap" / "gui" / "porthole.desktop"
        entry = desktop_entry(path)
        self.assertEqual(entry["Exec"], "porthole --launcher")
        self.assertEqual(entry["Icon"], "${SNAP}/meta/gui/porthole.png")
        self.assertEqual(entry["Name"], "Porthole")
        # The snap opens the default browser, not a Porthole window, so nothing to group under the icon.
        self.assertNotIn("StartupWMClass", entry)
        self.assertEqual(
            {k: v for k, v in entry.items() if k not in ("Icon", "StartupWMClass")},
            {k: v for k, v in desktop_entry(LINUX / "porthole.desktop").items() if k not in ("Icon", "StartupWMClass")},
        )

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate not installed")
    def test_desktop_file_is_valid_apart_from_the_snap_icon_path(self):
        # desktop-file-validate doesn't know ${SNAP}; snapd fills it in when the snap is installed.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "porthole.desktop"
            text = (ROOT / "snap" / "gui" / "porthole.desktop").read_text(encoding="utf-8")
            copy.write_text(text.replace("${SNAP}/meta/gui/porthole.png", "/snap/porthole/current/meta/gui/porthole.png"))
            validate_desktop(self, copy)

    def test_wrapper_runs_start_py_with_python3(self):
        text = (ROOT / "snap" / "local" / "porthole").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        # -s -E: the user's own Python packages and PYTHON* settings stay out, as in the AppImage.
        self.assertIn('exec python3 -s -E "$SNAP/start.py" "$@"', text)

    @unittest.skipUnless(os.name != "nt" and os.path.exists("/bin/sh"), "needs a POSIX shell")
    def test_wrapper_tells_porthole_it_is_started_with_porthole(self):
        # For hints like "Try: porthole --port 8766" (the snap's command), not "python3 start.py".
        result = run_wrapper(self, ROOT / "snap" / "local" / "porthole", {"SNAP": "/snap/porthole/x1"}, "--port", "9000")
        self.assertEqual(result, {"args": ["-s", "-E", "/snap/porthole/x1/start.py", "--port", "9000"], "command": "porthole"})

    @unittest.skipUnless(os.name != "nt" and os.path.exists("/bin/sh"), "needs a POSIX shell")
    def test_the_users_python_settings_stay_out(self):
        # PYTHONSAFEPATH would keep start.py's folder off the path (no module named porthole), and a PYTHONPATH folder
        # with a module named like one Porthole imports would replace it.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "secrets.py").write_text("raise SystemExit('the shadowing secrets.py ran')\n")
            env = {**os.environ, "SNAP": str(ROOT), "PYTHONSAFEPATH": "1", "PYTHONPATH": tmp}
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "snap" / "local" / "porthole"), "--help"],
                env=env, capture_output=True, text=True, timeout=60,
            )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--browser-tab", result.stdout)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_snapcraft_yaml(self):
        data = yaml.safe_load(self.YAML.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "porthole")
        self.assertEqual(data["base"], "core24")
        self.assertEqual(data["version"], "0.1.0")  # a string, not a number
        self.assertEqual(data["grade"], "stable")  # the Snap Store takes grade devel only on edge and beta
        self.assertEqual(data["confinement"], "strict")
        self.assertTrue(data["summary"] and len(data["summary"]) <= 78)  # the Snap Store's limit
        self.assertTrue(data["description"].strip())
        self.assertIn("default browser", data["description"])  # the snap opens Porthole there, not in its own window
        app = data["apps"]["porthole"]
        self.assertEqual(app["command"], "bin/porthole")
        self.assertEqual(sorted(app["plugs"]), ["desktop", "home", "network", "network-bind"])
        parts = data["parts"]
        staged = [pkg for part in parts.values() for pkg in part.get("stage-packages", [])]
        self.assertEqual(sorted(staged), ["libnotify-bin", "python3"])
        app_part = next(part for part in parts.values() if part.get("plugin") == "dump")
        self.assertEqual(
            app_part.get("organize"),
            {"snap/local/porthole": "bin/porthole", "LICENSE": "usr/share/doc/porthole/LICENSE"},
        )
        for wanted in ("porthole", "web", "start.py", "bin/porthole", "usr/share/doc/porthole/LICENSE"):
            self.assertIn(wanted, app_part["stage"])
        self.assertNotRegex(self.YAML.read_text(encoding="utf-8"), r"[\w.+-]+@[\w-]+\.\w+")  # no email addresses

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_store_links(self):
        # snapcraft 8 sends these to the Snap Store listing with each upload, as plain URLs for core24 snaps. Contact
        # is the issues page, so no email address is published.
        data = yaml.safe_load(self.YAML.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: data.get(key) for key in ("website", "source-code", "issues", "contact", "donation")},
            {
                "website": "https://github.com/bahrambakhtiyar/Porthole",
                "source-code": "https://github.com/bahrambakhtiyar/Porthole",
                "issues": "https://github.com/bahrambakhtiyar/Porthole/issues",
                "contact": "https://github.com/bahrambakhtiyar/Porthole/issues",
                "donation": "https://kreosus.com/bahram_bakhtiyar",
            },
        )
        self.assertEqual(data["donation"], (ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8").split('"')[1])

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_store_icon(self):
        # snapcraft copies the `icon` file into the snap as meta/gui/icon.png, the only icon the Snap Store reads from an
        # upload; without the key the store page shows a placeholder. The store takes a square icon under 256 KB.
        data = yaml.safe_load(self.YAML.read_text(encoding="utf-8"))
        self.assertEqual(data["icon"], "snap/gui/porthole.png")
        icon = (ROOT / data["icon"]).read_bytes()
        self.assertEqual(icon, (ROOT / "assets" / "icon.png").read_bytes())  # the app icon, the ring logo
        self.assertEqual(png_size(icon), (256, 256))
        self.assertLess(len(icon), 256 * 1024)

    def test_snapcraft_yaml_has_no_tabs(self):
        self.assertNotIn("\t", self.YAML.read_text(encoding="utf-8"))

    def test_going_public_no_longer_asks_to_set_the_grade(self):
        text = read_private_doc(self, GOING_PUBLIC)
        step = next(line for line in text.splitlines() if line.startswith("3. **Snap:**"))
        self.assertIn("grade: stable", step)
        self.assertNotIn("first set", step)
        self.assertNotIn("`devel` now", step)


SNAPSTORE = ROOT / "packaging" / "snapstore"
STORE_SCREENSHOTS = ("screenshot-1.png", "screenshot-2.png", "screenshot-3.png", "screenshot-4.png")
STORE_IMAGES = ("banner.png", *STORE_SCREENSHOTS)
STORE_MAX_BYTES = 2_000_000  # the Snap Store's limit for the banner and each screenshot
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The Snap Store's categories, as its dashboard lists them.
STORE_CATEGORIES = {
    "Art and Design", "Books and Reference", "Development", "Devices and IoT", "Education", "Entertainment", "Finance",
    "Games", "Health and Fitness", "Music and Audio", "News and Weather", "Personalisation", "Photo and Video",
    "Productivity", "Science", "Security", "Server and Cloud", "Social", "Utilities",
}


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header."""
    assert data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR", data[:16]
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class GitHubSocialPreviewTest(unittest.TestCase):
    """The picture GitHub shows when the repository's link is shared (Settings > General > Social preview): 2:1,
    1280 x 640, a PNG under 1 MB, made by tools/store_images.mjs in the banner's design."""

    def test_size_and_file(self):
        path = ROOT / "packaging" / "github" / "social-preview.png"
        data = path.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")), (1280, 640))
        self.assertLess(len(data), 1_000_000)

    def test_the_tool_makes_it(self):
        tool = (ROOT / "tools" / "store_images.mjs").read_text(encoding="utf-8")
        self.assertIn("social-preview", tool)
        self.assertIn("--social-only", tool)


class SnapStoreListingTest(unittest.TestCase):
    """packaging/snapstore/: the banner, the screenshots and listing.md, what goes where on the store's dashboard.
    tools/store_images.mjs makes the pictures; it needs Chrome and takes a while, so it isn't run here."""

    LISTING = SNAPSTORE / "listing.md"
    TOOL = ROOT / "tools" / "store_images.mjs"

    def listing(self) -> str:
        return self.LISTING.read_text(encoding="utf-8")

    def pasted(self, label: str) -> str:
        """The text in listing.md's code block under **label**."""
        found = re.search(rf"\*\*{label}\*\*[^\n]*\n\n```text\n(.*?)\n```", self.listing(), re.S)
        self.assertIsNotNone(found, label)
        return found.group(1)

    def test_the_folder_holds_the_five_pictures(self):
        # Up to five pictures in all, and the banner may count toward them: the banner and four screenshots.
        self.assertEqual(sorted(p.name for p in SNAPSTORE.iterdir() if p.suffix != ".md"), sorted(STORE_IMAGES))
        self.assertLessEqual(len(STORE_IMAGES), 5)

    def test_png_files_under_2_mb(self):
        for name in STORE_IMAGES:
            with self.subTest(name=name):
                data = (SNAPSTORE / name).read_bytes()
                self.assertEqual(data[:8], PNG_SIGNATURE)
                self.assertLess(len(data), STORE_MAX_BYTES)

    def test_the_banner_is_3_to_1(self):
        # Exactly 3:1, from 1920 × 640 to 4320 × 1440, named banner.png.
        width, height = png_size((SNAPSTORE / "banner.png").read_bytes())
        self.assertEqual(width, 3 * height)
        self.assertTrue(1920 <= width <= 4320, width)
        self.assertEqual((width, height), (1920, 640))  # as tools/store_images.mjs makes it

    def test_the_screenshots_fit_the_store(self):
        # At least 480 × 480, between 1:2 and 2:1.
        for name in STORE_SCREENSHOTS:
            with self.subTest(name=name):
                width, height = png_size((SNAPSTORE / name).read_bytes())
                self.assertGreaterEqual(min(width, height), 480)
                self.assertTrue(0.5 <= width / height <= 2, (width, height))
                self.assertEqual((width, height), (1920, 1080))  # as tools/store_images.mjs makes them

    def test_png_size(self):
        header = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + (1920).to_bytes(4, "big") + (640).to_bytes(4, "big")
        self.assertEqual(png_size(header + b"\x08\x06\x00\x00\x00"), (1920, 640))

    def test_the_listing_names_each_picture_in_order(self):
        text = self.listing()
        places = [text.index(f"`{name}`") for name in STORE_IMAGES]
        self.assertEqual(places, sorted(places))
        rows = [line for line in text.splitlines() if re.match(r"\s*\| \d \| `", line)]
        self.assertEqual(len(rows), len(STORE_IMAGES))
        for row, name in zip(rows, STORE_IMAGES):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", row)
                alt = row.rstrip(" |").rsplit("|", 1)[1].strip()  # the last column: the alt text
                self.assertGreater(len(alt), 40, row)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_the_listing_matches_snapcraft_yaml(self):
        # The title, summary and description go to the store with each upload: listing.md shows the same text.
        data = yaml.safe_load((ROOT / "snap" / "snapcraft.yaml").read_text(encoding="utf-8"))
        self.assertEqual(self.pasted("Title"), data["title"])
        self.assertEqual(self.pasted("Summary"), data["summary"])
        self.assertEqual(self.pasted("Description"), data["description"].rstrip("\n"))
        stated = re.search(r"\*\*Summary\*\* \((\d+) characters", self.listing())
        self.assertEqual(int(stated.group(1)), len(data["summary"]))
        self.assertLessEqual(len(data["summary"]), 78)
        for link in ("website", "source-code", "issues", "contact", "donation"):
            with self.subTest(link=link):
                self.assertIn(f"| {data[link]} |", self.listing())

    def test_the_description_is_short_and_plain(self):
        # The store shows simple Markdown: short paragraphs and "- " lists, nothing else.
        text = self.pasted("Description")
        self.assertLessEqual(len(text.split()), 250)
        for line in text.splitlines():
            with self.subTest(line=line[:40]):
                self.assertNotRegex(line, r"[*_`#\[]|^\s+\S")  # no emphasis, code, headings, links or indented lines

    def test_two_real_categories(self):
        text = self.listing()
        primary = re.search(r"Primary: \*\*([^*]+)\*\*", text).group(1)
        secondary = re.search(r"Secondary: \*\*([^*]+)\*\*", text).group(1)
        self.assertIn(primary, STORE_CATEGORIES)
        self.assertIn(secondary, STORE_CATEGORIES)
        self.assertNotEqual(primary, secondary)

    def test_no_email_addresses(self):
        self.assertNotRegex(self.listing(), r"[\w.+-]+@[\w-]+\.\w+")

    def test_the_tool_uses_the_example_address_and_a_port_of_its_own(self):
        text = self.TOOL.read_text(encoding="utf-8")
        for words in ("'192.168.1.20'", "'--no-browser'", "'--host'", "'--record-dir'", "/api/quit", "porthole-settings",
                      "easy: false", "port > 8800", "port = 8801", "getDisplayMedia", "'packaging', 'snapstore'",
                      "MAX_BYTES = 2_000_000", "{ width: 1920, height: 1080 }", "{ width: 1920, height: 640 }"):
            self.assertIn(words, text)
        for name in STORE_IMAGES:
            self.assertIn(name.removesuffix(".png"), text)
        # The sharer page as in normal use, with only the screen picker replaced: test mode would put its note and
        # its test picture into the store's pictures.
        self.assertNotIn("?test=1", text)

    @unittest.skipUnless(shutil.which("node"), "node not installed (only needed for the browser-code tests)")
    def test_the_tool_parses(self):
        result = subprocess.run([shutil.which("node"), "--check", str(self.TOOL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_design_md_names_the_tool_and_the_listing(self):
        text = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        start = text.index("\n## How it works\n")
        section = text[start : text.index("\n## ", start + 1)]
        self.assertIn("`tools/store_images.mjs`", section)
        self.assertIn("`node tools/store_images.mjs`", section)
        start = text.index("\n## Releases\n")
        releases = text[start : text.index("\n# ", start + 1)]
        self.assertIn("### Snap Store listing", releases)
        self.assertIn("packaging/snapstore/listing.md", releases)


class AppImageRecipeTest(unittest.TestCase):
    RECIPE = ROOT / "packaging" / "appimage"

    def test_entrypoint_runs_start_py_with_the_bundled_python(self):
        text = (self.RECIPE / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('"{{ python-executable }}"', text)
        self.assertIn('"${APPDIR}/start.py" "$@"', text)
        self.assertNotIn(" -I ", text)  # -I would leave start.py's folder off the import path

    def test_requirements_list_nothing(self):
        lines = (self.RECIPE / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in lines if line.strip() and not line.startswith("#")], [])

    def test_build_sh_trims_before_it_packs(self):
        # python-appimage builds and packs in one go, so build.sh unpacks its AppImage, trims it and packs it again
        # with the appimagetool python-appimage uses.
        text = (self.RECIPE / "build.sh").read_text(encoding="utf-8")
        trim_at = text.index("python3 packaging/appimage/trim.py")
        self.assertLess(text.index("--appimage-extract"), trim_at)
        self.assertLess(trim_at, text.index("--no-appstream"))
        self.assertIn("python-appimage which appimagetool", text)
        self.assertIn("python-appimage install appimagetool", text)
        self.assertIn('"$app/THIRD-PARTY-NOTICES.txt"', text)  # bundled next to AppRun with -x, like LICENSE


FAKE_BUNDLED_PYTHON = """#!{python}
import json, os, sys
json.dump(sys.argv[1:], open(os.environ["FAKE_RECORD"], "w"))
"""


@unittest.skipUnless(BASH and hasattr(os, "openpty"), "needs bash and a terminal device (Linux and macOS)")
class AppImageEntrypointTest(unittest.TestCase):
    """The AppImage started without a terminal (a double-click in the file manager) passes --launcher,
    so start-up problems show on screen instead of in output nobody sees."""

    MOUNT = "/tmp/.mount_PorthoAb12"

    def run_entrypoint(self, terminal, *args):
        """terminal: True (all three on a terminal), False (none, as from a file manager) or "piped" (typed in a
        terminal with the output piped or redirected)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        python = tmp / "python3.12"
        python.write_text(FAKE_BUNDLED_PYTHON.format(python=sys.executable))
        python.chmod(0o755)
        apprun = tmp / "AppRun"
        text = (ROOT / "packaging" / "appimage" / "entrypoint.sh").read_text(encoding="utf-8")
        apprun.write_text(text.replace("{{ python-executable }}", str(python)))  # what python-appimage does
        record = tmp / "args.json"
        env = {**os.environ, "APPDIR": self.MOUNT, "FAKE_RECORD": str(record)}
        command = [BASH, str(apprun), *args]
        if terminal:
            main, child = os.openpty()
            out = subprocess.PIPE if terminal == "piped" else child
            try:
                result = subprocess.run(command, stdin=child, stdout=out, stderr=child, env=env, timeout=60)
            finally:
                os.close(child)
                os.close(main)
        else:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0)
        return json.loads(record.read_text())

    def test_no_terminal_adds_launcher(self):
        self.assertEqual(
            self.run_entrypoint(False, "--port", "9000"),
            ["-s", "-E", f"{self.MOUNT}/start.py", "--launcher", "--port", "9000"],
        )

    def test_a_terminal_passes_the_arguments_as_they_are(self):
        self.assertEqual(
            self.run_entrypoint(True, "--port", "9000"), ["-s", "-E", f"{self.MOUNT}/start.py", "--port", "9000"]
        )

    def test_output_piped_from_a_terminal_isnt_a_launcher(self):
        # `--help | less` or `> help.txt`: typed at a terminal, so the help goes to the pipe, not a notification.
        self.assertEqual(self.run_entrypoint("piped", "--help"), ["-s", "-E", f"{self.MOUNT}/start.py", "--help"])

    def test_a_menu_entry_that_already_passes_launcher_still_works(self):
        args = self.run_entrypoint(False, "--launcher")
        self.assertEqual(args[:3], ["-s", "-E", f"{self.MOUNT}/start.py"])
        self.assertEqual(set(args[3:]), {"--launcher"})


FAKE_PYTHON_APPIMAGE = """#!{python}
# Stands in for python-appimage.
# `build app`: records what build.sh gave it, then makes an "AppImage" that unpacks (--appimage-extract) a copy of
# the AppDir in $FAKE_BASE_APPDIR with the -x files added, its folders 0700 as the AppImage runtime makes them.
# `which appimagetool`: prints $FAKE_APPIMAGETOOL, once installed when FAKE_WHICH is "after-install", never when
# it is "never". `install appimagetool`: works when FAKE_INSTALL is "works".
import hashlib, json, os, pathlib, shutil, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
if args[:2] == ["which", "appimagetool"]:
    which = os.environ.get("FAKE_WHICH", "yes")
    if which == "yes" or (which == "after-install" and state.exists()):
        print(os.environ["FAKE_APPIMAGETOOL"])
    sys.exit(0)
if args[:2] == ["install", "appimagetool"]:
    if os.environ.get("FAKE_INSTALL") != "works":
        sys.exit("can't download appimagetool")
    state.write_text("installed")
    sys.exit(0)
assert args[:2] == ["build", "app"], args
recipe = pathlib.Path(args[args.index("-p") + 2])
extra = args[args.index("-x") + 1 :]
base = args[args.index("--base-image") + 1] if "--base-image" in args else None
def seen(path):
    if os.path.isdir(path):
        return sorted(os.listdir(path))
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
json.dump(
    {{
        "args": args,
        "recipe": sorted(p.name for p in recipe.iterdir()),
        "entrypoint": (recipe / "entrypoint.sh").read_text(),
        "desktop": (recipe / "porthole.desktop").read_text(),
        "extra": [[os.path.basename(p), seen(p)] for p in extra],
        "base_here": bool(base) and os.access(base, os.X_OK),
    }},
    open(os.environ["FAKE_RECORD"], "w"),
)
stage = pathlib.Path(os.environ["FAKE_STAGE"])
shutil.copytree(os.environ["FAKE_BASE_APPDIR"], stage, symlinks=True)
for path in extra:
    if os.path.isdir(path):
        shutil.copytree(path, stage / os.path.basename(path))
    else:
        shutil.copy2(path, stage)
image = pathlib.Path("Porthole-x86_64.AppImage")
image.write_text(
    "#!" + sys.executable + "\\n"
    "import os, shutil, sys\\n"
    "assert sys.argv[1:] == ['--appimage-extract'], sys.argv\\n"
    "assert 'APPIMAGE_EXTRACT_AND_RUN' not in os.environ\\n"
    f"shutil.copytree({{str(stage)!r}}, 'squashfs-root', symlinks=True)\\n"
    "for folder, _, _ in os.walk('squashfs-root'):\\n"
    "    os.chmod(folder, 0o700)\\n"
)
image.chmod(0o755)
"""

FAKE_APPIMAGETOOL = """#!{python}
# Stands in for appimagetool: records what it was given and the AppDir it packs, and writes the AppImage.
import json, os, pathlib, stat, sys
args = sys.argv[1:]
appdir = pathlib.Path(args[-2])
files, modes = [], set()
for folder, dirs, names in os.walk(appdir):
    modes.add(stat.S_IMODE(os.stat(folder).st_mode))
    files += [(pathlib.Path(folder) / name).relative_to(appdir).as_posix() for name in dirs + names]
notices = appdir / "THIRD-PARTY-NOTICES.txt"
json.dump(
    {{"args": args, "arch": os.environ.get("ARCH"), "files": sorted(files), "folder_modes": sorted(modes),
      "notices": notices.read_text() if notices.is_file() else None}},
    open(os.environ["FAKE_TOOL_RECORD"], "w"),
)
pathlib.Path(args[-1]).write_bytes(b"repacked by appimagetool")
"""


@unittest.skipUnless(BASH and os.name != "nt", "needs bash; AppImages are built on Linux")
class AppImageBuildScriptTest(unittest.TestCase):
    """packaging/appimage/build.sh, run with a fake python-appimage and appimagetool (the real ones download, so
    they run in CI): it builds, unpacks, trims (packaging/appimage/trim.py) and packs the AppImage again."""

    RECIPE = ROOT / "packaging" / "appimage"

    def build(self, ok=True, change_base=None, prepare_home=None, **env):
        from tests.test_appimage_trim import fake_appdir

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        fake = fake_bin / "python-appimage"
        fake.write_text(FAKE_PYTHON_APPIMAGE.format(python=sys.executable))
        fake.chmod(0o755)
        tool = tmp / "tools" / "AppRun"
        tool.parent.mkdir()
        tool.write_text(FAKE_APPIMAGETOOL.format(python=sys.executable))
        tool.chmod(0o755)
        base = fake_appdir(tmp / "base", porthole=False)
        if change_base:
            change_base(base)
        record, tool_record, out = tmp / "record.json", tmp / "tool.json", tmp / "out"
        home = tmp / "home"  # no python-appimage cache of this PC's own
        home.mkdir()
        if prepare_home:
            prepare_home(home)
        environ = {key: value for key, value in os.environ.items() if key not in ("XDG_CACHE_HOME", "APPIMAGETOOL")}
        result = subprocess.run(
            [BASH, "packaging/appimage/build.sh"],
            cwd=ROOT,
            env={**environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(home),
                 "FAKE_RECORD": str(record), "FAKE_TOOL_RECORD": str(tool_record), "FAKE_APPIMAGETOOL": str(tool),
                 "FAKE_BASE_APPDIR": str(base), "FAKE_STAGE": str(tmp / "stage"), "FAKE_STATE": str(tmp / "installed"),
                 "OUT_DIR": str(out), **env},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.result = result
        if not ok:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(out.glob("*.AppImage")) if out.exists() else [], [])  # nothing half-made
            return result
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(record.read_text()), json.loads(tool_record.read_text()), out

    def test_recipe_and_app_files(self):
        record, _, out = self.build()
        args = record["args"]
        self.assertEqual(args[:4], ["build", "app", "-p", "3.12"])
        self.assertNotIn("--base-image", args)
        self.assertEqual(
            record["recipe"],
            sorted(["entrypoint.sh", "requirements.txt", "porthole.desktop", f"{APP_ID}.png", f"{APP_ID}.appdata.xml"]),
        )
        self.assertEqual(record["entrypoint"], (self.RECIPE / "entrypoint.sh").read_text(encoding="utf-8"))
        self.assertEqual(record["desktop"], (LINUX / "porthole.desktop").read_text(encoding="utf-8"))
        extra = dict((name, files) for name, files in record["extra"])
        self.assertEqual(set(extra), {"porthole", "web", "start.py", "LICENSE", "THIRD-PARTY-NOTICES.txt"})
        self.assertIn("app.py", extra["porthole"])
        self.assertNotIn("__pycache__", extra["porthole"])
        self.assertIn("share.html", extra["web"])
        # The licence and the third-party notices go into the AppImage next to AppRun, as they are in the repository.
        self.assertEqual(extra["LICENSE"], hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest())
        self.assertEqual(extra["THIRD-PARTY-NOTICES.txt"],
                         hashlib.sha256((self.RECIPE / "THIRD-PARTY-NOTICES.txt").read_bytes()).hexdigest())
        built = out / "Porthole-x86_64.AppImage"
        self.assertTrue(built.is_file())
        self.assertTrue(os.access(built, os.X_OK))

    def test_unpacks_trims_and_packs_again(self):
        from tests.test_appimage_trim import KEPT_LIBRARIES, REMOVED, under

        _, tool, out = self.build()
        flag, appdir, destination = tool["args"]
        self.assertEqual(flag, "--no-appstream")  # as python-appimage packs it
        self.assertEqual(Path(destination).name, "Porthole-x86_64.AppImage")
        self.assertEqual(tool["arch"], "x86_64")
        files = set(tool["files"])
        self.assertEqual({path for path in files if under(path, REMOVED)}, set())
        for name in KEPT_LIBRARIES:
            self.assertIn(f"usr/lib/{name}", files)
        for name in ("AppRun", "LICENSE", "THIRD-PARTY-NOTICES.txt", "start.py", "porthole/app.py", "web/share.html",
                     "opt/python3.12/lib/python3.12/LICENSE.txt"):
            self.assertIn(name, files)
        self.assertEqual(tool["notices"], (self.RECIPE / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8"))
        self.assertEqual(tool["folder_modes"], [0o755])  # not the 0700 the runtime unpacks them with
        # The AppImage that comes out is the one packed again, not python-appimage's.
        self.assertEqual((out / "Porthole-x86_64.AppImage").read_bytes(), b"repacked by appimagetool")
        self.assertIn("Removed", self.result.stdout)

    def test_appimagetool_is_installed_when_python_appimage_has_none(self):
        _, tool, out = self.build(FAKE_WHICH="after-install", FAKE_INSTALL="works")
        self.assertEqual(Path(tool["args"][-1]).name, "Porthole-x86_64.AppImage")
        self.assertTrue((out / "Porthole-x86_64.AppImage").is_file())

    def test_appimagetool_from_the_environment(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = Path(tmp.name) / "appimagetool"
        tool.write_text(FAKE_APPIMAGETOOL.format(python=sys.executable))
        tool.chmod(0o755)
        _, record, _ = self.build(FAKE_WHICH="never", APPIMAGETOOL=str(tool))
        self.assertEqual(record["args"][0], "--no-appstream")

    def test_which_output_with_colours_and_a_label(self):
        # Only the path counts, whatever python-appimage prints around it.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = Path(tmp.name) / "AppRun"
        tool.write_text(FAKE_APPIMAGETOOL.format(python=sys.executable))
        tool.chmod(0o755)
        _, record, _ = self.build(FAKE_APPIMAGETOOL=f"\x1b[1;32m[WHICH]\x1b[0m {tool}")
        self.assertEqual(record["args"][0], "--no-appstream")

    def test_appimagetool_found_in_python_appimages_cache(self):
        # The build itself has just put appimagetool there, unpacked, when `which` can't say where.
        def cached(home: Path):
            tool = home / ".cache" / "python-appimage" / "bin" / ".appimagetool-continuous.appdir.x86_64" / "AppRun"
            tool.parent.mkdir(parents=True)
            tool.write_text(FAKE_APPIMAGETOOL.format(python=sys.executable))
            tool.chmod(0o755)

        _, record, out = self.build(FAKE_WHICH="never", prepare_home=cached)
        self.assertEqual(record["args"][0], "--no-appstream")
        self.assertIn(".cache/python-appimage", self.result.stderr)  # says which one it took
        self.assertTrue((out / "Porthole-x86_64.AppImage").is_file())

    def test_no_appimagetool_stops_the_build_with_a_clear_message(self):
        result = self.build(ok=False, FAKE_WHICH="never")
        self.assertIn("appimagetool", result.stderr)
        self.assertIn("APPIMAGETOOL", result.stderr)

    def test_a_trim_that_refuses_stops_the_build(self):
        from tests.test_appimage_trim import EXT, PY, make_elf

        def ssl_needs_tinfo(base):
            (base / PY / "lib-dynload" / f"_ssl{EXT}").write_bytes(make_elf(["libssl.so.3", "libtinfo.so.5"]))

        result = self.build(ok=False, change_base=ssl_needs_tinfo)
        self.assertIn("libtinfo.so.5", result.stderr)

    def test_a_downloaded_python_is_passed_by_its_bare_name(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "python" / "python3.12.15-cp312-cp312-manylinux2014_x86_64.AppImage"
        base.parent.mkdir()
        base.write_bytes(b"base")
        record, _, _ = self.build(BASE_IMAGE=str(base))
        args = record["args"]
        self.assertEqual(args[args.index("--base-image") + 1], base.name)
        self.assertTrue(record["base_here"])  # copied next to where python-appimage runs, and executable
        self.assertLess(args.index("--base-image"), args.index("-x"))  # -x takes every argument after it


@unittest.skipUnless(BASH, "bash not installed")
class ShellScriptsTest(unittest.TestCase):
    def test_line_endings(self):
        # bash fails on a script with Windows line endings; .gitattributes keeps them LF in every checkout.
        for script in shell_scripts():
            self.assertNotIn(b"\r\n", script.read_bytes(), script)

    def test_syntax(self):
        scripts = shell_scripts()
        self.assertGreaterEqual(len(scripts), 4)
        for script in scripts:
            result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_strict_mode(self):
        for script in shell_scripts():
            text = script.read_text(encoding="utf-8")
            if text.startswith("#!/usr/bin/env bash"):
                self.assertIn("set -euo pipefail", text, script)

    def test_no_personal_email(self):
        for path in [*shell_scripts(), ROOT / "packaging" / "deb" / "copyright"]:
            for address in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", path.read_text(encoding="utf-8")):
                self.assertEqual(address, NOREPLY, path)


DEB = ROOT / "packaging" / "deb"
DEP5_TEXT = DEB / "dep5_text.sh"
COPYRIGHT_TEMPLATE = DEB / "copyright"
LICENSE = ROOT / "LICENSE"


def dep5_unindent(lines: list[str]) -> str:
    """The text that DEP-5 continuation lines stand for: one leading space off each, " ." an empty line."""
    out = []
    for line in lines:
        if not line.startswith(" "):
            raise ValueError(f"not a continuation line: {line!r}")
        out.append("" if line == " ." else line[1:])
    return "".join(f"{line}\n" for line in out)


def deb822_paragraphs(text: str) -> list[list[str]]:
    """The paragraphs of a deb822 file (empty lines between them), each as its lines."""
    paragraphs, current = [], []
    for line in text.split("\n"):
        if line:
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return paragraphs


class DebCopyrightTemplateTest(unittest.TestCase):
    """packaging/deb/copyright is the copyright file without the licence text, which build.sh adds from LICENSE."""

    def test_header_files_and_licence_stanzas(self):
        self.assertEqual(
            deb822_paragraphs(COPYRIGHT_TEMPLATE.read_text(encoding="utf-8")),
            [
                [
                    "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/",
                    "Upstream-Name: Porthole",
                    f"Upstream-Contact: Bahram Bakhtiyar <{NOREPLY}>",
                    "Source: https://github.com/bahrambakhtiyar/Porthole",
                ],
                ["Files: *", "Copyright: 2026 Bahram Bakhtiyar", "License: AGPL-3.0-only"],
                ["License: AGPL-3.0-only"],  # its text comes from LICENSE at build time
            ],
        )
        self.assertTrue(COPYRIGHT_TEMPLATE.read_text(encoding="utf-8").endswith("\nLicense: AGPL-3.0-only\n"))

    def test_build_sh_adds_the_full_text_from_license(self):
        text = (DEB / "build.sh").read_text(encoding="utf-8")
        self.assertIn("cat packaging/deb/copyright", text)
        self.assertIn("bash packaging/deb/dep5_text.sh LICENSE", text)
        self.assertIn('>"$share/doc/porthole/copyright"', text)
        self.assertNotIn("install -m 0644 packaging/deb/copyright", text)  # the template alone lacks the text


@unittest.skipUnless(BASH and os.name != "nt", "needs bash and sed; the .deb is built on Linux")
class Dep5TextTest(unittest.TestCase):
    """packaging/deb/dep5_text.sh: a text file as the body of a DEP-5 field."""

    def convert(self, data: bytes) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "text"
        path.write_bytes(data)
        return subprocess.run([BASH, str(DEP5_TEXT), str(path)], capture_output=True, timeout=30)

    def test_indents_every_line_and_marks_empty_ones(self):
        result = self.convert(b"Title\n\n  indented\n\t\ttabbed\n   \nlast\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Lines of only spaces would end the paragraph in deb822, so they become " ." too.
        self.assertEqual(result.stdout, b" Title\n .\n   indented\n \t\ttabbed\n .\n last\n")

    def test_the_licence_round_trips(self):
        result = self.convert(LICENSE.read_bytes())
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.decode("ascii").split("\n")
        self.assertEqual(lines[-1], "")
        body = lines[:-1]
        self.assertEqual(len(body), 661)
        for line in body:
            self.assertTrue(line.startswith(" "), repr(line))
            self.assertTrue(line == " ." or line.strip(), repr(line))
        self.assertIn(" .", body)
        self.assertEqual(dep5_unindent(body), LICENSE.read_text(encoding="ascii"))

    def test_a_line_starting_with_a_full_stop_is_refused(self):
        # " ." is an empty line in DEP-5, so ".", or ".more", can't be written as it is.
        result = self.convert(b"fine\n.oops\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"line 2", result.stderr)
        self.assertIn(b"full stop", result.stderr)

    def test_a_missing_file_is_refused(self):
        result = subprocess.run([BASH, str(DEP5_TEXT), str(ROOT / "no-such-file")], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Usage", result.stderr)


@unittest.skipUnless(BASH and shutil.which("dpkg-deb"), "bash or dpkg-deb not installed")
class DebBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "out"
        env = {**os.environ, "OUT_DIR": str(out), "PORTHOLE_VERSION": "0.1.0", "SOURCE_DATE_EPOCH": "1795392000"}
        cls.build = subprocess.run(
            [BASH, "packaging/deb/build.sh"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )
        cls.deb = out / "porthole_0.1.0_all.deb"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.assertEqual(self.build.returncode, 0, self.build.stdout + self.build.stderr)
        self.assertTrue(self.deb.is_file())

    def listing(self) -> dict:
        """Path -> (mode, owner) from `dpkg-deb -c`."""
        out = subprocess.run(["dpkg-deb", "-c", str(self.deb)], capture_output=True, text=True, check=True).stdout
        entries = {}
        for line in out.splitlines():
            fields = line.split()
            path = fields[5].removeprefix(".")
            entries[path.rstrip("/") or "/"] = (fields[0], fields[1])
        return entries

    def field(self, name: str) -> str:
        return subprocess.run(["dpkg-deb", "-f", str(self.deb), name], capture_output=True, text=True, check=True).stdout.strip()

    def test_layout(self):
        entries = self.listing()
        for path in (
            "/usr/bin/porthole",
            "/usr/share/porthole/start.py",
            "/usr/share/porthole/porthole/app.py",
            "/usr/share/porthole/porthole/appwindow.py",
            "/usr/share/porthole/porthole/sandbox.py",
            "/usr/share/porthole/web/share.html",
            "/usr/share/porthole/web/view.js",
            "/usr/share/applications/porthole.desktop",
            f"/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png",
            f"/usr/share/icons/hicolor/512x512/apps/{APP_ID}.png",
            f"/usr/share/metainfo/{APP_ID}.metainfo.xml",
            "/usr/share/doc/porthole/copyright",
            "/usr/share/doc/porthole/changelog.gz",
        ):
            self.assertIn(path, entries)
        app_files = {p.name for p in (ROOT / "porthole").glob("*.py")}
        packaged = {Path(p).name for p in entries if p.startswith("/usr/share/porthole/porthole/") and p.endswith(".py")}
        self.assertEqual(packaged, app_files)
        web_files = {p.name for p in (ROOT / "web").iterdir() if p.is_file()}
        self.assertEqual({Path(p).name for p in entries if p.startswith("/usr/share/porthole/web/")}, web_files)

    def test_no_caches_or_stray_files(self):
        for path in self.listing():
            self.assertNotIn("__pycache__", path)
            self.assertFalse(path.endswith((".pyc", ".pyo")), path)
            self.assertFalse(path.startswith(("/usr/share/porthole/tests", "/usr/share/porthole/docs")), path)

    def test_modes_and_owners(self):
        for path, (mode, owner) in self.listing().items():
            self.assertEqual(owner, "root/root", path)
            if mode.startswith("d"):
                self.assertEqual(mode, "drwxr-xr-x", path)
            elif path in ("/usr/bin/porthole", "/usr/share/porthole/start.py"):
                self.assertEqual(mode, "-rwxr-xr-x", path)
            else:
                self.assertEqual(mode, "-rw-r--r--", path)

    def test_control_fields(self):
        self.assertEqual(self.field("Package"), "porthole")
        self.assertEqual(self.field("Version"), "0.1.0")
        self.assertEqual(self.field("Architecture"), "all")
        self.assertEqual(self.field("Depends"), "python3 (>= 3.10)")
        self.assertEqual(self.field("Section"), "net")
        self.assertEqual(self.field("Priority"), "optional")
        self.assertEqual(self.field("Maintainer"), f"Bahram Bakhtiyar <{NOREPLY}>")
        self.assertEqual(self.field("Homepage"), "https://github.com/bahrambakhtiyar/Porthole")
        description = self.field("Description").splitlines()
        self.assertEqual(description[0], "Share your screen with phones on the same Wi-Fi")
        self.assertGreater(len(description), 3)
        self.assertTrue(all(len(line) <= 80 for line in description))
        self.assertTrue(int(self.field("Installed-Size")) > 0)
        info = subprocess.run(["dpkg-deb", "-I", str(self.deb)], capture_output=True, text=True, check=True).stdout
        self.assertIn("control", info)

    def test_files_inside_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["dpkg-deb", "-x", str(self.deb), tmp], check=True)
            root = Path(tmp)
            self.assertEqual(
                (root / "usr/bin/porthole").read_text(),
                "#!/bin/sh\n"
                '# PORTHOLE_COMMAND tells Porthole how it is started, for its hints ("Try: porthole --port 8766").\n'
                "export PORTHOLE_COMMAND=porthole\n"
                "# The system Python the package depends on, not whichever python3 comes first on PATH.\n"
                "# -s -E: the user's own Python packages and PYTHON* settings stay out (PYTHONSAFEPATH, PYTHONPATH).\n"
                'exec /usr/bin/python3 -s -E /usr/share/porthole/start.py "$@"\n',
            )
            self.assertEqual(
                run_wrapper(self, root / "usr/bin/porthole", {}, "--port", "9000", interpreter="/usr/bin/python3"),
                {"args": ["-s", "-E", "/usr/share/porthole/start.py", "--port", "9000"], "command": "porthole"},
            )
            self.assertTrue((root / "usr/share/porthole/start.py").read_text().startswith("#!/usr/bin/python3\n"))
            self.assertEqual(
                (root / "usr/share/applications/porthole.desktop").read_bytes(), (LINUX / "porthole.desktop").read_bytes()
            )
            self.assertEqual(
                (root / f"usr/share/icons/hicolor/512x512/apps/{APP_ID}.png").read_bytes(),
                (ROOT / "web" / "icon-512.png").read_bytes(),
            )
            # The packaged copy starts: every module it imports is there.
            result = subprocess.run(
                [sys.executable, str(root / "usr/share/porthole/start.py"), "--help"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--browser-tab", result.stdout)

    def test_copyright_holds_the_full_licence(self):
        # Debian Policy 12.5: the licence text itself, since AGPL-3.0 isn't in /usr/share/common-licenses.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["dpkg-deb", "-x", str(self.deb), tmp], check=True)
            text = (Path(tmp) / "usr/share/doc/porthole/copyright").read_text(encoding="utf-8")
        template = COPYRIGHT_TEMPLATE.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(template))
        body = text[len(template) :].split("\n")
        self.assertEqual(body[-1], "")
        self.assertEqual(dep5_unindent(body[:-1]), LICENSE.read_text(encoding="ascii"))
        paragraphs = deb822_paragraphs(text)
        self.assertEqual([p[0] for p in paragraphs], [template.splitlines()[0], "Files: *", "License: AGPL-3.0-only"])
        self.assertEqual(paragraphs[2][1].strip(), "GNU AFFERO GENERAL PUBLIC LICENSE")
        for line in text.split("\n"):
            self.assertTrue(line == "" or line.strip(), repr(line))  # a line of only spaces would end a paragraph
            self.assertLessEqual(len(line), 80, line)

    def test_a_bad_version_is_refused(self):
        env = {**os.environ, "OUT_DIR": str(Path(self.tmp.name) / "bad"), "PORTHOLE_VERSION": "0.1.0-1; rm -rf /"}
        result = subprocess.run([BASH, "packaging/deb/build.sh"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not a usable package version", result.stderr)
        self.assertFalse((Path(self.tmp.name) / "bad").exists())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


FAKE_PORTHOLE_WITHOUT_PAGES = """
import http.server, json, socketserver, sys, threading
port = int(sys.argv[sys.argv.index("--port") + 1])
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path == "/api/ping":
            body = json.dumps({"app": "porthole"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)
    def do_POST(self):
        self.send_response(204)
        self.end_headers()
        threading.Thread(target=server.shutdown).start()
class Server(http.server.HTTPServer):
    def server_bind(self):  # skip HTTPServer's name lookup, which can hang for about 30 s on a Mac
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", port
server = Server(("127.0.0.1", port), Handler)
server.serve_forever()
"""


class SmokeTestScriptTest(unittest.TestCase):
    """packaging/smoke_test.py, which CI runs against each built package, works against the app itself."""

    def test_starts_pings_and_quits_the_app(self):
        port = free_port()
        result = subprocess.run(
            [sys.executable, str(ROOT / "packaging" / "smoke_test.py"), "--port", str(port), "--", sys.executable, str(ROOT / "start.py")],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Porthole answered and quit.", result.stdout)

    def test_fails_when_the_pages_are_missing(self):
        # A build that left out web/ still answers /api/ping and quits; the pages must load too.
        result = subprocess.run(
            [sys.executable, str(ROOT / "packaging" / "smoke_test.py"), "--port", str(free_port()), "--timeout", "20",
             "--", sys.executable, "-c", FAKE_PORTHOLE_WITHOUT_PAGES],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/share", result.stdout + result.stderr)

    def test_fails_when_the_app_never_answers(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "packaging" / "smoke_test.py"), "--port", str(free_port()), "--timeout", "2",
             "--", sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never answered", result.stdout + result.stderr)


class WorkflowTest(unittest.TestCase):
    PATH = ROOT / ".github" / "workflows" / "linux.yml"
    USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")

    def test_every_action_is_pinned_to_a_commit(self):
        uses = [self.USES.match(line) for line in self.PATH.read_text(encoding="utf-8").splitlines()]
        uses = [m for m in uses if m]
        self.assertGreaterEqual(len(uses), 6)
        for match in uses:
            action, rest = match.groups()
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", action)
            self.assertRegex(rest, r"#\s*v\d+(\.\d+)*", action)  # the version, as a comment

    def test_scripts_are_run_with_bash(self):
        text = self.PATH.read_text(encoding="utf-8")
        for script in ("packaging/deb/build.sh", "packaging/appimage/build.sh"):
            self.assertIn(f"bash {script}", text)  # git here doesn't keep the executable bit

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_structure(self):
        data = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))
        triggers = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` as true
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertEqual(triggers["push"]["tags"], ["v*"])
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(data["permissions"], {"contents": "read"})
        jobs = data["jobs"]
        self.assertEqual(set(jobs), {"tests", "deb", "appimage", "snap"})  # release.yml makes the draft release
        tests = jobs["tests"]
        self.assertEqual(tests["runs-on"], "ubuntu-latest")
        run_steps = [step for step in tests["steps"] if "run" in step]
        unit = next(step for step in run_steps if "unittest discover -s tests" in step["run"])
        self.assertEqual(str(unit.get("env", {}).get("REQUIRE_BROWSER")), "1")
        with_values = [step.get("with", {}) for step in tests["steps"]]
        self.assertIn("${{ matrix.python-version }}", [str(w.get("python-version")) for w in with_values])
        # Every Python the packages may meet: 3.10 (oldest the .deb allows), 3.12 (snap and AppImage), the newest.
        versions = [str(v) for v in tests["strategy"]["matrix"]["python-version"]]
        for version in ("3.10", "3.12", "3.13", "3.14"):
            self.assertIn(version, versions)
        self.assertIn("22", [str(w.get("node-version")) for w in with_values])
        for name in ("deb", "appimage", "snap"):
            self.assertEqual(jobs[name]["needs"], "tests", name)
            self.assertNotIn("permissions", jobs[name], name)  # read-only, from the top
            steps_text = yaml.safe_dump(jobs[name]["steps"])
            self.assertIn("upload-artifact", steps_text, name)
        self.assertIn("lintian", yaml.safe_dump(jobs["deb"]["steps"]))
        self.assertIn("python-appimage==", yaml.safe_dump(jobs["appimage"]["steps"]))
        self.assertIn("snapcore/action-build", yaml.safe_dump(jobs["snap"]["steps"]))
        self.assertIn("snap install --dangerous", yaml.safe_dump(jobs["snap"]["steps"]))
        for name in ("deb", "appimage", "snap"):
            self.assertIn("smoke_test.py", yaml.safe_dump(jobs[name]["steps"]), name)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_a_package_without_the_licence_fails(self):
        jobs = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))["jobs"]

        def after(job: str, marker: str) -> list[str]:
            # The lines of the shell scripts run after the step that mentions marker, each one whole, so a
            # check that only prints (echo ...) or can't fail (... || true) doesn't count.
            steps = jobs[job]["steps"]
            start = next(i for i, step in enumerate(steps) if marker in yaml.safe_dump(step, width=1000))
            return [line.strip() for step in steps[start + 1 :] for line in step.get("run", "").splitlines()]

        # The installed .deb's copyright ends with LICENSE as DEP-5 text.
        deb = after("deb", "packaging/deb/build.sh")
        self.assertIn('diff <(bash packaging/deb/dep5_text.sh LICENSE) '
                      '<(tail -n "$(wc -l <LICENSE)" /usr/share/doc/porthole/copyright)', deb)
        # The AppImage, unpacked (no FUSE needed), holds LICENSE next to AppRun.
        appimage = after("appimage", "packaging/appimage/build.sh")
        self.assertTrue(any("--appimage-extract" in line for line in appimage), appimage)
        self.assertIn('cmp squashfs-root/LICENSE "$GITHUB_WORKSPACE/LICENSE"', appimage)
        # The installed snap holds it in usr/share/doc/porthole.
        snap = after("snap", "snapcore/action-build")
        self.assertIn("cmp /snap/porthole/current/usr/share/doc/porthole/LICENSE LICENSE", snap)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_the_appimage_carries_the_notices_and_not_what_the_trim_takes_out(self):
        steps = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))["jobs"]["appimage"]["steps"]
        start = next(i for i, step in enumerate(steps) if "packaging/appimage/build.sh" in step.get("run", ""))
        lines = [line.strip() for step in steps[start + 1 :] for line in step.get("run", "").splitlines()]
        # Unpacked (no FUSE needed): the notices next to AppRun, as in the repository.
        self.assertTrue(any("--appimage-extract" in line for line in lines), lines)
        self.assertIn('cmp squashfs-root/THIRD-PARTY-NOTICES.txt '
                      '"$GITHUB_WORKSPACE/packaging/appimage/THIRD-PARTY-NOTICES.txt"', lines)
        # trim.py --check fails if a library or module it takes out is still there, or a kept library isn't named
        # in the notices: the same lists as the build, so the check can't drift from it.
        self.assertIn('python3 "$GITHUB_WORKSPACE/packaging/appimage/trim.py" --check squashfs-root', lines)
        # The bundled Python still imports what Porthole needs, and can't import what was taken out.
        needed = next(line for line in lines if line.startswith('"$python" -I -c \'import '))
        for module in ("ssl", "hashlib", "hmac", "secrets", "uuid", "ctypes", "json", "http.server", "urllib.request"):
            self.assertRegex(needed, rf"[ ,]{re.escape(module)}[,']")
        self.assertIn("for module in tkinter sqlite3 readline curses dbm.gnu ensurepip; do", lines)
        self.assertIn('if "$python" -I -c "import $module" 2>/dev/null; then', lines)
        self.assertIn("exit 1", lines[lines.index('if "$python" -I -c "import $module" 2>/dev/null; then') :])

    @unittest.skipUnless(yaml and BASH, "needs PyYAML and bash")
    def test_the_appimage_runtime_is_the_one_the_notices_name(self):
        # appimagetool puts the AppImage runtime (libfuse, musl, zlib and more, linked in) at the start of the file,
        # and the newer appimagetool fetches the latest one at each build. THIRD-PARTY-NOTICES.txt names the one it
        # covers, so CI fails when the built AppImage has another. The workflow's own step runs here, on a stand-in
        # AppImage that prints what type2-runtime prints for --appimage-version.
        steps = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))["jobs"]["appimage"]["steps"]
        build = next(i for i, step in enumerate(steps) if "packaging/appimage/build.sh" in step.get("run", ""))
        found = [i for i, step in enumerate(steps) if "--appimage-version" in step.get("run", "")]
        self.assertEqual(len(found), 1, found)
        self.assertGreater(found[0], build)
        script = steps[found[0]]["run"]
        notices = ROOT / "packaging" / "appimage" / "THIRD-PARTY-NOTICES.txt"
        named = re.search(r"https://github\.com/AppImage/type2-runtime/commit/[0-9a-f]+", notices.read_text()).group(0)
        for printed, ok in ((f"AppImage runtime version: {named}", True),
                            ("AppImage runtime version: https://github.com/AppImage/type2-runtime/commit/0123abc", False),
                            (named, False),  # not the line the runtime prints
                            ("", False)):
            with self.subTest(printed=printed):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                workspace = Path(tmp.name)
                (workspace / "packaging" / "appimage").mkdir(parents=True)
                shutil.copy(notices, workspace / "packaging" / "appimage")
                image = workspace / "dist" / "Porthole-x86_64.AppImage"
                image.parent.mkdir()
                image.write_text(f"#!/bin/sh\n[ \"$1\" = --appimage-version ] || exit 2\necho '{printed}' >&2\n")
                image.chmod(0o755)
                result = subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                        env={**os.environ, "GITHUB_WORKSPACE": str(workspace)}, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
                if not ok:
                    self.assertIn("THIRD-PARTY-NOTICES.txt", result.stderr)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_the_checks_branch_protection_requires_run_on_every_pull_request(self):
        # Main's branch protection requires the `tests` jobs (the next test). A required check must be reported on a
        # pull request, or the pull request waits for it forever: so the tests run on every pull request into main,
        # text-only ones too (no path filter), while the package builds stay on pushes.
        data = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))
        triggers = data.get("on", data.get(True))
        self.assertIn("pull_request", triggers)
        pull_request = triggers["pull_request"] or {}
        self.assertEqual(pull_request.get("branches", ["main"]), ["main"])
        for key in ("paths", "paths-ignore", "types"):
            self.assertNotIn(key, pull_request)
        jobs = data["jobs"]
        self.assertNotIn("if", jobs["tests"])
        for name in ("deb", "appimage", "snap"):
            self.assertEqual(jobs[name].get("if"), "github.event_name != 'pull_request'", name)

    def test_going_public_has_branch_protection_require_the_tests(self):
        going_public = read_private_doc(self, GOING_PUBLIC)
        start = going_public.index("## 4.")
        section = going_public[start : going_public.index("\n## ", start + 1)]
        rule = next(line for line in section.splitlines() if "Branch protection" in line)
        self.assertIn("`tests`", rule)
        self.assertIn("pull request", rule)

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_a_tag_must_have_its_release_in_the_metainfo(self):
        steps = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))["jobs"]["deb"]["steps"]
        check = next(step for step in steps if "appstreamcli validate" in step.get("run", ""))
        self.assertIn("refs/tags/v", check["run"])
        self.assertIn("python3 packaging/buildinfo.py --check-metainfo", check["run"])

    @unittest.skipUnless(yaml, "PyYAML not installed")
    def test_the_snap_takes_the_tags_version(self):
        # snapcraft.yaml says 0.1.0; on a v* tag the snap must carry the tag's version like the other packages.
        steps = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))["jobs"]["snap"]["steps"]
        build = next(i for i, step in enumerate(steps) if "snapcore/action-build" in step.get("uses", ""))
        before = yaml.safe_dump(steps[:build], width=1000)
        for wanted in ("refs/tags/v", "packaging/buildinfo.py", "snap/snapcraft.yaml", "version:"):
            self.assertIn(wanted, before)

    def test_no_untrusted_expressions_in_scripts(self):
        # Values from the event (tag names, titles) go through env, never straight into a shell script.
        text = self.PATH.read_text(encoding="utf-8")
        for expression in re.findall(r"\$\{\{([^}]*)\}\}", text):
            self.assertNotIn("github.event", expression)
            self.assertNotIn("head_ref", expression)


if __name__ == "__main__":
    unittest.main()
