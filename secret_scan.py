"""Stop a push that would publish a secret or something personal.

The repository is public. Run as git's pre-push hook (githooks/pre-push), it
reads what is about to be pushed - the lines each new commit adds, the commit
messages, the author / committer e-mails - and blocks the push on:

* a key or token by its shape (secret_shapes: Anthropic, GitHub, Slack, AWS,
  Google, JWT, Telegram bot token, private key);
* this machine's own user folder or user name (C:\\Users\\<you>, its short
  8.3 form), and the local secrets folder;
* an e-mail address other than a GitHub noreply / example one, in a file or as
  a commit's author or committer;
* config.json or .env being added (they hold the owner's settings and keys;
  config.example.json / .env.example are the published versions).

A line that really must stay can carry the words "secret-scan: allow".
`git push --no-verify` skips the hook - for the owner, deliberately.

Manual use:  python secret_scan.py [<revision range>]   (default: unpushed commits)
Standard library only.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from secret_shapes import NAMED_SHAPES

ALLOW = "secret-scan: allow"
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_FINE_EMAIL = re.compile(
    r"(?i)(?:@users\.noreply\.github\.com|^[a-z0-9.\-]*(?:no-?reply|bot|agent)@[a-z0-9.\-]+|"
    r"@example\.(?:com|org|net)|@localhost)$")     # tool co-authors: noreply@, cursoragent@
_SECRETS_DIR = re.compile(r"(?i)\bflow" + r"-secrets\b")
_PRIVATE_FILES = {"config.json", ".env"}


@dataclass(frozen=True)
class Finding:
    where: str          # "file.py:12", "commit abc1234 message", "commit abc1234 author"
    what: str

    def __str__(self) -> str:
        return f"{self.where}: {self.what}"


def personal_names() -> tuple[str, ...]:
    """This machine's user name in the forms it shows up in paths: the home
    folder's name, %USERNAME%, and the short 8.3 name Windows puts in %TEMP%."""
    names = {Path.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    for var in ("TEMP", "TMP", "LOCALAPPDATA"):
        m = re.search(r"(?i)[\\/]users[\\/]([^\\/]+)", os.environ.get(var, ""))
        if m:
            names.add(m.group(1))
    generic = {"", "user", "users", "runner", "root", "admin", "public", "default"}
    return tuple(sorted(n for n in names if len(n) >= 3 and n.lower() not in generic))


def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?i)(?<![\w~])(?:{alts})(?![\w~])")


def scan_text(text: str, where: str, names: tuple[str, ...]) -> list[Finding]:
    """Findings in one piece of text (a line, a commit message)."""
    if ALLOW in text:
        return []
    found = [Finding(where, f"looks like a {name}")
             for name, shape in NAMED_SHAPES if shape.search(text)]
    if _SECRETS_DIR.search(text):
        found.append(Finding(where, "names the local secrets folder"))
    pattern = _name_pattern(names)
    if pattern and pattern.search(text):
        found.append(Finding(where, "contains this computer's user name (a personal path?)"))
    for email in _EMAIL.findall(text):
        if not _FINE_EMAIL.search(email):
            found.append(Finding(where, f"contains an e-mail address ({_mask(email)})"))
    return found


def _mask(email: str) -> str:
    user, _, domain = email.partition("@")
    return f"{user[:2]}...@{domain}"


def added_lines(diff: str):
    """(path, line number, text) of every added line in a unified diff."""
    path, number = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            number = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            yield path, number, line[1:]
            number += 1
        elif not line.startswith("-"):
            number += 1


def _git(*args: str, cwd: str | None = None) -> str:
    out = subprocess.run(["git", *args], capture_output=True, cwd=cwd, check=True)
    return out.stdout.decode("utf-8", "replace")


def scan_commits(commits: list[str], names: tuple[str, ...], cwd: str | None = None) -> list[Finding]:
    found: list[Finding] = []
    for sha in commits:
        short = sha[:7]
        meta = _git("show", "-s", "--format=%ae%x00%ce%x00%B", sha, cwd=cwd)
        author, committer, message = (meta.split("\x00") + ["", "", ""])[:3]
        for role, email in (("author", author.strip()), ("committer", committer.strip())):
            if email and not _FINE_EMAIL.search(email):
                found.append(Finding(f"commit {short} {role}",
                                     f"a personal e-mail ({_mask(email)}); use the GitHub noreply one"))
        found += scan_text(message, f"commit {short} message", names)
        changes = _git("show", "--format=", "--name-status", "--no-renames", sha, cwd=cwd)
        for row in changes.splitlines():
            status, _, path = row.partition("\t")
            if status.startswith("A") and Path(path).name in _PRIVATE_FILES:
                found.append(Finding(f"commit {short}", f"adds {path}, which is private (gitignored)"))
        diff = _git("show", "--format=", "--unified=0", "--no-color", "--no-ext-diff", sha, cwd=cwd)
        for path, number, text in added_lines(diff):
            found += scan_text(text, f"{path}:{number} (commit {short})", names)
    return found


def commits_to_push(stdin_text: str, cwd: str | None = None) -> list[str]:
    """The new commits git's pre-push hook is told about on stdin."""
    commits: list[str] = []
    for row in stdin_text.splitlines():
        parts = row.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue                       # malformed, or a branch deletion
        local, remote = parts[1], parts[3]
        new_only = [local, "--not", "--remotes"]
        try:
            spec = new_only if remote == ZERO else [f"{remote}..{local}"]
            listed = _git("rev-list", "--reverse", *spec, cwd=cwd).split()
        except subprocess.CalledProcessError:   # the remote's tip is not here (force push)
            listed = _git("rev-list", "--reverse", *new_only, cwd=cwd).split()
        commits += [c for c in listed if c not in commits]
    return commits


def main(argv: list[str], stdin_text: str = "", cwd: str | None = None) -> int:
    names = personal_names()
    if argv[:1] == ["--pre-push"]:
        commits = commits_to_push(stdin_text, cwd)
    else:
        spec = argv[0] if argv else "HEAD"
        extra = [] if argv else ["--not", "--remotes"]
        commits = _git("rev-list", "--reverse", spec, *extra, cwd=cwd).split()
    found = scan_commits(commits, names, cwd)
    if not found:
        return 0
    print(f"secret-scan: push blocked - {len(found)} finding(s) in {len(commits)} commit(s):",
          file=sys.stderr)
    for f in found:
        print(f"  {f}", file=sys.stderr)
    print(f'Fix them (or mark a line that must stay with "{ALLOW}") and push again.',
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.stderr.reconfigure(errors="replace")   # Hebrew paths on a code-page console
    except (AttributeError, ValueError):
        pass
    stdin = sys.stdin.read() if sys.argv[1:2] == ["--pre-push"] else ""
    sys.exit(main(sys.argv[1:], stdin))
