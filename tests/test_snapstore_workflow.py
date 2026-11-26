"""The Snap Store upload: when the owner publishes a GitHub release, its snap goes to the store's stable channel.

The draft release stays the owner's to check; publishing it is the go for the store too, so nothing reaches the
store before a person has looked. The store login is the repository secret SNAPCRAFT_STORE_CREDENTIALS (made
with `snapcraft export-login`); without it the job stops with a plain error, uploading nothing."""

import unittest
from pathlib import Path

try:
    import yaml  # PyYAML: only for checking the workflows, never needed by the app
except ImportError:
    yaml = None

from tests.test_release_workflow import PINNED, WORKFLOWS, load

SNAPSTORE = WORKFLOWS / "snapstore.yml"


class SnapStoreTextTest(unittest.TestCase):
    def setUp(self):
        self.text = SNAPSTORE.read_text(encoding="utf-8")

    def test_uploads_the_published_release_s_snap_to_stable(self):
        self.assertIn("gh release download", self.text)
        self.assertIn("--pattern '*.snap'", self.text)
        self.assertIn("snapcraft upload --release=stable", self.text)

    def test_stops_plainly_without_the_store_login(self):
        self.assertIn("SNAPCRAFT_STORE_CREDENTIALS: ${{ secrets.SNAPCRAFT_STORE_CREDENTIALS }}", self.text)
        self.assertIn('if [[ -z "$SNAPCRAFT_STORE_CREDENTIALS" ]]', self.text)

    def test_takes_only_a_version_tag(self):
        self.assertIn(r'[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]', self.text)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class SnapStoreWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.data = load(SNAPSTORE)
        (self.job,) = self.data["jobs"].values()

    def test_runs_when_a_release_is_published_or_by_hand(self):
        self.assertEqual(self.data["on"]["release"], {"types": ["published"]})
        self.assertIn("tag", self.data["on"]["workflow_dispatch"]["inputs"])
        self.assertNotIn("push", self.data["on"])

    def test_reads_only(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", self.job)

    def test_one_upload_per_tag_at_a_time(self):
        self.assertEqual(self.job["concurrency"]["cancel-in-progress"], False)
        self.assertIn("snapstore-", self.job["concurrency"]["group"])

    def test_the_tag_and_the_secret_come_through_env(self):
        env = self.job["env"]
        self.assertEqual(env["TAG"], "${{ github.event.release.tag_name || inputs.tag }}")
        self.assertEqual(env["SNAPCRAFT_STORE_CREDENTIALS"], "${{ secrets.SNAPCRAFT_STORE_CREDENTIALS }}")
        for step in self.job["steps"]:
            self.assertNotIn("${{", step.get("run", ""), step.get("name"))

    def test_every_action_is_pinned(self):
        for step in self.job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], PINNED)
