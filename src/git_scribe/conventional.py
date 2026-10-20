"""Conventional Commits: parsing, cleanup of model replies, and linting."""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

DEFAULT_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")

TYPE_HELP = {
    "feat": "a new feature or capability",
    "fix": "a bug fix",
    "docs": "documentation only",
    "style": "formatting only, no logic change",
    "refactor": "restructures code without changing behavior",
    "perf": "improves performance",
    "test": "adds or updates tests only",
    "build": "build system, packaging or dependencies",
    "ci": "CI configuration",
    "chore": "maintenance and tooling",
    "revert": "reverts an earlier commit",
}

ALIASES = {
    "feature": "feat", "features": "feat", "bugfix": "fix", "bug": "fix", "hotfix": "fix",
    "fixes": "fix", "fixed": "fix", "doc": "docs", "documentation": "docs", "tests": "test",
    "testing": "test", "refactoring": "refactor", "performance": "perf", "chores": "chore",
    "styles": "style", "builds": "build", "reverts": "revert",
}

HEADER_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\r\n]+)\))?(?P<bang>!)?: (?P<desc>\S.*)$")
LOOSE_RE = re.compile(
    r"^[\s>*`\"'#-]*(?P<type>[A-Za-z]+)[*`]*\s*(?:\(\s*(?P<scope>[^()\r\n]*?)\s*\))?\s*(?P<bang>!)?\s*:\s*(?P<desc>\S.*?)[\s`\"'*]*$"
)
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:\s*(?P<note>.+)$", re.M)
EXEMPT_RE = re.compile(r"^(Merge |Revert \"|fixup! |squash! |amend! |Initial commit\b)", re.I)
SCISSORS = "------------------------ >8 ------------------------"
_LABEL_RE = re.compile(
    r"^\s*(?:\*\*)?(?:suggested |proposed )?(?:commit message|message|subject|title|output|answer)(?:\*\*)?\s*:\s*", re.I
)
_CHATTER_RE = re.compile(
    r"^(this commit|this message|explanation|note:|notes:|here'?s|here is|i (have|hope)|the (above|commit message)|let me know|feel free)",
    re.I,
)


def canonical(kind: str) -> str:
    kind = kind.strip().lower()
    return ALIASES.get(kind, kind)


@dataclass
class Message:
    type: str
    description: str
    scope: Optional[str] = None
    breaking: bool = False
    body: str = ""

    @property
    def header(self) -> str:
        scope = f"({self.scope})" if self.scope else ""
        return f"{self.type}{scope}{'!' if self.breaking else ''}: {self.description}"

    def render(self) -> str:
        body = self.body.strip()
        return f"{self.header}\n\n{body}" if body else self.header


def parse(text: str, types: Optional[Sequence[str]] = None, lenient: bool = False) -> Optional[Message]:
    """Parse a commit message or PR title. Returns None when it is not conventional."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return None
    match = (LOOSE_RE if lenient else HEADER_RE).match(lines[0].strip())
    if not match:
        return None
    kind = canonical(match["type"]) if lenient else match["type"]
    if kind not in (types or DEFAULT_TYPES):
        return None
    body = "\n".join(lines[1:]).strip()
    scope = (match["scope"] or "").strip() or None
    breaking = bool(match["bang"]) or bool(BREAKING_RE.search(body))
    return Message(kind, match["desc"].strip(), scope, breaking, body)


def clean_reply(raw: str) -> str:
    """Strip reasoning blocks and code fences that small models like to add."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S | re.I)
    fence = re.search(r"```[\w-]*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    return text.replace("```", "").strip()


def tidy(description: str) -> str:
    text = re.sub(r"\s+", " ", description).strip().strip("`\"'*").strip()
    text = text.rstrip(".").rstrip()
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def coerce(raw: str, types: Sequence[str], scopes: Sequence[str] = ()) -> Tuple[Optional[Message], str]:
    """Turn a model reply into a Message, or explain why it can't be used."""
    text = clean_reply(raw)
    if not text:
        return None, "the reply was empty"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = LOOSE_RE.match(_LABEL_RE.sub("", line))
        if match and canonical(match["type"]) in types:
            break
    else:
        return None, "the first line must look like 'type(scope): description' with a type from: " + ", ".join(types)

    scope = (match["scope"] or "").strip().lower().replace(" ", "-") or None
    if scope and scopes and scope not in scopes:
        scope = None
    description = tidy(match["desc"])
    if not description:
        return None, "the description after the colon is empty"
    body = _clean_body(lines[index + 1:])
    breaking = bool(match["bang"]) or bool(BREAKING_RE.search(body))
    return Message(canonical(match["type"]), description, scope, breaking, body), ""


def _clean_body(lines: List[str]) -> str:
    paragraphs, current = [], []
    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    kept = []
    for paragraph in paragraphs:
        if _CHATTER_RE.match(paragraph[0].strip()):
            break                                   # model started explaining itself
        kept.append("\n".join(re.sub(r"^\s*[*\u2022]\s+", "- ", line) for line in paragraph))
    return wrap_body("\n\n".join(kept))


def wrap_body(body: str, width: int = 72) -> str:
    out = []
    for line in body.splitlines():
        if len(line) <= width or "://" in line or re.match(r"^(BREAKING CHANGE|[\w-]+)(: | #)", line):
            out.append(line)
            continue
        bullet = re.match(r"^(\s*[-*] )", line)
        indent = " " * len(bullet.group(1)) if bullet else ""
        out.extend(textwrap.wrap(line, width, subsequent_indent=indent,
                                 break_long_words=False, break_on_hyphens=False) or [line])
    return "\n".join(out)


def fit_header(message: Message, max_len: int) -> Message:
    """Shorten the description at a word boundary so the header fits."""
    if len(message.header) <= max_len:
        return message
    if message.scope and len(message.description) < 20:
        message.scope = None
    room = len(message.description) - (len(message.header) - max_len)
    if room <= 0:
        return message
    cut = message.description[:room]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    message.description = cut.rstrip(" ,;:-")
    return message


def strip_comments(text: str, comment: str = "#") -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(comment) and SCISSORS in line:
            break                                   # everything below is the -v diff
        if comment and line.startswith(comment):
            continue
        out.append(line)
    return "\n".join(out).strip()


def lint(text: str, types: Sequence[str], scopes: Sequence[str] = (), max_len: int = 72) -> List[str]:
    text = text.strip()
    if not text or EXEMPT_RE.match(text):
        return []
    lines = text.splitlines()
    match = HEADER_RE.match(lines[0])
    if not match:
        return [f"first line must match 'type(scope): description', got {lines[0][:60]!r}"]
    problems = []
    if match["type"] not in types:
        problems.append(f"type '{match['type']}' is not one of: {', '.join(types)}")
    if scopes and match["scope"] and match["scope"] not in scopes:
        problems.append(f"scope '{match['scope']}' is not one of: {', '.join(scopes)}")
    if len(lines[0]) > max_len:
        problems.append(f"first line is {len(lines[0])} characters (max {max_len})")
    if match["desc"].rstrip().endswith("."):
        problems.append("description should not end with a period")
    if len(lines) > 1 and lines[1].strip():
        problems.append("leave a blank line between the first line and the body")
    return problems
