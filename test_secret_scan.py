"""The pre-push secret scan (secret_scan.py). Every secret-looking value is
assembled at run time, so this file itself passes the scan. The git tests
use a throwaway repository in a temp folder; nothing is pushed anywhere."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secret_scan  # noqa: E402
import test_ghost_sweeper  # noqa: E402  (not its classes: pytest would run them twice)

BARE_SECRETS = test_ghost_sweeper.TestCommandLineRedaction.BARE_SECRETS

NAMES = ("Dana" + "Q", "D9X7" + "~1")          # stand-ins for this machine's user name
NOREPLY = "123+someone" + "@users.noreply.github.com"


def _scan(text):
    return secret_scan.scan_text(text, "f.py:1", NAMES)


class ScanText(unittest.TestCase):
    def test_every_secret_shape(self):
        shapes = dict(BARE_SECRETS)
        shapes["private key"] = "-----BEGIN RSA " + "PRIVATE KEY-----"
        for kind, value in shapes.items():
            with self.subTest(kind):
                self.assertTrue(_scan(f'TOKEN = "{value}"'), kind)

    def test_personal_paths_and_names(self):
        for text in ("C:\\Users\\" + NAMES[0] + "\\AppData\\x.log",
                     "C:/Users/" + NAMES[0].lower() + "/x",
                     "%TEMP% is C:\\Users\\" + NAMES[1] + "\\AppData\\Local\\Temp"):
            with self.subTest(text):
                self.assertTrue(_scan(text))

    def test_any_real_looking_user_folder(self):
        # Not this machine's name: any name that is not a placeholder.
        self.assertTrue(_scan(r"log = r'C:" + r"\Users\someone\AppData\x.log'"))
        for fine in (r"C:\Users\<your-name>\Documents", r"C:\Users\you\AppData", r"C:\Users\x\a.lnk"):
            with self.subTest(fine):
                self.assertEqual(_scan(fine), [])

    def test_absolute_paths_outside_the_neutral_examples(self):
        for text in ("cwd = r'E:" + r"\clientSite\src'", "E:/" + "myStuff/x.py",
                     'open("D:' + '\\\\Taxes\\\\2026.pdf")'):
            with self.subTest(text):
                self.assertTrue(_scan(text))
        for fine in (r"E:\proj\server.js", r"C:\Program Files\nodejs\node.exe", r"C:\Windows\System32",
                     r"E:\demo\site", "E:\\", "https://example.com/a:b", r"[^\s:/@]+:)"):
            with self.subTest(fine):
                self.assertEqual(_scan(fine), [])

    def test_private_denylist_lives_outside_the_repo(self):
        listed = Path(tempfile.mkdtemp()) / "publish_denylist.txt"
        listed.write_text("# the owner's project names\nSecretProj" + "ect\n  side-gig  # client\n\n",
                          encoding="utf-8")
        words = secret_scan.denylist(listed)
        self.assertEqual(words, ("SecretProj" + "ect", "side-gig"))
        self.assertTrue(secret_scan.scan_text("cwd E:/demo/secretproj" + "ectV2", "f", (), words))
        self.assertTrue(secret_scan.scan_text("title = 'Side-Gig chat'", "f", (), words))
        self.assertEqual(secret_scan.scan_text("title = 'chat A'", "f", (), words), [])
        self.assertEqual(secret_scan.denylist(listed.with_name("absent.txt")), ())
        self.assertFalse(str(secret_scan.denylist_path()).startswith(str(HERE)))

    def test_emails(self):
        self.assertTrue(_scan("contact: someone.real@" + "gmail.com"))
        for fine in (NOREPLY, "noreply@" + "anthropic.com", "dev@" + "example.com",
                     "cursoragent@" + "cursor.com"):
            with self.subTest(fine):
                self.assertEqual(_scan(f"Co-Authored-By: X <{fine}>"), [])

    def test_ordinary_code_passes(self):
        for text in ('C:\\Users\\a\\AppData\\Local\\Temp\\alp_isolated_7',
                     "git checkout sk-feature-branch",
                     "host = '127.0.0.1:8000'",
                     "DanaQuest = 1  # a longer word is not the name",
                     'print("hello")'):
            with self.subTest(text):
                self.assertEqual(_scan(text), [])

    def test_allow_marker(self):
        self.assertEqual(_scan("me@" + "gmail.com  # secret-scan: allow"), [])

    def test_personal_names_skip_generic_ones(self):
        env = {"USERNAME": "runner", "USER": "", "TEMP": "C:\\Users\\" + NAMES[1] + "\\AppData\\Local\\Temp",
               "TMP": "", "LOCALAPPDATA": ""}
        from unittest import mock
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(secret_scan.Path, "home", return_value=Path("/home/root")):
            self.assertEqual(secret_scan.personal_names(), (NAMES[1],))


class AddedLines(unittest.TestCase):
    def test_only_added_lines_with_their_numbers(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -3,0 +4,2 @@\n+first\n+second\n@@ -10 +12 @@\n-old\n+new\n")
        self.assertEqual(list(secret_scan.added_lines(diff)),
                         [("x.py", 4, "first"), ("x.py", 5, "second"), ("x.py", 12, "new")])


class PrePushOnARealRepository(unittest.TestCase):
    """A throwaway repository with a bare 'remote' next to it."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.remote, self.repo = root / "remote.git", root / "work"
        self._git(root, "init", "-q", "--bare", str(self.remote))
        self._git(root, "init", "-q", str(self.repo))
        self._git(self.repo, "remote", "add", "origin", str(self.remote))
        self._commit("README.md", "hello\n")
        self._git(self.repo, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/main")

    def _git(self, cwd, *args, email=NOREPLY):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
               "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True).stdout.decode().strip()

    def _commit(self, name, text, email=NOREPLY):
        (self.repo / name).write_text(text, encoding="utf-8")
        self._git(self.repo, "add", "-f", name)
        self._git(self.repo, "commit", "-q", "-m", f"add {name}", email=email)
        return self._git(self.repo, "rev-parse", "HEAD")

    def _pre_push(self):
        local = self._git(self.repo, "rev-parse", "HEAD")
        remote = self._git(self.repo, "rev-parse", "origin/main")
        stdin = f"refs/heads/main {local} refs/heads/main {remote}\n"
        from unittest import mock
        with mock.patch.object(secret_scan, "personal_names", return_value=NAMES), \
             mock.patch("sys.stderr"):
            return secret_scan.main(["--pre-push"], stdin, cwd=str(self.repo))

    def test_a_clean_push_passes(self):
        self._commit("app.py", "print('ok')\n")
        self.assertEqual(self._pre_push(), 0)

    def test_a_key_in_a_new_line_blocks(self):
        self._commit("bot.py", 'TOKEN = "' + BARE_SECRETS["telegram"] + '"\n')
        self._commit("later.py", "print('fine')\n")      # the bad commit is not the tip
        self.assertEqual(self._pre_push(), 1)

    def test_a_personal_author_email_blocks(self):
        self._commit("app.py", "print('ok')\n", email="someone.real@" + "gmail.com")
        self.assertEqual(self._pre_push(), 1)

    def test_adding_config_json_blocks(self):
        self._commit("config.json", '{"dock": {"x": 1}}\n')
        self.assertEqual(self._pre_push(), 1)

    def test_a_personal_path_in_a_commit_message_blocks(self):
        self._commit("app.py", "print('ok')\n")
        self._git(self.repo, "commit", "-q", "--amend", "-m", "fix C:\\Users\\" + NAMES[0] + "\\x")
        self.assertEqual(self._pre_push(), 1)

    def test_a_new_branch_scans_only_what_the_remote_lacks(self):
        self._git(self.repo, "fetch", "-q", "origin")
        self._commit("app.py", "print('ok')\n")
        local = self._git(self.repo, "rev-parse", "HEAD")
        commits = secret_scan.commits_to_push(f"refs/heads/b {local} refs/heads/b {'0' * 40}\n",
                                              cwd=str(self.repo))
        self.assertEqual(commits, [local])


class PublishedFilesStayGeneric(unittest.TestCase):
    def test_every_tracked_file_passes_the_publish_scan(self):
        """The whole repository as it is now: no key shapes, no real user
        folder, no absolute path outside the neutral examples, no personal
        e-mail, no private file tracked - and, on the owner's machine, none of
        his private words or his user name. CI runs the generic checks only."""
        found = secret_scan.tracked_findings(secret_scan.personal_names(), secret_scan.denylist(),
                                             cwd=str(HERE))
        self.assertEqual([str(f) for f in found], [])

    def test_the_tracked_scan_catches_a_personal_fixture(self):
        # A copy of the repository's own scan on a throwaway repo with one bad fixture.
        root = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "test_x.py").write_text("CWD = r'C:" + r"\clientWork\shop'\n", encoding="utf-8")
        (root / "config.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-f", "test_x.py", "config.json"], cwd=root, check=True)
        found = [str(f) for f in secret_scan.tracked_findings((), (), cwd=str(root))]
        self.assertEqual(len(found), 2, found)

    def test_example_config_has_placeholder_protection_lists(self):
        import json
        example = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
        claude = example["claude"]
        for key in ("protected_chat_words", "protected_path_words", "protected_chat_folders"):
            self.assertIn(key, claude)
            self.assertTrue(all("my-important-project" in v.lower() or v == "MyImportantProject"
                                for v in claude[key]), claude[key])

    def test_the_hooks_run_the_scan(self):
        for name, mode in (("pre-push", "--pre-push"), ("pre-commit", "--staged")):
            with self.subTest(name):
                hook = (HERE / "githooks" / name).read_text(encoding="utf-8")
                self.assertTrue(hook.startswith("#!/bin/sh"))
                self.assertIn(f"secret_scan.py {mode}", hook)

    def test_workflow_actions_are_pinned_to_a_commit(self):
        import re
        for wf in sorted((HERE / ".github" / "workflows").glob("*.yml")):
            text = wf.read_text(encoding="utf-8")
            with self.subTest(wf.name):
                self.assertIn("permissions:", text)
                for ref in re.findall(r"uses:\s*([^\s#]+)", text):
                    self.assertRegex(ref, r"@[0-9a-f]{40}$", f"{ref} is not pinned to a full SHA")


if __name__ == "__main__":
    unittest.main()
