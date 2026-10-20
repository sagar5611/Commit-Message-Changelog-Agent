"""Thin wrappers around the git CLI."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


class GitError(Exception):
    """A git command failed or the repository is in an unexpected state."""


def run_git(args, cwd=None, check: bool = True, input: Optional[str] = None) -> subprocess.CompletedProcess:
    # core.quotePath=false keeps non-ASCII paths readable instead of octal-escaped.
    cmd = ["git", "-c", "core.quotePath=false", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, input=input, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GitError(detail or f"git {' '.join(args)} failed with exit code {proc.returncode}")
    return proc


def optional(root, *args: str) -> str:
    """Run git and return stripped stdout, or "" if the command fails."""
    proc = run_git(list(args), cwd=root, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_root(cwd=None) -> Path:
    proc = run_git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GitError("not inside a git work tree")
    return Path(proc.stdout.strip())


def hooks_dir(root: Path) -> Path:
    # --git-path respects core.hooksPath and linked worktrees.
    path = Path(run_git(["rev-parse", "--git-path", "hooks"], cwd=root).stdout.strip())
    return path if path.is_absolute() else root / path


def current_branch(root: Path) -> str:
    return optional(root, "symbolic-ref", "--short", "-q", "HEAD")


def recent_subjects(root: Path, count: int = 8) -> List[str]:
    out = optional(root, "log", f"-n{count}", "--no-merges", "--format=%s")
    return [line for line in out.splitlines() if line.strip()]


def has_staged(root: Path) -> bool:
    return run_git(["diff", "--cached", "--quiet"], cwd=root, check=False).returncode == 1


# --------------------------------------------------------------------------- staged diff

@dataclass
class FileChange:
    path: str
    status: str                      # A, M, D, R, C, T
    old_path: Optional[str] = None
    added: Optional[int] = 0         # None for binary files
    removed: Optional[int] = 0
    patch: str = ""
    omitted: str = ""                # why the patch is hidden from the model ("" = shown)

    @property
    def binary(self) -> bool:
        return self.added is None


def staged_changes(root: Path) -> List[FileChange]:
    files: Dict[str, FileChange] = {}
    order: List[str] = []

    tokens = run_git(["diff", "--cached", "--name-status", "-z", "-M"], cwd=root).stdout.split("\0")
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if status[0] in "RC" and i + 2 < len(tokens):
            change = FileChange(tokens[i + 2], status[0], old_path=tokens[i + 1])
            i += 3
        elif i + 1 < len(tokens):
            change = FileChange(tokens[i + 1], status[0])
            i += 2
        else:
            break
        files[change.path] = change
        order.append(change.path)

    tokens = run_git(["diff", "--cached", "--numstat", "-z", "-M"], cwd=root).stdout.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token or token.count("\t") < 2:
            i += 1
            continue
        added, removed, path = token.split("\t", 2)
        if path == "" and i + 2 < len(tokens):      # rename: "a\td\t\0old\0new"
            path = tokens[i + 2]
            i += 3
        else:
            i += 1
        change = files.get(path)
        if change:
            change.added = None if added == "-" else int(added)
            change.removed = None if removed == "-" else int(removed)

    patch = run_git(
        ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
         "--src-prefix=a/", "--dst-prefix=b/", "-U3"],
        cwd=root,
    ).stdout
    for section in _split_sections(patch):
        change = files.get(_section_path(section))
        if change:
            change.patch = section
    return [files[p] for p in order]


def _split_sections(patch: str) -> List[str]:
    sections, current = [], []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return value


def _section_path(section: str) -> str:
    lines = section.splitlines()
    minus = None
    for line in lines[1:12]:
        if line.startswith(("rename to ", "copy to ")):
            return _unquote(line.split(" ", 2)[2])
        if line.startswith("--- "):
            minus = _unquote(line[4:].rstrip("\t"))
        elif line.startswith("+++ "):
            plus = _unquote(line[4:].rstrip("\t"))
            if plus == "/dev/null" and minus:
                return minus[2:] if minus.startswith("a/") else minus
            return plus[2:] if plus.startswith("b/") else plus
        elif line.startswith("@@"):
            break
    head = lines[0][len("diff --git "):] if lines else ""
    quoted = re.match(r'^"(?:[^"\\]|\\.)*" ("b/(?:[^"\\]|\\.)*")$', head)
    if quoted:
        return _unquote(quoted.group(1))[2:]
    size = (len(head) - 5) // 2                  # head == "a/" + P + " b/" + P
    return head[2:2 + size] if size > 0 else head


# --------------------------------------------------------------------------- history

@dataclass
class Commit:
    sha: str
    parents: List[str]
    author: str
    date: str
    subject: str
    body: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


_LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%cs%x1f%s%x1f%b%x1e"


def log(root: Path, *args: str) -> List[Commit]:
    out = run_git(["log", f"--format={_LOG_FORMAT}", *args], cwd=root).stdout
    commits = []
    for record in out.split("\x1e"):
        record = record.lstrip("\n")
        parts = record.split("\x1f")
        if len(parts) < 6:
            continue
        sha, parents, author, date, subject, body = parts[:6]
        commits.append(Commit(sha, parents.split(), author, date, subject, body.strip()))
    return commits
