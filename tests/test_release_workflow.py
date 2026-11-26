"""The draft GitHub release: one workflow makes it, once the Linux build of the tag has passed.

The build workflow only builds, and release.yml waits for its run of the tag, then makes the draft with the
.deb, the AppImage and the snap in one go. (Separate release jobs sharing a concurrency group lost files with
three build workflows: GitHub cancels the older pending job of a group. The Windows and macOS builds are parked
on the private repository's archive/windows-macos branch; its copy of release.yml waits for all three.)"""

import re
import unittest
from pathlib import Path

try:
    import yaml  # PyYAML: only for checking the workflows, never needed by the app
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
BUILD = "linux.yml"
LINUX_FILES = ["*.deb", "*.AppImage", "*.snap"]
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML reads a bare `on:` key as True
    return data


class ReleaseWorkflowTextTest(unittest.TestCase):
    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_waits_for_the_linux_build_of_this_tag(self):
        self.assertIn("workflow=linux.yml", self.text)
        self.assertIn("gh run list", self.text)
        self.assertIn("--commit", self.text)
        self.assertIn("headBranch", self.text)  # the tag's run, not the branch push of the same commit
        self.assertIn("--event push", self.text)  # never a pull request's run, which builds no packages

    def test_only_the_linux_build(self):
        self.assertEqual(sorted(path.name for path in WORKFLOWS.glob("*.yml")), ["linux.yml", "release.yml", "snapstore.yml"])
        self.assertNotRegex(self.text, r"(?i)windows|macos|\.exe\b|\.zip\b")

    def test_a_failed_api_call_while_waiting_is_tried_again(self):
        # Under `set -e` one failed `gh run list` (a 502, a blip) would end the whole wait.
        self.assertIn('if ! line="$(gh run list', self.text)

    def test_one_deadline_and_an_early_error_for_a_missing_run(self):
        # The script's own error must come before the job's time limit, and a run that hasn't appeared after a
        # few minutes never will (GitHub makes the runs when the tag is pushed).
        self.assertIn("deadline=$((SECONDS", self.text)
        self.assertIn("never started", self.text)

    def test_a_failed_build_means_no_draft(self):
        self.assertIn('!= success', self.text)

    def test_downloads_the_build_and_checks_exactly_the_three_linux_files(self):
        self.assertIn('gh run download "$BUILD_RUN"', self.text)
        loops = re.findall(r"^\s*for pattern in (.+); do$", self.text, re.MULTILINE)
        self.assertEqual(len(loops), 1)
        self.assertEqual(re.findall(r"'([^']+)'", loops[0]), LINUX_FILES)
        self.assertIn('if [[ "$total" -ne 3 ]]', self.text)  # and nothing else

    def test_makes_one_draft(self):
        self.assertIn("gh release create", self.text)
        self.assertIn("--draft", self.text)
        self.assertIn("--verify-tag", self.text)


@unittest.skipUnless(yaml, "PyYAML not installed")
class ReleaseWorkflowStructureTest(unittest.TestCase):
    def test_runs_on_tags_only(self):
        data = load(RELEASE)
        self.assertEqual(data["on"], {"push": {"tags": ["v*"]}})

    def test_writes_only_in_its_job(self):
        data = load(RELEASE)
        self.assertEqual(data["permissions"], {"contents": "read"})
        (job,) = data["jobs"].values()
        self.assertEqual(job["permissions"], {"contents": "write", "actions": "read"})

    def test_the_wait_outlasts_the_linux_build_and_ends_before_the_job(self):
        # The Linux workflow's longest path: its tests, then the slowest package job. The wait must allow at
        # least that (plus queueing), and end with its own error before the job's limit cuts it off.
        jobs = load(WORKFLOWS / BUILD)["jobs"]
        packages = [job["timeout-minutes"] for name, job in jobs.items() if job.get("needs") == "tests"]
        longest = jobs["tests"]["timeout-minutes"] + max(packages)
        (job,) = load(RELEASE)["jobs"].values()
        (wait,) = [int(m) for m in re.findall(r"deadline=\$\(\(SECONDS \+ (\d+) \* 60\)\)", job["steps"][0]["run"])]
        self.assertGreaterEqual(wait, longest + 10)
        self.assertGreater(job["timeout-minutes"], wait)
        self.assertLessEqual(job["timeout-minutes"], 120)  # Linux only: no waiting for the slow macOS builds

    def test_two_release_runs_for_one_tag_never_overlap(self):
        (job,) = load(RELEASE)["jobs"].values()
        self.assertEqual(job["concurrency"], {"group": "release-${{ github.ref }}", "cancel-in-progress": False})

    def test_every_action_is_pinned(self):
        for step in next(iter(load(RELEASE)["jobs"].values()))["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], PINNED)

    def test_the_build_workflow_doesnt_release(self):
        data = load(WORKFLOWS / BUILD)
        self.assertNotIn("release", data["jobs"])
        text = (WORKFLOWS / BUILD).read_text(encoding="utf-8")
        self.assertNotIn("contents: write", text)
        self.assertNotIn("gh release create", text)
        self.assertNotIn("gh release upload", text)
        self.assertNotIn("concurrency:", text)

    def test_the_build_uploads_the_three_files(self):
        jobs = load(WORKFLOWS / BUILD)["jobs"]
        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for job in jobs.values()
            for step in job["steps"]
            if "upload-artifact" in step.get("uses", "")
        }
        self.assertEqual(set(uploads), {"deb", "appimage", "snap"})
        self.assertTrue(uploads["deb"].endswith("*.deb"))
        self.assertTrue(uploads["appimage"].endswith("*.AppImage"))


if __name__ == "__main__":
    unittest.main()
