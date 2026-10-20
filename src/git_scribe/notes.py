"""Release notes from the Conventional Commits in a range of history."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import conventional as cc
from .config import Settings
from .gitutil import Commit, GitError, log, optional
from .llm import Client, LLMError, parse_json

# Section key -> heading, in display order. Keys are commit types plus "breaking" and "other".
SECTIONS = {
    "breaking": "Breaking changes",
    "feat": "Features",
    "fix": "Bug fixes",
    "perf": "Performance",
    "revert": "Reverts",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "build": "Build and dependencies",
    "ci": "Continuous integration",
    "test": "Tests",
    "style": "Code style",
    "chore": "Maintenance",
    "other": "Other changes",
}
# Not user-facing, so only listed on request. Custom types from scribe.types are always listed.
INTERNAL = {"refactor", "docs", "build", "ci", "test", "style", "chore"}


@dataclass
class Entry:
    sha: str
    section: str
    description: str
    scope: Optional[str] = None
    note: str = ""                                  # BREAKING CHANGE footer
    refs: List[str] = field(default_factory=list)   # PR/issue numbers from a trailing "(#123)"


@dataclass
class Release:
    title: str
    date: str
    entries: List[Entry]
    previous: str = ""          # tag the range starts after ("" = start of history)
    current: str = ""           # tag at the end of the range ("" = untagged)
    head: str = ""              # newest commit in the range
    web: str = ""               # https URL of the repository page
    forge: str = ""             # "github" | "gitlab" | "" (no links)
    summary: str = ""
    warning: str = ""

    def visible(self, include_all: bool = False) -> List[Entry]:
        return [e for e in self.entries if include_all or e.section not in INTERNAL]


# --------------------------------------------------------------------------- history

def _tag(root: Path, rev: str) -> str:
    """`rev` if it names a tag, else ""."""
    if rev and optional(root, "rev-parse", "-q", "--verify", f"refs/tags/{rev}"):
        return rev
    return ""


def resolve_range(root: Path, rev_range: Optional[str] = None) -> Tuple[str, str, str]:
    """(range for git log, previous tag, current tag). Default: last tag..HEAD."""
    if rev_range:
        if rev_range.startswith("-"):
            raise GitError(f"not a revision range: {rev_range}")
        start, dots, end = rev_range.partition("..")
        if not dots:
            return rev_range, "", _tag(root, rev_range)
        return rev_range, _tag(root, start), _tag(root, end.lstrip(".") or "HEAD")
    if not optional(root, "rev-parse", "-q", "--verify", "HEAD"):
        raise GitError("the repository has no commits yet")
    # When HEAD is tagged, describe that release: previous tag..HEAD.
    current = optional(root, "describe", "--tags", "--exact-match", "HEAD")
    previous = optional(root, "describe", "--tags", "--abbrev=0", "HEAD^" if current else "HEAD")
    return (f"{previous}..HEAD" if previous else "HEAD"), previous, current


_REFS_RE = re.compile(r"\s*\(((?:#\d+[,\s]*)+)\)$")
_REVERT_RE = re.compile(r'^Revert "(?P<subject>.+)"$')
_SKIP_RE = re.compile(r"^(fixup|squash|amend)! ")


def to_entry(commit: Commit, types: Sequence[str]) -> Optional[Entry]:
    subject = commit.subject.strip()
    if not subject or _SKIP_RE.match(subject):
        return None
    refs: List[str] = []
    match = _REFS_RE.search(subject)
    if match:                                       # squash merges: "feat: add x (#12)"
        refs = re.findall(r"\d+", match.group(1))
        subject = subject[:match.start()].rstrip()
    revert = _REVERT_RE.match(subject)
    if revert:
        return Entry(commit.sha, "revert", revert["subject"], refs=refs)
    message = cc.parse(f"{subject}\n\n{commit.body}", types, lenient=True)
    if message is None:
        return Entry(commit.sha, "other", subject, refs=refs)
    note = ""
    if message.breaking:
        found = cc.BREAKING_RE.search(message.body)
        note = found["note"].strip() if found else ""
    return Entry(commit.sha, "breaking" if message.breaking else message.type,
                 cc.tidy(message.description) or subject, message.scope, note, refs)


_REMOTE_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?(?P<host>[^@/:\s]+\.[^@/:\s]+)(?::\d+)?[:/]"
    r"(?P<path>[^:\s]+?)(?:\.git)?/*$", re.I)


def web_url(root: Path, remote: str) -> Tuple[str, str]:
    """(https URL, forge) for GitHub/GitLab remotes, else ("", ""). Credentials in the URL are dropped."""
    url = optional(root, "remote", "get-url", remote) if remote else ""
    match = _REMOTE_RE.match(url)
    if not match:
        return "", ""
    host = match["host"].lower()
    forge = "github" if "github" in host else "gitlab" if "gitlab" in host else ""
    return (f"https://{host}/{match['path']}", forge) if forge else ("", "")


# --------------------------------------------------------------------------- summary

SYSTEM = """You write the opening paragraph of release notes for the users of a software project.

Rules:
- Write one to three plain sentences about the changes that matter most to users.
- Use only what the change list says. Never invent features, reasons, numbers or names.
- No headings, lists, markdown or emoji.
- {language}Reply with JSON only, shaped like {{"summary": "..."}}."""


def summarize(release: Release, entries: Sequence[Entry], s: Settings, timeout: Optional[float] = None) -> str:
    budget = min(s.max_diff_chars, 6000)
    lines: List[str] = []
    size = 0
    for index, e in enumerate(entries):
        scope = f"{e.scope}: " if e.scope else ""
        line = f"- [{SECTIONS.get(e.section, e.section)}] {scope}{e.description}"
        if e.note:
            line += f" (breaking: {e.note})"
        if lines and size + len(line) > budget:
            lines.append(f"- ... and {len(entries) - index} more")
            break
        lines.append(line)
        size += len(line) + 1
    language = f"Write the summary in {s.language}.\n- " if s.language else ""
    messages = [
        {"role": "system", "content": SYSTEM.format(language=language)},
        {"role": "user", "content": f"Release: {release.title}\n\nChanges:\n" + "\n".join(lines)
                                    + "\n\nWrite the summary paragraph."},
    ]
    reply = Client(s, timeout=timeout).chat(messages, json_mode=True, max_tokens=300)
    data = parse_json(reply)
    summary = data.get("summary") if isinstance(data, dict) else None
    if isinstance(summary, list):
        summary = " ".join(str(part) for part in summary)
    return re.sub(r"\s+", " ", summary).strip() if isinstance(summary, str) else ""


# --------------------------------------------------------------------------- pipeline

def build(root: Path, s: Settings, rev_range: Optional[str] = None, *, title: Optional[str] = None,
          include_all: bool = False, use_llm: bool = True, timeout: Optional[float] = None) -> Release:
    git_range, previous, current = resolve_range(root, rev_range)
    commits = log(root, "--no-merges", git_range, "--")
    if not commits:
        raise GitError(f"no commits in {git_range}")
    entries: List[Entry] = []
    seen = set()
    for commit in commits:
        entry = to_entry(commit, s.types)
        if entry is None:
            continue
        key = (entry.section, entry.scope, entry.description.lower())
        if key not in seen:                         # cherry-picks and repeated subjects
            seen.add(key)
            entries.append(entry)
    web, forge = web_url(root, s.remote)
    release = Release(title or current or "Unreleased", commits[0].date, entries,
                      previous, current, commits[0].sha, web, forge)
    shown = release.visible(include_all)
    if use_llm and shown:
        try:
            release.summary = summarize(release, shown, s, timeout)
            if not release.summary:
                release.warning = "the model did not return a usable summary; left it out"
        except LLMError as exc:
            release.warning = f"{exc}; left out the summary"
    return release


_PATHS = {
    "github": {"issue": "issues", "commit": "commit", "compare": "compare"},
    "gitlab": {"issue": "-/issues", "commit": "-/commit", "compare": "-/compare"},
}


def _link(release: Release, text: str, kind: str, target: str) -> str:
    if not release.forge:
        return text
    return f"[{text}]({release.web}/{_PATHS[release.forge][kind]}/{target})"


def _line(release: Release, e: Entry) -> str:
    scope = f"**{e.scope}:** " if e.scope else ""
    refs = "".join(f" ({_link(release, '#' + n, 'issue', n)})" for n in e.refs)
    line = f"- {scope}{e.description}{refs} ({_link(release, e.sha[:7], 'commit', e.sha)})"
    if e.note:
        line += f"\n  - {e.note}"
    return line


def _rank(section: str) -> float:
    keys = list(SECTIONS)
    return keys.index(section) if section in keys else keys.index("refactor") - 0.5


def render(release: Release, include_all: bool = False) -> str:
    shown = release.visible(include_all)
    out = [f"## {release.title} ({release.date})", ""]
    if release.summary:
        out += [release.summary, ""]
    if not shown:
        out += ["No user-facing changes.", ""]
    sections: List[str] = []
    for e in shown:
        if e.section not in sections:
            sections.append(e.section)
    for section in sorted(sections, key=_rank):
        out += [f"### {SECTIONS.get(section, section.capitalize())}", ""]
        out += [_line(release, e) for e in shown if e.section == section]
        out.append("")
    if release.forge and release.previous:
        label = f"{release.previous}...{release.current or release.head[:7]}"
        target = f"{release.previous}...{release.current or release.head}"
        out += [f"Full changelog: {_link(release, label, 'compare', target)}", ""]
    return "\n".join(out).rstrip() + "\n"
