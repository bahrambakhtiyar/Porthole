"""sandbox.py: Snap or Flatpak, an AppImage, and the user's real home folder."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porthole import sandbox

SANDBOX_VARS = ("SNAP", "SNAP_NAME", "SNAP_REAL_HOME", "FLATPAK_ID", "APPIMAGE", "APPDIR")


class SandboxTest(unittest.TestCase):
    def setUp(self):
        # Start from an environment with none of the sandbox variables, whatever runs the tests.
        clean = {key: value for key, value in os.environ.items() if key not in SANDBOX_VARS}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.no_info = Path(tmp.name) / "no-flatpak-info"
        self.info = Path(tmp.name) / ".flatpak-info"
        self.info.write_text("[Application]\nname=io.github.bahrambakhtiyar.Porthole\n")
        self.real_info = sandbox.FLATPAK_INFO
        info_patch = mock.patch.object(sandbox, "FLATPAK_INFO", self.no_info)
        info_patch.start()
        self.addCleanup(info_patch.stop)

    def test_none_outside_a_sandbox(self):
        self.assertEqual(sandbox.kind(), "none")
        self.assertFalse(sandbox.sandboxed())

    def test_snap_needs_both_variables(self):
        os.environ["SNAP"] = "/snap/porthole/x1"
        snap_root = mock.patch.object(sandbox, "APP_ROOT", Path("/snap/porthole/x1"))
        snap_root.start()
        self.addCleanup(snap_root.stop)
        self.assertEqual(sandbox.kind(), "none")
        os.environ["SNAP_NAME"] = "porthole"
        self.assertEqual(sandbox.kind(), "snap")
        self.assertTrue(sandbox.sandboxed())
        del os.environ["SNAP"]
        self.assertEqual(sandbox.kind(), "none")

    def test_snap_variables_inherited_from_another_snap_do_not_count(self):
        # A terminal inside another snap app (an editor, say) passes $SNAP on to everything it starts.
        os.environ.update({"SNAP": "/snap/code/123", "SNAP_NAME": "code"})
        with mock.patch.object(sandbox, "APP_ROOT", Path("/home/ayla/porthole")):
            self.assertEqual(sandbox.kind(), "none")
        with mock.patch.object(sandbox, "APP_ROOT", Path("/snap/code/123/porthole")):
            self.assertEqual(sandbox.kind(), "snap")

    def test_empty_snap_variables_are_not_a_snap(self):
        os.environ.update({"SNAP": "", "SNAP_NAME": "porthole"})
        self.assertEqual(sandbox.kind(), "none")

    def test_flatpak_by_variable(self):
        os.environ["FLATPAK_ID"] = "io.github.bahrambakhtiyar.Porthole"
        self.assertEqual(sandbox.kind(), "flatpak")

    def test_flatpak_by_info_file(self):
        with mock.patch.object(sandbox, "FLATPAK_INFO", self.info):
            self.assertEqual(sandbox.kind(), "flatpak")

    def test_the_flatpak_info_file_is_at_the_root(self):
        self.assertEqual(self.real_info, Path("/.flatpak-info"))

    def test_real_home_in_a_snap(self):
        os.environ.update({"SNAP": "/snap/porthole/x1", "SNAP_NAME": "porthole", "SNAP_REAL_HOME": "/home/ayla"})
        os.environ["HOME"] = "/home/ayla/snap/porthole/x1"
        self.assertEqual(sandbox.real_home(), Path("/home/ayla"))

    def test_real_home_without_a_snap(self):
        os.environ["HOME"] = "/home/ayla"
        if os.name == "nt":
            self.assertEqual(sandbox.real_home(), Path.home())
        else:
            self.assertEqual(sandbox.real_home(), Path("/home/ayla"))

    def test_empty_real_home_is_ignored(self):
        os.environ["SNAP_REAL_HOME"] = ""
        self.assertEqual(sandbox.real_home(), Path.home())


class AppImageTest(unittest.TestCase):
    """The AppImage runtime sets $APPIMAGE (the .AppImage file) and $APPDIR (where it is mounted) for its app,
    and every program that app starts inherits them. They mean "Porthole is an AppImage" only when
    Porthole's own files are inside $APPDIR."""

    FILE = "/home/ayla/Apps/Porthole-x86_64.AppImage"

    def setUp(self):
        clean = {key: value for key, value in os.environ.items() if key not in SANDBOX_VARS}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mount = Path(tmp.name) / ".mount_PorthoAb12"
        (self.mount / "porthole").mkdir(parents=True)

    def test_porthole_inside_the_mount_is_the_appimage(self):
        os.environ.update({"APPIMAGE": self.FILE, "APPDIR": str(self.mount)})
        self.assertEqual(sandbox.appimage(self.mount), self.FILE)
        self.assertEqual(sandbox.appimage(self.mount / "porthole"), self.FILE)

    def test_not_an_appimage_without_the_variables(self):
        self.assertIsNone(sandbox.appimage(self.mount))
        os.environ["APPDIR"] = str(self.mount)
        self.assertIsNone(sandbox.appimage(self.mount))
        os.environ.update({"APPIMAGE": "", "APPDIR": str(self.mount)})
        self.assertIsNone(sandbox.appimage(self.mount))

    def test_variables_inherited_from_another_appimage_are_ignored(self):
        os.environ["APPIMAGE"] = "/opt/Editor.AppImage"
        self.assertIsNone(sandbox.appimage(self.mount))  # no $APPDIR
        os.environ["APPDIR"] = str(self.mount.parent / ".mount_EditorZz99")
        self.assertIsNone(sandbox.appimage(self.mount))  # mounted somewhere else
        os.environ["APPDIR"] = str(self.mount / "porthole")
        self.assertIsNone(sandbox.appimage(self.mount))  # $APPDIR inside Porthole's folder doesn't count
        os.environ["APPDIR"] = str(self.mount) + "-other"
        self.assertIsNone(sandbox.appimage(self.mount))  # a name that only starts the same

    @unittest.skipIf(os.name == "nt", "symbolic links")
    def test_links_are_followed(self):
        link = self.mount.parent / "link-to-mount"
        link.symlink_to(self.mount)
        os.environ.update({"APPIMAGE": self.FILE, "APPDIR": str(link)})
        self.assertEqual(sandbox.appimage(self.mount / "porthole"), self.FILE)

    def test_by_default_it_checks_portholes_own_folder(self):
        os.environ.update({"APPIMAGE": self.FILE, "APPDIR": str(self.mount)})
        self.assertIsNone(sandbox.appimage())  # these tests run from the source folder, not the mount
        os.environ["APPDIR"] = str(sandbox.APP_ROOT)
        self.assertEqual(sandbox.appimage(), self.FILE)
        self.assertTrue((sandbox.APP_ROOT / "start.py").is_file())


if __name__ == "__main__":
    unittest.main()
