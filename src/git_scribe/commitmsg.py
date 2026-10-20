"""Draft a Conventional Commits message for the staged changes."""
from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional, Sequence

from . import conventional as cc
from .config import Settings
from .gitutil import FileChange, current_branch, recent_subjects, staged_changes
from .llm import Client, LLMError


class NothingStaged(Exception):
    """The index has no changes to describe."""


# --------------------------------------------------------------------------- file triage

LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "cargo.lock", "gemfile.lock", "poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock", "composer.lock",
    "go.sum", "mix.lock", "pubspec.lock", "podfile.lock", "packages.lock.json", "flake.lock",
    "gradle.lockfile", "deno.lock", "package.resolved",
}
ASSET_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".svg", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".mp3", ".mp4", ".wav", ".mov", ".webm", ".pdf", ".zip", ".gz", ".tar", ".jar",
}
_SENSITIVE = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|[^/]*\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)[^/]*|\.npmrc|\.pypirc|\.netrc)$", re.I)
_GENERATED = re.compile(r"(\.min\.(js|css)|\.map|\.pb\.go|_pb2(_grpc)?\.py|\.g\.dart|\.snap)$|(^|/)(dist|vendor|node_modules|__snapshots__)/", re.I)
_CI = re.compile(r"^(\.github/(workflows|actions)/|\.gitlab-ci|\.circleci/|\.buildkite/|\.travis\.yml|azure-pipelines|jenkinsfile|bitbucket-pipelines|\.drone\.yml|appveyor)", re.I)
_TEST = re.compile(r"(^|/)(tests?|__tests__|specs?|e2e|testdata|fixtures)/|(_test\.\w+|\.test\.\w+|\.spec\.\w+|_spec\.rb)$|(^|/)(test_[^/]+\.py|conftest\.py)$", re.I)
_TEST_CLASS = re.compile(r"(^|/)\w+Tests?\.(java|kt|cs|swift)$")
_BUILD = re.compile(
    r"(^|/)(package\.json|pyproject\.toml|setup\.(py|cfg)|requirements[^/]*\.(txt|in)|cargo\.toml|go\.mod|gemfile|pipfile|"
    r"composer\.json|pom\.xml|(build|settings)\.gradle(\.kts)?|makefile|cmakelists\.txt|dockerfile|containerfile|"
    r"(docker-)?compose[^/]*\.ya?ml|justfile|[^/]*\.csproj|tsconfig[^/]*\.json|(vite|webpack|rollup|esbuild)\.config\.\w+)$", re.I)
_DOCS = re.compile(r"\.(md|mdx|rst|adoc|txt)$|(^|/)(docs?|documentation)/|(^|/)(readme|changelog|contributing|license|authors|notice)[^/]*$", re.I)


def categorize(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in LOCKFILES:
        return "lockfile"
    if _SENSITIVE.search(path):
        return "sensitive"
    if _GENERATED.search(path):
        return "generated"
    if _CI.search(path):
        return "ci"
    if _TEST.search(path) or _TEST_CLASS.search(path):
        return "test"
    if _BUILD.search(path):
        return "build"
    if _DOCS.search(path):
        return "docs"
    if PurePosixPath(name).suffix in ASSET_EXT:
        return "asset"
    return "source"


_HIDDEN = {"lockfile": "lockfile", "sensitive": "sensitive file, content hidden",
           "generated": "generated file", "asset": "asset"}


def omit_reason(change: FileChange, settings: Settings) -> str:
    if change.binary:
        return "binary"
    reason = _HIDDEN.get(categorize(change.path))
    if reason:
        return reason
    name = change.path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(change.path, p) or fnmatch.fnmatch(name, p) for p in settings.exclude):
        return "excluded by config"
    return ""


# High-confidence secret shapes. Redacted before anything reaches the model.
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[\"']?\s*[:=]\s*[\"'])[^\"'\s]{6,}([\"'])"), r"\1[REDACTED]\2"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- diff budget

def _compact(patch: str) -> str:
    noise = ("index ", "similarity index ", "dissimilarity index ")
    return "".join(line for line in patch.splitlines(keepends=True) if not line.startswith(noise))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    lines = text.splitlines(keepends=True)
    kept, size = [], 0
    for line in lines:
        if size + len(line) > limit and kept:
            break
        kept.append(line)
        size += len(line)
    return "".join(kept).rstrip("\n") + f"\n... [{len(lines) - len(kept)} more lines truncated]\n"


def render_diff(files: Sequence[FileChange], budget: int) -> str:
    """Fit the diff into `budget` chars: small files stay whole, big ones share what's left."""
    shown = [(f.path, _compact(redact(f.patch))) for f in files if f.patch and not f.omitted]
    pieces, remaining = {}, budget
    for i, (path, text) in enumerate(sorted(shown, key=lambda item: len(item[1]))):
        share = max(remaining // (len(shown) - i), 0)
        pieces[path] = text if len(text) <= share else _truncate(text, share)
        remaining -= len(pieces[path])
    return "".join(pieces[path] for path, _ in shown)


_STATUS = {"A": "added", "D": "deleted", "R": "renamed", "C": "copied", "M": "modified", "T": "type changed"}


def _file_line(f: FileChange) -> str:
    name = f"{f.old_path} -> {f.path}" if f.old_path else f.path
    stats = "binary" if f.binary else f"+{f.added} -{f.removed}"
    note = f"; {f.omitted}, diff not shown" if f.omitted else ""
    return f"- {_STATUS.get(f.status, f.status)}: {name} ({stats}{note})"


# --------------------------------------------------------------------------- heuristics

_BRANCH_TYPE = re.compile(r"^(?:[\w.-]+/)?(feat|feature|fix|bugfix|hotfix|docs|refactor|perf|test|build|ci|chore|style)[/_-]", re.I)
_GENERIC_DIRS = {"src", "lib", "libs", "app", "apps", "packages", "pkg", "internal", "cmd", "source", "main",
                 "test", "tests", "spec", "docs", "doc", "scripts", "python", "java", "js", "ts"}
_VERBS = {"A": "add", "D": "remove", "R": "rename", "C": "copy"}


def _common_scope(files: Sequence[FileChange]) -> Optional[str]:
    candidates = set()
    for f in files:
        parts = [p for p in f.path.split("/")[:-1] if p.lower() not in _GENERIC_DIRS and not p.startswith(".")]
        candidates.add(parts[0].lower() if parts else "")
    scope = candidates.pop() if len(candidates) == 1 else ""
    return scope if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,24}", scope) else None


def _describe(files: Sequence[FileChange]) -> str:
    def base(path: str) -> str:
        return path.rsplit("/", 1)[-1]

    if len(files) == 1:
        f = files[0]
        if f.status == "R" and f.old_path:
            return f"rename {base(f.old_path)} to {base(f.path)}"
        return f"{_VERBS.get(f.status, 'update')} {base(f.path)}"
    statuses = {f.status for f in files}
    verb = _VERBS.get(next(iter(statuses)), "update") if len(statuses) == 1 else "update"
    if len(files) == 2:
        return f"{verb} {base(files[0].path)} and {base(files[1].path)}"
    return f"{verb} {len(files)} files"


def heuristic(files: Sequence[FileChange], branch: str, types: Sequence[str]) -> cc.Message:
    """A deterministic draft from file paths alone (used offline and as a safety net)."""
    categories = {categorize(f.path) for f in files}
    scope = None
    if "lockfile" in categories and categories <= {"lockfile", "build"}:
        kind, scope, description = "chore", "deps", "update dependencies"
    else:
        core = categories - {"lockfile"}
        if core == {"docs"}:
            kind = "docs"
        elif core == {"test"}:
            kind = "test"
        elif core == {"ci"}:
            kind = "ci"
        elif core == {"build"}:
            kind = "build"
        else:
            match = _BRANCH_TYPE.match(branch or "")
            if match:
                kind = cc.canonical(match.group(1))
            elif any(f.status == "A" and categorize(f.path) == "source" for f in files):
                kind = "feat"
            else:
                kind = "chore"
        scope = _common_scope(files)
        description = _describe(files)
    if kind not in types:
        kind = "chore" if "chore" in types else types[0]
    return cc.Message(kind, description, scope)


# --------------------------------------------------------------------------- prompts

SYSTEM = """You write git commit messages that follow the Conventional Commits specification.

Format:
<type>(<optional scope>): <description>

<optional body>

<optional footer>

Types:
{types}

Rules:
- The first line is at most {max_len} characters.
- The scope is optional: one short lowercase word naming the affected area{scope_rule}. Leave it out when no single area fits.
- The description uses the imperative mood ("add", "fix", "remove"; not "added" or "adds"), starts with a lowercase letter and has no trailing period.
- {body_rule}
- Only when the change breaks backwards compatibility: put "!" before the colon and add the footer "BREAKING CHANGE: <what breaks>".
- Describe only what the diff shows. Never invent features, reasons, issue numbers or file names.
- {language_rule}Reply with the commit message only: no quotes, no code fences, no headings, no explanations."""

_BODY_RULES = {
    "auto": "Add a body (1-3 short lines or '- ' bullets explaining what and why) only when the change is not obvious from the first line; small changes get only the first line.",
    "always": "After a blank line, add a body of 1-4 short '- ' bullets explaining what changed and why.",
    "never": "Write only the first line. No body.",
}


def system_prompt(s: Settings) -> str:
    types = "\n".join(f"- {t}: {cc.TYPE_HELP[t]}" if t in cc.TYPE_HELP else f"- {t}" for t in s.types)
    return SYSTEM.format(
        types=types,
        max_len=s.max_subject_length,
        scope_rule=f", preferably one of: {', '.join(s.scopes)}" if s.scopes else "",
        body_rule=_BODY_RULES[s.body],
        language_rule=(f"Write the description and body in {s.language}; keep type and scope in English.\n- "
                       if s.language else ""),
    )


def user_prompt(files, s: Settings, branch: str, recent: List[str], context: Optional[str],
                type_hint: Optional[str], scope_hint: Optional[str]) -> str:
    parts = []
    if context:
        parts.append(f"Author's note about the intent of this change: {context}")
    if branch:
        parts.append(f"Branch: {branch}")
    if type_hint:
        parts.append(f"Use type: {type_hint}")
    if scope_hint:
        parts.append(f"Use scope: {scope_hint}")
    styled = [subject for subject in recent if cc.parse(subject, s.types)]
    if styled:
        parts.append("Recent commits in this repository (match their style and scope names):\n"
                     + "\n".join(f"- {subject}" for subject in styled[:6]))
    listing = [_file_line(f) for f in files[:60]]
    if len(files) > 60:
        listing.append(f"- ... and {len(files) - 60} more files")
    parts.append(f"Staged files ({len(files)}):\n" + "\n".join(listing))
    diff = render_diff(files, s.max_diff_chars)
    if diff:
        parts.append("Diff:\n" + diff.rstrip())
    parts.append("Write the commit message for these staged changes.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- pipeline

@dataclass
class Draft:
    message: str
    source: str                 # "model" | "heuristic"
    model: str = ""
    seconds: float = 0.0
    files: int = 0
    added: int = 0
    removed: int = 0
    warning: str = ""


def _accept(reply: str, s: Settings, final: bool):
    message, problem = cc.coerce(reply, s.types, s.scopes)
    if message is None:
        return None, problem
    if len(message.header) > s.max_subject_length and not final:
        return None, f"the first line has {len(message.header)} characters; keep it within {s.max_subject_length}"
    return message, ""


def _salvage(reply: str, fallback: cc.Message) -> Optional[cc.Message]:
    """Model wrote a sensible line but forgot the type prefix: borrow it from the heuristics."""
    lines = [line.strip() for line in cc.clean_reply(reply).splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[0]
    if not 3 <= len(line) <= 120 or line.endswith(":") or cc._CHATTER_RE.match(line):
        return None
    return cc.Message(fallback.type, cc.tidy(line), fallback.scope)


def _finish(message: cc.Message, s: Settings, type_hint: Optional[str], scope_hint: Optional[str]) -> cc.Message:
    if type_hint:
        message.type = cc.canonical(type_hint)
    if scope_hint:
        message.scope = scope_hint
    if s.body == "never":
        message.body = "\n".join(line for line in message.body.splitlines() if cc.BREAKING_RE.match(line))
    return cc.fit_header(message, s.max_subject_length)


def draft(root: Path, s: Settings, *, context: Optional[str] = None, type_hint: Optional[str] = None,
          scope_hint: Optional[str] = None, use_llm: bool = True, timeout: Optional[float] = None,
          temperature: Optional[float] = None) -> Draft:
    files = staged_changes(root)
    if not files:
        raise NothingStaged("nothing is staged; stage changes with `git add` first")
    for f in files:
        f.omitted = omit_reason(f, s)
    stats = dict(files=len(files), added=sum(f.added or 0 for f in files),
                 removed=sum(f.removed or 0 for f in files))
    branch = current_branch(root)
    fallback = heuristic(files, branch, s.types)
    if not use_llm:
        return Draft(_finish(fallback, s, type_hint, scope_hint).render(), "heuristic", **stats)

    client = Client(s, timeout=timeout)
    messages = [
        {"role": "system", "content": system_prompt(s)},
        {"role": "user", "content": user_prompt(files, s, branch, recent_subjects(root), context, type_hint, scope_hint)},
    ]
    started = time.monotonic()
    reply = client.chat(messages, max_tokens=320, temperature=temperature)
    message, problem = _accept(reply, s, final=False)
    if message is None:
        # One self-correction round: show the model its reply and what was wrong with it.
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"That is not a valid commit message: {problem}. "
                                        "Reply again with only the corrected commit message."},
        ]
        reply = client.chat(messages, max_tokens=320, temperature=temperature)
        message, problem = _accept(reply, s, final=True)
    warning = ""
    if message is None:
        message = _salvage(reply, fallback)
        if message is None:
            raise LLMError(f"the model did not return a usable commit message ({problem})")
        warning = "the model left out the type prefix; added one from file heuristics"
    message = _finish(message, s, type_hint, scope_hint)
    return Draft(message.render(), "model", s.model, time.monotonic() - started, warning=warning, **stats)
