"""The APT repository: when the owner publishes a GitHub release, its .deb goes into a signed APT repository on
GitHub Pages, and a last job runs the README's one-line setup against it, as a person would.

The draft release stays the owner's to check; publishing it is the go for the repository too. The repository
holds one version, so only the newest release is published. The signing key is three secrets of the environment
apt-signing, which only main and v* tags may use; without them the job stops with a plain error, publishing
nothing. The build job (Linux workflow, deb) builds the same
kind of repository with a throwaway key on every build and installs from it, so the script is tested before a
release needs it."""

import html
import re
import unittest

try:
    import yaml  # PyYAML: only for checking the workflows, never needed by the app
except ImportError:
    yaml = None

from tests.test_release_workflow import PINNED, ROOT, WORKFLOWS, load

APT = WORKFLOWS / "apt.yml"
LINUX = WORKFLOWS / "linux.yml"
APT_DIR = ROOT / "packaging" / "apt"
SITE = "https://bahrambakhtiyar.github.io/Porthole/apt"


def one_line_setup(text: str) -> str:
    """The one-line command that adds the repository, from a page or a README."""
    (line,) = [line for line in text.splitlines() if "/etc/apt/sources.list.d/porthole.list" in line]
    line = re.sub(r"^\s*(<pre><code>)?", "", line)
    return html.unescape(re.sub(r"</code></pre>\s*$", "", line)).strip()


class AptWorkflowTextTest(unittest.TestCase):
    def setUp(self):
        self.text = APT.read_text(encoding="utf-8")

    def test_builds_the_repository_from_the_published_release_s_deb(self):
        self.assertIn("gh release download \"$TAG\" --pattern '*.deb' --dir dist", self.text)
        self.assertIn('if [[ "$count" -ne 1 ]]', self.text)
        self.assertIn("bash packaging/apt/build-repo.sh site/apt", self.text)

    def test_stops_plainly_without_the_signing_key(self):
        # All three: the README's key always has a passphrase, and without it gpg-agent fails with an unclear error.
        self.assertIn(
            'if [[ -z "$APT_SIGNING_KEY" || -z "$APT_SIGNING_KEY_ID" || -z "$APT_SIGNING_PASSPHRASE" ]]', self.text
        )
        self.assertIn("APT_SIGNING_PASSPHRASE aren't all set, so nothing was published.", self.text)

    def test_takes_only_a_version_tag(self):
        self.assertIn(r'[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]', self.text)

    def test_only_the_newest_release(self):
        # One version in the repository: an older tag or a pre-release would take the newest away from everyone.
        self.assertIn('latest="$(gh release view --json tagName --jq .tagName)"', self.text)
        self.assertIn('if [[ "$TAG" != "$latest" ]]', self.text)

    def test_runs_the_readme_s_own_setup_line(self):
        # Exactly what people are told, so a broken line fails here, not on someone's PC.
        self.assertIn("setup=\"$(grep -F '/etc/apt/sources.list.d/porthole.list' packaging/apt/README.md)\"", self.text)
        self.assertIn('bash -euo pipefail -c "$setup"', self.text)
        self.assertIn('"$setup" != *"$SITE"*', self.text)
        self.assertIn("APT::Get::Assume-Yes", self.text)  # apt asks before more than one package; nobody can answer
        self.assertIn("python3 packaging/smoke_test.py -- /usr/bin/porthole", self.text)
        self.assertNotIn("trusted=yes", self.text)  # apt must check the signature

    def test_waits_for_pages_to_serve_the_new_version(self):
        self.assertIn('grep -qx "Version: $want"', self.text)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class AptWorkflowStructureTest(unittest.TestCase):
    def setUp(self):
        self.data = load(APT)
        self.jobs = self.data["jobs"]

    def test_runs_when_a_release_is_published_or_by_hand(self):
        self.assertEqual(self.data["on"]["release"], {"types": ["published"]})
        self.assertIn("tag", self.data["on"]["workflow_dispatch"]["inputs"])
        self.assertNotIn("push", self.data["on"])
        self.assertNotIn("pull_request", self.data["on"])

    def test_checks_the_documented_address(self):
        self.assertEqual(self.jobs["check"]["env"]["SITE"], SITE)

    def test_build_then_deploy_then_check(self):
        self.assertEqual(list(self.jobs), ["build", "deploy", "check"])
        self.assertEqual(self.jobs["deploy"]["needs"], "build")
        self.assertEqual(self.jobs["check"]["needs"], "deploy")

    def test_one_publish_at_a_time_never_cancelled(self):
        self.assertEqual(self.data["concurrency"], {"group": "apt-pages", "cancel-in-progress": False})

    def test_only_the_deploy_job_can_write_and_only_to_pages(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", self.jobs["build"])
        self.assertNotIn("permissions", self.jobs["check"])
        self.assertEqual(self.jobs["deploy"]["permissions"], {"pages": "write", "id-token": "write"})
        self.assertEqual(self.jobs["deploy"]["environment"]["name"], "github-pages")

    def test_the_key_comes_from_the_locked_environment(self):
        # Environment secrets, which only main and v* tags may use: a run started from another branch gets no key.
        self.assertEqual(self.jobs["build"]["environment"], "apt-signing")
        self.assertNotIn("environment", self.jobs["check"])

    def test_the_secrets_reach_only_the_steps_that_need_them(self):
        self.assertNotIn("secrets.", str(self.data.get("env", {})))
        for name, job in self.jobs.items():
            self.assertNotIn("secrets.", str(job.get("env", {})), name)
            for step in job["steps"]:
                self.assertNotIn("secrets.", str(step.get("with", {})), f"{name}: {step.get('name')}")
                if "secrets." in str(step.get("env", {})):
                    self.assertEqual(name, "build")
                    self.assertIn(step["name"], ["Check the signing key and the tag", "Build the signed repository"])

    def test_the_tag_comes_through_env_never_into_a_command(self):
        for name, job in self.jobs.items():
            for step in job["steps"]:
                self.assertNotIn("${{", step.get("run", ""), f"{name}: {step.get('name')}")
        self.assertEqual(self.jobs["build"]["env"]["TAG"], "${{ github.event.release.tag_name || inputs.tag }}")
        self.assertEqual(self.jobs["check"]["env"]["TAG"], "${{ github.event.release.tag_name || inputs.tag }}")

    def test_checkouts_keep_no_credentials(self):
        for name, job in self.jobs.items():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["persist-credentials"], False, name)

    def test_every_action_is_pinned(self):
        for job in self.jobs.values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], PINNED)


class AptRepositoryTestedOnEveryBuildTest(unittest.TestCase):
    """The Linux workflow's deb job puts the .deb in a repository signed with a throwaway key and installs it from
    there with apt, so build-repo.sh is checked on every build, not first on a release day."""

    def setUp(self):
        text = LINUX.read_text(encoding="utf-8")
        self.deb = text[text.index("\n  deb:\n") : text.index("\n  appimage:\n")]

    def test_builds_a_signed_repository_with_a_throwaway_key(self):
        self.assertIn("reprepro", self.deb)
        self.assertIn("--quick-generate-key \"Porthole CI throwaway <ci@invalid>\" ed25519 sign 1d", self.deb)
        self.assertIn("APT_SIGNING_PASSPHRASE=ci-only", self.deb)  # the passphrase path of build-repo.sh too
        self.assertIn("bash packaging/apt/build-repo.sh /tmp/porthole-apt", self.deb)
        self.assertNotIn("secrets.", self.deb)

    def test_installs_from_it_with_apt_checking_the_signature(self):
        self.assertIn("[signed-by=/etc/apt/keyrings/porthole.asc] file:///tmp/porthole-apt stable main", self.deb)
        self.assertIn("sudo apt-get install -y porthole", self.deb)
        self.assertNotIn("apt-get install -y ./dist/", self.deb)
        self.assertNotIn("trusted=yes", self.deb)
        self.assertLess(self.deb.index("sudo apt-get install -y porthole"), self.deb.index("smoke_test.py"))


class AptPagesTest(unittest.TestCase):
    def test_the_setup_needs_no_curl(self):
        # A fresh Ubuntu desktop has wget but no curl.
        readme = one_line_setup((APT_DIR / "README.md").read_text(encoding="utf-8"))
        self.assertIn(f"sudo wget -qO /etc/apt/keyrings/porthole.asc {SITE}/porthole.asc", readme)
        self.assertNotIn("curl", readme)

    def test_the_key_is_made_outside_the_repository(self):
        text = (APT_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn('umask 077 && cd "$(mktemp -d)"', text)
        secret_lines = [line for line in text.splitlines() if line.startswith("gh secret set")]
        self.assertEqual(len(secret_lines), 4)  # three when the key is made, one when it is renewed
        for line in secret_lines:
            self.assertIn(" --env apt-signing ", line)  # environment secrets, never repository-wide ones
        self.assertNotIn('cat porthole-apt-private.asc', text)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("porthole-apt-private.asc", ignored)
        self.assertIn("porthole-apt-revoke.rev", ignored)

    def test_one_command_everywhere(self):
        readme = one_line_setup((APT_DIR / "README.md").read_text(encoding="utf-8"))
        page = one_line_setup((APT_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(page, readme)
        self.assertIn(f"{SITE}/porthole.asc", readme)
        self.assertIn(f"deb [signed-by=/etc/apt/keyrings/porthole.asc] {SITE} stable main", readme)

    def test_the_page_loads_nothing_from_elsewhere(self):
        text = (APT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"<script|<link[^>]+stylesheet|src=\"https?:")
        self.assertIn('<meta name="viewport"', text)
        self.assertIn("<title>Porthole APT repository</title>", text)

    def test_build_repo_still_keeps_the_key_out_of_the_home_folder(self):
        text = (APT_DIR / "build-repo.sh").read_text(encoding="utf-8")
        self.assertIn('export GNUPGHOME="$work/gnupg"', text)
        self.assertIn("trap cleanup EXIT", text)


if __name__ == "__main__":
    unittest.main()
