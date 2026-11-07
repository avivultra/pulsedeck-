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
                     "%TEMP% is C:\\Users\\" + NAMES[1] + "\\AppData\\Local\\Temp",
                     "E:\\flow" + "-secrets\\telegram.txt"):
            with self.subTest(text):
                self.assertTrue(_scan(text))

    def test_emails(self):
        self.assertTrue(_scan("contact: someone.real@" + "gmail.com"))
        for fine in (NOREPLY, "noreply@" + "anthropic.com", "dev@" + "example.com",
                     "cursoragent@" + "cursor.com"):
            with self.subTest(fine):
                self.assertEqual(_scan(f"Co-Authored-By: X <{fine}>"), [])

    def test_ordinary_code_passes(self):
        for text in ('C:\\Users\\a\\AppData\\Local\\Temp\\flow_isolated_7',
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
    def test_example_config_has_placeholder_protection_lists(self):
        import json
        example = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
        claude = example["claude"]
        for key in ("protected_chat_words", "protected_path_words", "protected_chat_folders"):
            self.assertIn(key, claude)
        self.assertNotIn("flowstream", json.dumps(claude).lower())

    def test_the_hook_runs_the_scan(self):
        hook = (HERE / "githooks" / "pre-push").read_text(encoding="utf-8")
        self.assertTrue(hook.startswith("#!/bin/sh"))
        self.assertIn("secret_scan.py --pre-push", hook)


if __name__ == "__main__":
    unittest.main()
