"""paths.py: where Porthole's own files are, from source and in a frozen app (PyInstaller)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porthole import paths

ROOT = Path(__file__).resolve().parent.parent


class FrozenMixin:
    """Pretend to be a frozen app: sys.frozen, sys._MEIPASS (the unpacked bundle) and sys.executable."""

    def freeze(self, meipass=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.program_dir = Path(tmp.name) / "Porthole"
        self.bundle = self.program_dir / "_internal"
        (self.bundle / "web").mkdir(parents=True)
        self.exe = str(self.program_dir / ("Porthole.exe" if os.name == "nt" else "Porthole"))
        patches = [mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", self.exe)]
        if meipass:
            patches.append(mock.patch.object(sys, "_MEIPASS", str(self.bundle), create=True))
        elif hasattr(sys, "_MEIPASS"):
            patches.append(mock.patch.object(sys, "_MEIPASS", None))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class MacBundleMixin:
    """Pretend to be the Porthole.app PyInstaller builds: the program is Porthole.app/Contents/MacOS/Porthole and
    sys._MEIPASS is Contents/Frameworks, where web/ is a link to Contents/Resources/web."""

    def freeze_mac(self, name="Porthole.app", platform="darwin"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.apps = Path(tmp.name) / "Applications"
        self.bundle = self.apps / name
        contents = self.bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources" / "web").mkdir(parents=True)
        self.frameworks = contents / "Frameworks"
        self.frameworks.mkdir()
        if os.name == "nt":  # Windows needs extra rights for links: a plain folder stands in
            (self.frameworks / "web").mkdir()
        else:
            (self.frameworks / "web").symlink_to(Path("..") / "Resources" / "web")
        self.exe = str(contents / "MacOS" / "Porthole")
        for patcher in (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", self.exe),
            mock.patch.object(sys, "_MEIPASS", str(self.frameworks), create=True),
            mock.patch.object(sys, "platform", platform),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class SourcePathsTest(unittest.TestCase):
    def test_not_frozen_when_run_from_source(self):
        self.assertFalse(paths.frozen())

    def test_resources_sit_next_to_start_py(self):
        self.assertEqual(paths.resource_root(), ROOT)
        self.assertTrue((paths.resource_root() / "start.py").is_file())
        self.assertEqual(paths.web_dir(), ROOT / "web")
        self.assertTrue((paths.web_dir() / "share.html").is_file())

    def test_the_app_folder_is_the_folder_with_start_py(self):
        self.assertEqual(paths.app_folder(), ROOT)


class FrozenPathsTest(FrozenMixin, unittest.TestCase):
    def test_frozen(self):
        self.freeze()
        self.assertTrue(paths.frozen())

    def test_resources_come_from_the_unpacked_bundle(self):
        self.freeze()
        self.assertEqual(paths.resource_root(), self.bundle)
        self.assertEqual(paths.web_dir(), self.bundle / "web")

    def test_without_meipass_the_program_folder_holds_them(self):
        # Other freezers set sys.frozen but keep the files next to the program.
        self.freeze(meipass=False)
        self.assertEqual(paths.resource_root(), self.program_dir.resolve())

    def test_the_app_folder_is_the_programs_folder_not_the_bundle(self):
        # "Already running from <folder>" names the folder people know (C:\Program Files\Porthole), and in
        # one-file mode the bundle is a new temporary folder on every run.
        self.freeze()
        self.assertEqual(paths.app_folder(), self.program_dir.resolve())

    def test_a_false_frozen_flag_is_not_frozen(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertFalse(paths.frozen())
            self.assertEqual(paths.resource_root(), ROOT)

    def test_program_name(self):
        self.freeze()
        self.assertEqual(paths.program_name(), Path(self.exe).name)


class MacAppPathsTest(MacBundleMixin, unittest.TestCase):
    def test_the_app_bundle(self):
        self.freeze_mac()
        self.assertEqual(paths.app_bundle(), self.bundle.resolve())

    def test_the_app_folder_is_the_folder_that_holds_porthole_app(self):
        # "Already running from /Applications", not from .../Porthole.app/Contents/MacOS.
        self.freeze_mac()
        self.assertEqual(paths.app_folder(), self.apps.resolve())

    def test_another_name_still_counts(self):
        self.freeze_mac(name="Porthole 2.app")
        self.assertEqual(paths.app_bundle(), self.bundle.resolve())
        self.assertEqual(paths.app_folder(), self.apps.resolve())

    def test_resources_come_from_meipass(self):
        # PyInstaller 6 puts the data in Contents/Resources and links it into Contents/Frameworks (sys._MEIPASS).
        self.freeze_mac()
        self.assertEqual(paths.resource_root(), self.frameworks)
        self.assertEqual(paths.web_dir(), self.frameworks / "web")
        self.assertTrue(paths.web_dir().is_dir())

    def test_only_on_a_mac(self):
        self.freeze_mac(platform="linux")
        self.assertIsNone(paths.app_bundle())
        self.assertEqual(paths.app_folder(), (self.bundle / "Contents" / "MacOS").resolve())

    def test_not_frozen_is_no_bundle(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsNone(paths.app_bundle())
            self.assertEqual(paths.app_folder(), ROOT)

    def test_a_plain_program_folder_on_a_mac_is_no_bundle(self):
        # PyInstaller's plain dist/Porthole folder, next to the .app.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        program_dir = Path(tmp.name) / "Porthole"
        program_dir.mkdir()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(program_dir / "Porthole")),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            self.assertIsNone(paths.app_bundle())
            self.assertEqual(paths.app_folder(), program_dir.resolve())


if __name__ == "__main__":
    unittest.main()
