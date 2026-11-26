"""tools/export_public.sh, which makes the public repository's first commit. It is tried on throwaway repositories in
a temporary folder, never on this one: it must leave the private notes out, refuse a copy that still names one (in any
case) or has a file under private/, a symbolic link, a machine path or a real home folder (Linux, macOS or Windows
style), or a personal email address, in text and binary files alike, and make exactly one commit under the GitHub
no-reply address, without ever adding a remote or pushing. The steps that then turn the owner's folder into the
public repository's working copy are run here too, on a throwaway copy, so that no private history stays behind."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.private_docs import GOING_PUBLIC, NOTES, private_doc

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "export_public.sh"
GIT = shutil.which("git")
BASH = shutil.which("bash")
NOREPLY = "64256223+bahrambakhtiyar@users.noreply.github.com"
AUTHOR = f"Bahram Bakhtiyar <{NOREPLY}>"
MESSAGE = "Porthole 0.1.0: share your screen on your Wi-Fi"
PUBLIC_URL = "https://github.com/bahrambakhtiyar/Porthole.git"
# Put together from pieces, so this file passes the checks it tests when the tool exports the repository it is in.
SOMEONES_EMAIL = "zaur" + "@" + "example.com"
UPSTREAM_EMAIL = "author" + "@" + "zlib.example"
REAL_HOME = "/home/" + "zaur/porthole"
MOUNT = "/m" + "nt/d/porthole"
SESSION = "/t" + "mp/claude-1000/notes"
WINDOWS_HOME = "C:\\" + "Users\\Zaur's PC\\porthole\\share.py"  # as a Windows program writes it
ESCAPED_HOME = "'C:\\\\" + "Users\\\\Zaur''s PC'"  # as a string in code or SQL writes it
WSL_HOME = "\\\\wsl$\\Ubuntu\\" + "home\\zaur\\porthole"
NON_ASCII_HOME = "/home/" + "\u018fli/porthole"
WINDOWS_PC = "D:" + "\\Desktop\\My Projects\\porthole"
FORWARD_PC = "D:" + "/Desktop/porthole"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
FAKE_GIT = """#!/bin/sh
printf '%s\\n' "$@" --- >> "$GIT_LOG"
exec "$REAL_GIT" "$@"
"""


def step_4_blocks(text: str) -> list[str]:
    """The bash blocks of step 4 in section 3 of the steps for going public: the checks, then the switch."""
    start = text.index("\n## 3.")
    section = text[start : text.index("\n## ", start + 1)]
    step = section[section.index("\n4. ") : section.index("\n5. ")]
    return [textwrap.dedent(block) for block in re.findall(r"^ *```bash\n(.*?)^ *```$", step, re.S | re.M)]


@unittest.skipUnless(GIT and BASH and sys.platform != "win32", "needs git and bash (Linux or macOS)")
class ExportPublicTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "git.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(FAKE_GIT)
        (bin_dir / "git").chmod(0o755)
        # Every git command the tool runs goes through the fake, which writes it down; the user's own git settings
        # stay out of it.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_LOG=str(self.log), REAL_GIT=GIT,
                        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def git(self, cwd: Path, *args: str) -> str:
        env = {**self.env, "GIT_AUTHOR_NAME": "Zaur", "GIT_AUTHOR_EMAIL": SOMEONES_EMAIL,
               "GIT_COMMITTER_NAME": "Zaur", "GIT_COMMITTER_EMAIL": SOMEONES_EMAIL, "PATH": os.environ.get("PATH", "")}
        result = subprocess.run([GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_repo(self, extra: dict | None = None) -> Path:
        """A throwaway repository with the tool, the list of private notes, this test, public files, the private
        notes in docs/, and files git doesn't track."""
        repo = self.tmp / "private-repo"
        files = {
            "tools/export_public.sh": TOOL.read_bytes(),
            "tests/private_docs.py": (ROOT / "tests" / "private_docs.py").read_bytes(),
            "tests/test_export_public.py": Path(__file__).read_bytes(),
            "README.md": f"# Demo\n\nRun it from /home/ayla/demo. Contact: {NOREPLY}\n",
            "docs/design.md": "How it works.\n",
            "packaging/appimage/THIRD-PARTY-NOTICES.txt": f"Copyright (C) 1995 Someone <{UPSTREAM_EMAIL}>\n",
            "assets/icon.png": PNG,
            "start.sh": "#!/bin/sh\necho hi\n",
            ".gitignore": "/dist/\n*.log\n",
            **{f"docs/{name}": f"Private: see {' and '.join(NOTES)}, {SOMEONES_EMAIL}, {MOUNT}.\n" for name in NOTES},
            **(extra or {}),
        }
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        (repo / "start.sh").chmod(0o755)
        self.git(repo, "init", "-q", "-b", "work")
        self.git(repo, "add", "-A")
        (repo / "kept.log").write_text("tracked although ignored\n")
        self.git(repo, "add", "-f", "kept.log")
        self.git(repo, "commit", "-q", "-m", "Private history")
        (repo / "untracked.txt").write_text(f"not tracked: {SOMEONES_EMAIL}\n")
        (repo / "dist").mkdir()
        (repo / "dist" / "porthole.deb").write_text("ignored\n")
        return repo

    def export(self, repo: Path, target: Path | None = None, **env) -> tuple[subprocess.CompletedProcess, Path]:
        target = target or self.tmp / "public"
        result = subprocess.run([BASH, str(repo / "tools" / "export_public.sh"), str(target)], cwd=self.tmp,
                                env={**self.env, **env}, capture_output=True, text=True, timeout=120)
        return result, target

    def returncode(self, cwd: Path, *args: str) -> int:
        return subprocess.run([GIT, *args], cwd=cwd, env=self.env, capture_output=True, timeout=60).returncode

    def git_commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        commands, current = [], []
        for line in self.log.read_text().splitlines():
            if line == "---":
                commands.append(current)
                current = []
            else:
                current.append(line)
        return commands

    def assert_refused(self, result: subprocess.CompletedProcess, target: Path, *expected: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("refused, and nothing was written", result.stderr)
        for words in expected:
            self.assertIn(words, result.stderr)
        self.assertFalse(target.exists())

    def problems(self, result: subprocess.CompletedProcess, file: str) -> list[str]:
        """What the refusal says about `file`, one line per problem."""
        lines = [line.strip() for line in result.stderr.splitlines()]
        return [line for line in lines if line.startswith(f"{file}:")]

    def assert_problem(self, result: subprocess.CompletedProcess, file: str, why: str) -> None:
        found = self.problems(result, file)
        self.assertTrue(found, result.stderr)
        self.assertTrue(all(line.endswith(f"({why})") for line in found), found)

    def test_one_commit_by_the_no_reply_address_without_the_private_notes(self):
        repo = self.make_repo()
        head = self.git(repo, "rev-parse", "HEAD")
        # An author in the environment doesn't win over the no-reply address.
        result, target = self.export(repo, GIT_AUTHOR_EMAIL=SOMEONES_EMAIL, GIT_COMMITTER_EMAIL=SOMEONES_EMAIL)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in NOTES:
            self.assertFalse((target / "docs" / name).exists(), name)
        tracked = set(self.git(repo, "ls-files").splitlines())
        self.assertEqual(set(self.git(target, "ls-files").splitlines()), tracked - {f"docs/{name}" for name in NOTES})
        self.assertIn("kept.log", tracked)  # tracked although .gitignore names it: it comes along
        self.assertFalse((target / "untracked.txt").exists())
        self.assertFalse((target / "dist").exists())
        self.assertEqual((target / "assets" / "icon.png").read_bytes(), PNG)
        self.assertEqual((target / "README.md").read_bytes(), (repo / "README.md").read_bytes())
        self.assertTrue(self.git(target, "ls-files", "-s", "start.sh").startswith("100755 "))
        self.assertEqual(self.git(target, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git(target, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s"), f"{AUTHOR}|{AUTHOR}|{MESSAGE}")
        self.assertEqual(self.git(target, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(target, "config", "user.email"), NOREPLY)
        self.assertEqual(self.git(target, "remote"), "")
        self.assertEqual(self.git(target, "status", "--porcelain"), "")
        # It prints the next steps for the owner and runs none of them.
        self.assertIn("Nothing was pushed", result.stdout)
        self.assertIn(f"git remote add origin {PUBLIC_URL}\n", result.stdout)
        self.assertIn("git push -u origin main\n", result.stdout)
        commands = self.git_commands()
        self.assertTrue(commands)
        for args in commands:
            self.assertNotIn("push", args)
            self.assertFalse(any(a == "remote" and b in ("add", "set-url") for a, b in zip(args, args[1:])), args)
        # The private repository is as it was.
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(repo, "remote"), "")
        self.assertEqual(self.git(repo, "branch", "--format=%(refname:short)"), "work")

    def test_uncommitted_changes_stay_out(self):
        repo = self.make_repo()
        (repo / "README.md").write_text("# Changed, not committed\n")
        result, target = self.export(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertIn("Run it from /home/ayla/demo", (target / "README.md").read_text())

    def test_refuses_a_file_that_still_names_a_private_note(self):
        note, stem = NOTES[-1], NOTES[-1].removesuffix(".md")
        repo = self.make_repo({"README.md": f"# Demo\n\nThe steps are in docs/{note}.\n",
                               "docs/design.md": f"How it works.\nSee {stem}, section 3.\n"})
        result, target = self.export(repo)
        self.assert_refused(result, target, f"README.md:3:{note} (names a private note)",
                            f"docs/design.md:2:{stem} (names a private note)")
        self.assertNotIn("tests/private_docs.py", result.stderr)  # the list itself may name them

    def test_refuses_machine_paths_real_home_folders_and_personal_email(self):
        repo = self.make_repo({"a.txt": f"cd {REAL_HOME}\n", "b.txt": f"open {MOUNT}/start.py\n",
                               "c.txt": f"saved in {SESSION}\n", "d.txt": f"Write to {SOMEONES_EMAIL}.\n"})
        result, target = self.export(repo)
        self.assert_refused(result, target, "a.txt:1:", "(a home folder)", "b.txt:1:", "c.txt:1:", "(a path on this PC)",
                            f"d.txt:1:{SOMEONES_EMAIL} (an email address)")
        # The tests' made-up home folders, the no-reply address and the third-party notices pass.
        for allowed in ("README.md", "THIRD-PARTY-NOTICES.txt", "untracked.txt", "tests/test_export_public.py"):
            self.assertNotIn(allowed, result.stderr)

    def test_refuses_windows_home_folders_and_paths_on_this_pc(self):
        repo = self.make_repo({"e.py": f'Path("{WINDOWS_HOME}")\n', "f.sql": f"SELECT {ESCAPED_HOME};\n",
                               "g.txt": f"{WSL_HOME}\n", "h.txt": f"{NON_ASCII_HOME}\n",
                               "i.txt": f"open {WINDOWS_PC}\n", "j.txt": f"open {FORWARD_PC}\n"})
        result, target = self.export(repo)
        self.assert_refused(result, target)
        for file in ("e.py", "f.sql", "g.txt", "h.txt"):
            self.assert_problem(result, file, "a home folder")
        for file in ("i.txt", "j.txt"):
            self.assert_problem(result, file, "a path on this PC")

    def test_made_up_and_ci_home_folders_program_folders_and_urls_pass(self):
        readme = ("# Demo\n\nC:\\Users\\Ayla's PC\\porthole, C:\\\\Users\\\\runner\\\\work, /home/runner/work,\n"
                  "/Users/Ayla, C:\\Program Files\\Porthole, D:\\Apps\\chrome.exe, https://claude.ai/code\n")
        result, target = self.export(self.make_repo({"README.md": readme}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "README.md").read_text(), readme)

    def test_refuses_a_note_named_in_another_case(self):
        note, stem = NOTES[3].upper(), NOTES[4].removesuffix(".md").title()
        repo = self.make_repo({"README.md": f"# Demo\n\nSee {note} and {stem}.\n",
                               f"old/{NOTES[0].upper()}": "An old plan.\n"})
        result, target = self.export(repo)
        self.assert_refused(result, target, f"README.md:3:{note} (names a private note)",
                            f"README.md:3:{stem} (names a private note)",
                            f"old/{NOTES[0].upper()}: a copy of the private note {NOTES[0]}")

    def test_refuses_symbolic_links_and_reads_binary_files(self):
        # git keeps a link's target as its content, and grep -r neither follows links nor reads that target; a GIF
        # comment (or a PNG text chunk) can hold a path or an address behind a NUL byte.
        gif = b"GIF89a\x01\x00\x01\x00\x00\x00\x00!\xfe" + f"by {SOMEONES_EMAIL} in {REAL_HOME}".encode() + b"\x00;"
        repo = self.make_repo({"assets/shot.gif": gif})
        os.symlink(MOUNT + "/secret.txt", repo / "link")
        os.symlink("../README.md", repo / "docs" / "readme")
        self.git(repo, "add", "link", "docs/readme")
        self.git(repo, "commit", "-q", "-m", "Links")
        result, target = self.export(repo)
        self.assert_refused(result, target)
        link = "link: a symbolic link (git would publish its target as it is; the project has none)"
        self.assertEqual(self.problems(result, "link"), [link])
        self.assertEqual(len(self.problems(result, "docs/readme")), 1)
        found = self.problems(result, "assets/shot.gif")
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any(line.endswith("(a home folder)") for line in found), found)
        self.assertTrue(any(line.endswith("(an email address)") for line in found), found)

    def test_refuses_a_file_under_private_or_a_copy_of_a_note(self):
        repo = self.make_repo({"private/todo.md": "A private list.\n", f"old/{NOTES[0]}": "An old plan.\n"})
        result, target = self.export(repo)
        self.assert_refused(result, target, "private/todo.md: a file under private/",
                            f"old/{NOTES[0]}: a copy of the private note {NOTES[0]}")

    def test_refuses_a_folder_that_isnt_empty_or_is_inside_the_repository(self):
        repo = self.make_repo()
        full = self.tmp / "full"
        full.mkdir()
        (full / "keep.txt").write_text("mine\n")
        result, _ = self.export(repo, full)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isn't empty", result.stderr)
        self.assertEqual(os.listdir(full), ["keep.txt"])
        result, _ = self.export(repo, repo / "public")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the repository", result.stderr)
        self.assertFalse((repo / "public").exists())
        result, _ = self.export(repo, Path("--help"))  # an option, not a folder
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage: bash tools/export_public.sh", result.stderr)
        self.assertFalse((self.tmp / "--help").exists())
        empty = self.tmp / "empty"
        empty.mkdir()
        result, _ = self.export(repo, empty)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_folder_switches_to_the_public_repository_without_its_private_history(self):
        # Step 4 of section 3 in the steps for going public, run as written on a throwaway copy of the owner's folder:
        # afterwards no branch, remote-tracking ref, reflog or worktree there can reach a private commit, so no push
        # from it can publish one.
        doc = private_doc(GOING_PUBLIC)
        if doc is None:
            self.skipTest(f"{GOING_PUBLIC} is one of the project's private notes, and this copy doesn't have it")
        blocks = step_4_blocks(doc.read_text(encoding="utf-8"))
        self.assertEqual(len(blocks), 2, blocks)
        check, switch = blocks
        for block in blocks:  # they run in the folder they are given, here a throwaway one
            self.assertNotRegex(block, r"\b(cd|rm|sudo|push)\b")

        repo = self.make_repo({".gitignore": "/dist/\n*.log\n/private/\n"})
        (repo / "untracked.txt").unlink()
        self.git(repo, "branch", "-m", "work", "main")
        # Its private history: a branch that tracks one of the same name, an archive branch, remote-tracking refs, a
        # commit only the reflog still has, and a worktree.
        self.git(repo, "remote", "add", "origin", str(self.tmp / "private.git"))  # never contacted
        self.git(repo, "switch", "-q", "-c", "work/old")
        (repo / "docs" / "design.md").write_text(f"The old design, from {MOUNT}.\n")
        self.git(repo, "commit", "-q", "-am", "Old work")
        self.git(repo, "commit", "-q", "--amend", "-m", "Old work, reworded")
        self.git(repo, "switch", "-q", "main")
        self.git(repo, "branch", "archive/old", "work/old")
        for branch in ("main", "work/old", "archive/old"):
            self.git(repo, "update-ref", f"refs/remotes/origin/{branch}", branch)
        self.git(repo, "branch", "-q", "-u", "origin/main", "main")
        self.git(repo, "branch", "-q", "-u", "origin/work/old", "work/old")
        worktree = self.tmp / "worktree"
        self.git(repo, "worktree", "add", "-q", str(worktree), "archive/old")
        private = set(self.git(repo, "rev-list", "--all", "--reflog").split())
        self.assertEqual(len(private), 3)
        head = self.git(repo, "rev-parse", "HEAD")

        # The public repository on GitHub: the export, pushed. The public address leads to it, and git may use no
        # other protocol than local files, so nothing here reaches the network.
        result, target = self.export(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        public = self.tmp / "public.git"
        self.git(self.tmp, "clone", "-q", "--bare", str(target), str(public))
        config = self.tmp / "gitconfig"
        config.write_text(f'[url "{public}"]\n\tinsteadOf = {PUBLIC_URL}\n')
        env = {**self.env, "GIT_CONFIG_GLOBAL": str(config), "GIT_ALLOW_PROTOCOL": "file"}

        def run(block: str) -> subprocess.CompletedProcess:
            return subprocess.run([BASH, "-e", "-u", "-c", block], cwd=repo, env=env, capture_output=True, text=True,
                                  timeout=120)

        result = run(check)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[archive/old]", result.stdout)  # the worktree it says to remove first
        self.assertEqual(len(result.stdout.splitlines()), 2, result.stdout)  # and no commit only this folder has
        # The worktree is left: even then it can't reach the history afterwards.
        result = run(switch)
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = result.stdout.split()
        if "refs/remotes/origin/HEAD" in printed:  # newer git sets it on the first fetch
            printed.remove("refs/remotes/origin/HEAD")
            self.assertEqual(self.git(repo, "symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")
        self.assertEqual(printed, ["1", "refs/heads/main", "refs/remotes/origin/main"])

        self.assertEqual(self.git(repo, "rev-list", "--all", "--reflog", "--count"), "1")
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.git(public, "rev-parse", "main"))
        for commit in private:
            self.assertNotEqual(self.returncode(repo, "cat-file", "-e", commit), 0, commit)
        self.assertEqual(self.git(repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertNotEqual(self.returncode(worktree, "rev-parse", "HEAD"), 0)
        self.assertEqual(self.git(repo, "config", "remote.origin.url"), PUBLIC_URL)
        self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}"), "origin/main")
        self.assertEqual(self.git(repo, "config", "user.email"), NOREPLY)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        for name in NOTES:
            self.assertFalse((repo / "docs" / name).exists(), name)
            self.assertTrue((repo / "private" / name).is_file(), name)
        self.assertTrue((repo / "dist" / "porthole.deb").is_file())  # ignored files stay as they were
        # The backup next to the folder has all of the private history.
        backup = self.tmp / "porthole-private.git"
        self.assertEqual(self.git(self.tmp, "--git-dir", str(backup), "rev-parse", "main"), head)
        self.assertEqual(set(self.git(self.tmp, "--git-dir", str(backup), "rev-list", "--all", "--reflog").split()),
                         private)
        for args in self.git_commands():
            self.assertNotIn("push", args)

    def test_the_script_only_prints_the_push(self):
        # Every line that mentions a push or a new remote only prints it.
        for line in TOOL.read_text(encoding="utf-8").splitlines():
            if re.search(r"git (push|remote add|remote set-url)", line):
                self.assertRegex(line.strip(), r"^(echo|printf|#)", line)


if __name__ == "__main__":
    unittest.main()
