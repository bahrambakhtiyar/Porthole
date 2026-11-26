import contextlib
import io
import os
import shlex
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from porthole import app as share
from porthole import launcher
from porthole.launcher import desktop_entry, install_linux, uninstall_linux, windows_script
from tests.test_paths import FrozenMixin

ROOT = Path(__file__).resolve().parent.parent
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class DesktopEntryTest(unittest.TestCase):
    def test_entry_fields(self):
        entry = desktop_entry(
            "/usr/bin/python3",
            PurePosixPath("/opt/My Apps/porthole/share.py"),
            PurePosixPath("/opt/My Apps/porthole/assets/icon.png"),
        )
        self.assertTrue(entry.startswith("[Desktop Entry]\n"))
        for line in (
            "Type=Application",
            "Name=Porthole",
            'Exec="/usr/bin/python3" "/opt/My Apps/porthole/share.py" --launcher',
            "Terminal=false",
            "Icon=/opt/My Apps/porthole/assets/icon.png",
            "Categories=Network;RemoteAccess;",
        ):
            self.assertIn(line + "\n", entry)

    def test_python_path_with_space_is_quoted(self):
        entry = desktop_entry("/home/me/my env/bin/python3", PurePosixPath("/tmp/share.py"), PurePosixPath("/tmp/icon.png"))
        self.assertIn('Exec="/home/me/my env/bin/python3" "/tmp/share.py" --launcher\n', entry)

    def test_exec_quoting_follows_the_spec(self):
        entry = desktop_entry("/usr/bin/python3", PurePosixPath('/tmp/a$b"c%d/share.py'), PurePosixPath("/tmp/icon.png"))
        exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
        # quoting rule escapes $ and ", then the string rule doubles the backslash; % becomes %%
        self.assertEqual(exec_line, 'Exec="/usr/bin/python3" "/tmp/a\\\\$b\\\\"c%%d/share.py" --launcher')

    def test_install_and_uninstall_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = Path(tmp) / "applications"
            path = install_linux("/usr/bin/python3", ROOT / "start.py", ROOT / "assets" / "icon.png", apps)
            self.assertEqual(path, apps / "porthole.desktop")
            self.assertIn("Name=Porthole", path.read_text(encoding="utf-8"))
            if os.name != "nt":  # Windows keeps no executable bit (it goes by the file name's ending)
                self.assertTrue(os.stat(path).st_mode & 0o100)
            self.assertTrue(uninstall_linux(apps))
            self.assertFalse(path.exists())
            self.assertFalse(uninstall_linux(apps))


@unittest.skipIf(os.name == "nt", "AppImages and their menu entries exist only on Linux")
class AppImageLauncherTest(unittest.TestCase):
    """Run as an AppImage, the launcher starts the AppImage file ($APPIMAGE), not the Python inside it:
    the folder that Python sits in exists only while the AppImage runs. The AppImage runtime also sets
    $APPDIR, the mount folder, and Porthole's own files are inside it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        self.appimage = str(Path(tmp.name) / "My Apps" / "Porthole-x86_64.AppImage")
        env = {"XDG_DATA_HOME": str(self.data), "APPIMAGE": self.appimage, "APPDIR": str(launcher.ROOT)}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(launcher.sys, "platform", "linux"),
            mock.patch.object(launcher, "_refresh_menu", lambda apps: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def kept_icon(self):
        return self.data / "icons" / "hicolor" / "512x512" / "apps" / "porthole.png"

    def test_install_runs_the_appimage_and_keeps_a_copy_of_the_icon(self):
        messages = launcher.install()
        entry = (self.data / "applications" / "porthole.desktop").read_text(encoding="utf-8")
        exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
        self.assertEqual(exec_line, f'Exec="{self.appimage}" --launcher')
        # web/ is in every build (the AppImage bundles porthole/, web/ and start.py, not assets/).
        self.assertEqual(self.kept_icon().read_bytes(), (ROOT / "web" / "icon-512.png").read_bytes())
        self.assertIn(f"Icon={self.kept_icon()}\n", entry)
        self.assertIn(f"Remove it with: {shlex.quote(self.appimage)} --uninstall-launcher", messages)

    def test_the_remove_command_can_be_pasted_when_the_path_has_a_space(self):
        messages = launcher.install()
        hint = next(line for line in messages if line.startswith("Remove it with: "))
        command = shlex.split(hint[len("Remove it with: ") :])
        self.assertEqual(command, [self.appimage, "--uninstall-launcher"])

    def test_appimage_variables_inherited_from_another_app_are_ignored(self):
        # A terminal inside an editor that is itself an AppImage passes its $APPIMAGE and $APPDIR on.
        # Porthole run from source there is not that AppImage: the menu entry must start Porthole.
        editor = "/opt/Editor.AppImage"
        for appdir in (None, "/tmp/.mount_EditorXy12", str(launcher.ROOT / "web" / "elsewhere")):
            with self.subTest(appdir=appdir):
                os.environ["APPIMAGE"] = editor
                if appdir is None:
                    os.environ.pop("APPDIR", None)
                else:
                    os.environ["APPDIR"] = appdir
                messages = launcher.install()
                entry = (self.data / "applications" / "porthole.desktop").read_text(encoding="utf-8")
                self.assertNotIn(editor, entry)
                self.assertIn(f'"{launcher.SCRIPT}" --launcher', entry)
                self.assertNotIn(editor, "\n".join(messages))
                self.assertIn("Remove it with: python3 start.py --uninstall-launcher", messages)
                self.assertFalse(self.kept_icon().exists())

    def test_works_without_the_assets_folder(self):
        with mock.patch.object(launcher, "ICON_PNG", ROOT / "no-such-folder" / "icon.png"):
            launcher.install()
        self.assertTrue(self.kept_icon().is_file())

    def test_uninstall_removes_the_entry_and_the_icon(self):
        launcher.install()
        self.assertEqual(launcher.uninstall(), ["Removed Porthole from the app menu."])
        self.assertFalse((self.data / "applications" / "porthole.desktop").exists())
        self.assertFalse(self.kept_icon().exists())

    def test_a_copy_without_assets_points_at_the_web_icon(self):
        # The .deb ships porthole/, web/ and start.py only.
        del os.environ["APPIMAGE"]
        with mock.patch.object(launcher, "ICON_PNG", ROOT / "no-such-folder" / "icon.png"):
            launcher.install()
        entry = (self.data / "applications" / "porthole.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Icon={ROOT / 'web' / 'icon-512.png'}\n", entry)

    def test_without_appimage_it_runs_this_python(self):
        del os.environ["APPIMAGE"]
        launcher.install()
        entry = (self.data / "applications" / "porthole.desktop").read_text(encoding="utf-8")
        self.assertIn(f'"{launcher.SCRIPT}" --launcher', entry)
        self.assertFalse((self.data / "icons").exists())


PACKAGE_ENTRY = (ROOT / "packaging" / "linux" / "porthole.desktop").read_text(encoding="utf-8")
OLDER_REMOVED = "Removed an older Porthole menu entry; the package's own entry stays.\n"


@unittest.skipIf(os.name == "nt", "desktop entries exist only on Linux")
class OlderEntryUnderThePackageTest(unittest.TestCase):
    """The .deb installs /usr/share/applications/porthole.desktop. A user entry of the same name, left by
    --install-launcher from the AppImage or a folder, comes first in the menu and hides it. Run as the .deb, both
    --install-launcher and --uninstall-launcher take that older entry away, and never the package's own entry or a
    porthole.desktop that Porthole didn't write."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        self.system = Path(tmp.name) / "usr-share"  # stands in for /usr/share, where the package's entry is
        (self.system / "applications").mkdir(parents=True)
        self.package_entry = self.system / "applications" / "porthole.desktop"
        self.package_entry.write_text(PACKAGE_ENTRY, encoding="utf-8")
        self.refreshed = []
        env = {"XDG_DATA_HOME": str(self.data), "XDG_DATA_DIRS": str(self.system)}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(launcher.sys, "platform", "linux"),
            mock.patch.object(launcher, "_refresh_menu", self.refreshed.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("APPIMAGE", "APPDIR"):  # not an AppImage (patch.dict puts them back afterwards)
            os.environ.pop(name, None)

    @property
    def user_entry(self):
        return self.data / "applications" / "porthole.desktop"

    def kept_icon(self):
        return self.data / "icons" / "hicolor" / "512x512" / "apps" / "porthole.png"

    def as_the_deb(self, flag):
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PORTHOLE_COMMAND": "porthole"}),
            mock.patch("porthole.sandbox.kind", return_value="none"),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(share.main([flag]), 0)
        return out.getvalue()

    def install_from_the_appimage(self):
        appimage = str(self.data.parent / "Porthole-x86_64.AppImage")
        with mock.patch.dict(os.environ, {"APPIMAGE": appimage, "APPDIR": str(launcher.ROOT)}):
            launcher.install()
        self.assertTrue(self.kept_icon().is_file())
        self.assertIn(f'Exec="{appimage}" --launcher', self.user_entry.read_text(encoding="utf-8"))
        self.refreshed.clear()

    def test_install_launcher_removes_an_entry_the_appimage_left(self):
        self.install_from_the_appimage()
        self.assertEqual(self.as_the_deb("--install-launcher"), OLDER_REMOVED)
        self.assertFalse(self.user_entry.exists())
        self.assertFalse(self.kept_icon().exists())  # it was there for that entry only
        self.assertEqual(self.package_entry.read_text(encoding="utf-8"), PACKAGE_ENTRY)
        self.assertEqual(self.refreshed, [self.data / "applications"])
        # Nothing left to remove: said as before.
        self.assertEqual(self.as_the_deb("--install-launcher"), "Porthole is already in your app menu.\n")

    def test_uninstall_launcher_removes_an_entry_a_folder_left(self):
        launcher.install()  # from source: python3 start.py --install-launcher
        self.kept_icon().parent.mkdir(parents=True)
        self.kept_icon().write_bytes(b"an icon some other entry uses")
        self.assertEqual(self.as_the_deb("--uninstall-launcher"), OLDER_REMOVED)
        self.assertFalse(self.user_entry.exists())
        self.assertTrue(self.kept_icon().exists())  # that entry didn't use it
        self.assertEqual(self.package_entry.read_text(encoding="utf-8"), PACKAGE_ENTRY)
        self.assertEqual(
            self.as_the_deb("--uninstall-launcher"), "Porthole's menu entry comes with the app. Uninstall the app to remove it.\n"
        )

    def test_entries_that_porthole_didnt_write_stay(self):
        others = {
            "another app's": "[Desktop Entry]\nType=Application\nName=Porthole\nComment=A package manager\nExec=porthole\n",
            "the package's, copied": PACKAGE_ENTRY,
            "not a desktop entry": "Name=Porthole\nComment=Share this screen with phones and PCs on the same network\n",
        }
        self.user_entry.parent.mkdir(parents=True)
        for label, text in others.items():
            with self.subTest(label):
                self.user_entry.write_text(text, encoding="utf-8")
                self.assertEqual(self.as_the_deb("--install-launcher"), "Porthole is already in your app menu.\n")
                self.assertEqual(
                    self.as_the_deb("--uninstall-launcher"),
                    "Porthole's menu entry comes with the app. Uninstall the app to remove it.\n",
                )
                self.assertEqual(self.user_entry.read_text(encoding="utf-8"), text)
        self.user_entry.unlink()
        self.user_entry.symlink_to(self.package_entry)  # a link to the package's own entry
        self.assertEqual(self.as_the_deb("--install-launcher"), "Porthole is already in your app menu.\n")
        self.assertTrue(self.user_entry.is_symlink())
        self.assertEqual(self.package_entry.read_text(encoding="utf-8"), PACKAGE_ENTRY)
        self.assertEqual(self.refreshed, [])

    def test_an_entry_porthole_wrote_and_someone_edited_is_still_recognised(self):
        launcher.install()
        text = self.user_entry.read_text(encoding="utf-8").replace(" --launcher\n", " --launcher --port 8770\n")
        self.user_entry.write_text(text, encoding="utf-8")
        self.assertEqual(self.as_the_deb("--install-launcher"), OLDER_REMOVED)

    def test_from_a_folder_uninstall_leaves_an_entry_porthole_didnt_write(self):
        self.user_entry.parent.mkdir(parents=True)
        self.user_entry.write_text(PACKAGE_ENTRY, encoding="utf-8")
        self.assertEqual(launcher.uninstall(), [launcher.NOT_IN_MENU])
        self.assertEqual(self.user_entry.read_text(encoding="utf-8"), PACKAGE_ENTRY)


class WindowsScriptTest(unittest.TestCase):
    PYTHONW = "C:\\Python312\\pythonw.exe"
    SCRIPT = Path("C:\\Users\\Ayla's PC\\lan-screen-share\\share.py")
    ICON = Path("C:\\Users\\Ayla's PC\\lan-screen-share\\assets\\icon.ico")

    def test_install_script(self):
        script = windows_script(self.PYTHONW, self.SCRIPT, self.ICON)
        for part in (
            "GetFolderPath('Programs')",
            "GetFolderPath('Desktop')",
            "WScript.Shell",
            "CreateShortcut",
            "Porthole.lnk",
            "'C:\\Python312\\pythonw.exe'",
            "Ayla''s PC",
            "--launcher",
            "WorkingDirectory = 'C:\\Users\\Ayla''s PC\\lan-screen-share'",
        ):
            self.assertIn(part, script)

    def test_remove_script(self):
        script = windows_script(self.PYTHONW, self.SCRIPT, self.ICON, remove=True)
        self.assertIn("Remove-Item", script)
        self.assertNotIn("CreateShortcut", script)

    def test_powershell_runs_without_a_console_window(self):
        # From the windowed Porthole.exe a console program would flash a black window.
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with (
            mock.patch.object(launcher.subprocess, "run", fake_run),
            mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            launcher._run_powershell("Write-Output 'hi'")
        args, kwargs = calls[0]
        self.assertEqual(args[0], "powershell")
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_powershell_errors_are_reported(self):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="Access is denied.")

        with mock.patch.object(launcher.subprocess, "run", fake_run), self.assertRaises(RuntimeError) as ctx:
            launcher._run_powershell("Write-Output 'hi'")
        self.assertIn("Access is denied.", str(ctx.exception))


class WindowsShortcutTest(FrozenMixin, unittest.TestCase):
    """--install-launcher on Windows: from source the shortcut runs pythonw with start.py; the built
    Porthole.exe points the shortcut at itself (the installer normally makes the shortcuts)."""

    def install(self, remove=False):
        scripts = []
        with (
            mock.patch.object(launcher.sys, "platform", "win32"),
            mock.patch.object(launcher, "_run_powershell", scripts.append),
        ):
            messages = launcher.uninstall() if remove else launcher.install()
        self.assertEqual(len(scripts), 1)
        return scripts[0], messages

    def test_the_built_program_points_the_shortcut_at_itself(self):
        self.freeze()
        script, messages = self.install()
        self.assertIn(f"$link.TargetPath = {launcher._ps(self.exe)}", script)
        self.assertIn("$link.Arguments = '--launcher'", script)
        self.assertIn(f"$link.IconLocation = {launcher._ps(self.exe)}", script)  # the icon inside Porthole.exe
        self.assertIn(f"$link.WorkingDirectory = {launcher._ps(str(self.program_dir))}", script)
        self.assertNotIn("pythonw", script.lower())
        self.assertNotIn("start.py", script)
        self.assertEqual(messages, ["Added Porthole to the Start menu and the desktop."])

    def test_the_built_program_removes_the_same_shortcuts(self):
        self.freeze()
        script, messages = self.install(remove=True)
        self.assertIn("'Porthole.lnk'", script)
        self.assertIn("Remove-Item", script)
        self.assertEqual(messages, ["Removed Porthole from the Start menu and the desktop."])

    def test_from_source_the_shortcut_runs_start_py(self):
        script, _ = self.install()
        self.assertIn(f"$link.Arguments = {launcher._ps(chr(34) + str(launcher.SCRIPT) + chr(34) + ' --launcher')}", script)
        self.assertIn(f"$link.IconLocation = {launcher._ps(launcher.ICON_ICO)}", script)


class MacLauncherTest(unittest.TestCase):
    """On a Mac there is no launcher to add or remove. The built app goes in the Applications folder; run from
    source (the only way while the Mac app is parked), Porthole is started with python3 start.py."""

    def said(self, *, frozen: bool) -> dict[str, str]:
        words = {}
        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp})
        ), mock.patch.object(launcher.sys, "platform", "darwin"), (
            mock.patch.object(launcher.paths, "frozen", return_value=frozen)
        ):
            for action in (launcher.install, launcher.uninstall):
                with self.subTest(action=action.__name__), self.assertRaises(launcher.NotOnThisSystem) as ctx:
                    action()
                words[action.__name__] = str(ctx.exception)
            self.assertEqual(os.listdir(tmp), [])  # touches nothing
        return words

    def test_the_built_app_goes_in_applications(self):
        self.assertEqual(self.said(frozen=True), {"install": launcher.MAC_INSTALL, "uninstall": launcher.MAC_UNINSTALL})

    def test_from_source_it_never_points_at_an_app(self):
        words = self.said(frozen=False)
        self.assertEqual(words, {"install": launcher.MAC_SOURCE_INSTALL, "uninstall": launcher.MAC_SOURCE_UNINSTALL})
        for text in words.values():
            self.assertNotIn("Applications", text)
        self.assertIn("python3 start.py", words["install"])

    def test_the_words(self):
        self.assertEqual(
            launcher.MAC_INSTALL, "On a Mac there's no launcher to add: put Porthole in the Applications folder."
        )
        self.assertEqual(
            launcher.MAC_UNINSTALL,
            "On a Mac there's no launcher to remove: to remove Porthole, drag it from the Applications folder to the Bin.",
        )
        self.assertEqual(
            launcher.MAC_SOURCE_INSTALL, "On a Mac there's no launcher to add: start Porthole with python3 start.py."
        )
        self.assertEqual(
            launcher.MAC_SOURCE_UNINSTALL,
            "On a Mac there's no launcher to remove: Porthole run from source never adds one.",
        )


class LauncherPathsTest(unittest.TestCase):
    def test_icons_come_from_the_resource_folder(self):
        self.assertEqual(launcher.ROOT, ROOT)
        self.assertTrue(launcher.ICON_ICO.is_file())
        self.assertTrue(launcher.WEB_ICON.is_file())


class IconFilesTest(unittest.TestCase):
    def test_png_icon(self):
        data = (ROOT / "assets" / "icon.png").read_bytes()
        self.assertTrue(data.startswith(PNG_MAGIC))
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (256, 256))

    def test_ico_icon_has_four_png_images(self):
        data = (ROOT / "assets" / "icon.ico").read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        count = struct.unpack("<H", data[4:6])[0]
        self.assertEqual(count, 4)
        sizes = []
        for i in range(count):
            width, _h, _c, _r, _planes, _bits, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i : 22 + 16 * i])
            sizes.append(width or 256)
            self.assertTrue(data[offset : offset + 8] == PNG_MAGIC)
        self.assertEqual(sorted(sizes), [16, 32, 48, 256])

    def test_svg_favicon(self):
        self.assertIn("<svg", (ROOT / "web" / "icon.svg").read_text(encoding="utf-8"))


class ShareFlagsTest(unittest.TestCase):
    def test_install_launcher_flag(self):
        out = io.StringIO()
        with mock.patch("porthole.launcher.install", return_value=["Added it."]), contextlib.redirect_stdout(out):
            self.assertEqual(share.main(["--install-launcher"]), 0)
        self.assertIn("Added it.", out.getvalue())

    def test_install_launcher_in_a_sandbox_does_nothing(self):
        # A snap or Flatpak brings its own menu entry.
        for kind in ("snap", "flatpak"):
            out = io.StringIO()
            with (
                mock.patch("porthole.sandbox.kind", return_value=kind),
                mock.patch("porthole.launcher.install", side_effect=AssertionError("must not run")),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(share.main(["--install-launcher"]), 0)
            self.assertEqual(out.getvalue(), "Porthole is already in your app menu.\n", kind)

    def test_uninstall_launcher_in_a_sandbox_does_nothing(self):
        out = io.StringIO()
        with (
            mock.patch("porthole.sandbox.kind", return_value="snap"),
            mock.patch("porthole.launcher.uninstall", side_effect=AssertionError("must not run")),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(share.main(["--uninstall-launcher"]), 0)
        self.assertEqual(out.getvalue(), "Porthole's menu entry comes with the app. Uninstall the app to remove it.\n")

    def deb(self, *args, removed=False):
        """main() as the .deb's /usr/bin/porthole starts it (PORTHOLE_COMMAND set); what it printed. `removed`: whether
        launcher.remove_older_entry found a user entry an earlier --install-launcher left."""
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PORTHOLE_COMMAND": "porthole"}),
            mock.patch("porthole.sandbox.kind", return_value="none"),
            mock.patch("porthole.launcher.install", side_effect=AssertionError("must not run")),
            mock.patch("porthole.launcher.uninstall", side_effect=AssertionError("must not run")),
            mock.patch("porthole.launcher.remove_older_entry", return_value=removed),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(share.main(list(args)), 0)
        return out.getvalue()

    def test_the_deb_brings_its_own_menu_entry_too(self):
        # A user entry would hide the package's (same desktop ID), lose PORTHOLE_COMMAND, and be left broken after
        # the package is removed.
        self.assertEqual(self.deb("--install-launcher"), "Porthole is already in your app menu.\n")
        self.assertEqual(
            self.deb("--uninstall-launcher"), "Porthole's menu entry comes with the app. Uninstall the app to remove it.\n"
        )

    def test_the_deb_removes_a_user_entry_an_earlier_install_launcher_left(self):
        # From the AppImage or a folder, --install-launcher wrote porthole.desktop in the user's folder: it hides the
        # package's entry of the same name, so both flags take it away and say the package's own entry stays.
        older = "Removed an older Porthole menu entry; the package's own entry stays.\n"
        self.assertEqual(self.deb("--install-launcher", removed=True), older)
        self.assertEqual(self.deb("--uninstall-launcher", removed=True), older)

    def test_uninstall_launcher_flag(self):
        out = io.StringIO()
        with mock.patch("porthole.launcher.uninstall", return_value=["Removed it."]), contextlib.redirect_stdout(out):
            self.assertEqual(share.main(["--uninstall-launcher"]), 0)
        self.assertIn("Removed it.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
