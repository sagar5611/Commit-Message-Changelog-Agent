"""Command line interface: git-scribe <command> [options]."""
from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from . import __version__, commitmsg, config, notes, ui
from . import conventional as cc
from .commitmsg import Draft, NothingStaged
from .config import ConfigError, Settings
from .gitutil import GitError, current_branch, has_staged, hooks_dir, optional, repo_root
from .llm import Client, LLMError

# --------------------------------------------------------------------------- helpers

_MODEL_FLAGS = ("provider", "model", "base_url", "timeout", "temperature", "language", "body")


def _root_or_none() -> Optional[Path]:
    try:
        return repo_root()
    except GitError:
        return None


def _settings(args, root: Optional[Path]) -> Settings:
    return config.load(root, **{name: getattr(args, name, None) for name in _MODEL_FLAGS})


def _comment_char(root: Optional[Path]) -> str:
    value = (optional(root, "config", "core.commentString")
             or optional(root, "config", "core.commentChar") or "#")
    return "#" if value == "auto" else value


def _about(d: Draft) -> str:
    stats = f"{d.files} file{'' if d.files == 1 else 's'}, +{d.added} -{d.removed}"
    if d.source == "model":
        return f"drafted by {d.model} in {d.seconds:.1f}s ({stats})"
    return f"drafted from file heuristics ({stats})"


def _type_hint(value: Optional[str], s: Settings) -> Optional[str]:
    if not value:
        return None
    kind = cc.canonical(value)
    if kind not in s.types:
        raise ConfigError(f"unknown type '{value}'; choose from: {', '.join(s.types)}")
    return kind


def _draft(root: Path, s: Settings, args) -> Draft:
    options = dict(context=args.context, type_hint=_type_hint(args.type, s), scope_hint=args.scope)
    use_llm = not args.offline
    try:
        spinner = ui.Spinner(f"drafting a commit message with {s.model}") if use_llm else contextlib.nullcontext()
        with spinner:
            d = commitmsg.draft(root, s, use_llm=use_llm, **options)
    except LLMError as exc:
        if not args.fallback:
            raise
        ui.say(f"{exc}; drafting from file heuristics instead")
        d = commitmsg.draft(root, s, use_llm=False, **options)
    if d.warning:
        ui.say(f"warning: {d.warning}")
    if args.verbose:
        ui.say(_about(d))
    return d


# --------------------------------------------------------------------------- commit messages

def cmd_msg(args) -> int:
    root = repo_root()
    print(_draft(root, _settings(args, root), args).message)
    return 0


def cmd_commit(args) -> int:
    root = repo_root()
    d = _draft(root, _settings(args, root), args)
    fd, path = tempfile.mkstemp(prefix="git-scribe-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(d.message + "\n")
        # -e opens the editor so the draft can be reviewed; the hook skips messages given with -F.
        return subprocess.call(["git", "commit", "-F", path, *([] if args.yes else ["-e"])], cwd=root)
    finally:
        os.unlink(path)


def cmd_lint(args) -> int:
    root = _root_or_none()
    s = config.load(root)
    if args.file in (None, "-"):
        text = sys.stdin.read()
    else:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    message = cc.strip_comments(text, _comment_char(root))
    problems = cc.lint(message, s.types, s.scopes, s.max_subject_length)
    if not problems:
        return 0
    ui.say("the commit message does not follow Conventional Commits:")
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    if args.file and Path(args.file).name == "COMMIT_EDITMSG":
        print("  (bypass the check with: git commit --no-verify)", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- release notes

def cmd_notes(args) -> int:
    root = repo_root()
    s = _settings(args, root)
    use_llm = not args.offline
    spinner = ui.Spinner(f"writing the release summary with {s.model}") if use_llm else contextlib.nullcontext()
    with spinner:
        release = notes.build(root, s, args.range, title=args.title, include_all=args.all, use_llm=use_llm)
    if release.warning:
        ui.say(f"warning: {release.warning}")
    sys.stdout.write(notes.render(release, args.all))
    return 0


# --------------------------------------------------------------------------- git hooks

HOOK_MARK = "# Installed by git-scribe."
BACKUP_SUFFIX = ".pre-git-scribe"
# hook name -> (git-scribe arguments, what it does, never block the commit)
HOOKS = {
    "prepare-commit-msg": ("hook run", "Drafts a message for a plain `git commit`; never blocks a commit.", True),
    "commit-msg": ("lint", "Checks the message format; bypass with `git commit --no-verify`.", False),
}


def _hook_script(command: str, purpose: str, never_block: bool) -> str:
    # Pin the interpreter that installed the hook so it works without an activated venv;
    # fall back to git-scribe on PATH, and do nothing if neither is available any more.
    python = shlex.quote(Path(sys.executable).as_posix())
    run = '"$@"' + (" || true" if never_block else "")
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARK} {purpose}\n"
        "# Remove with: git-scribe hook uninstall\n"
        f"if [ -x {python} ] && {python} -c 'import git_scribe' 2>/dev/null; then\n"
        f"  {python} -m git_scribe {command} {run}\n"
        "elif command -v git-scribe >/dev/null 2>&1; then\n"
        f"  git-scribe {command} {run}\n"
        "fi\n"
    )


def _ours(path: Path) -> bool:
    try:
        return HOOK_MARK in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def cmd_hook_install(args) -> int:
    directory = hooks_dir(repo_root())
    names = ["prepare-commit-msg"] + (["commit-msg"] if args.lint else [])
    foreign = [directory / name for name in names if (directory / name).exists() and not _ours(directory / name)]
    for path in foreign:                            # check everything before touching anything
        command = HOOKS[path.name][0]
        if not args.force:
            raise GitError(f"{path} already exists; add `git-scribe {command} \"$@\"` to it, "
                           "or rerun with --force to replace it (the old hook is kept as a backup)")
        if path.with_name(path.name + BACKUP_SUFFIX).exists():
            raise GitError(f"{path.name}{BACKUP_SUFFIX} already exists in {directory}; move it away first")
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = directory / name
        if path in foreign:
            backup = path.with_name(name + BACKUP_SUFFIX)
            path.replace(backup)
            ui.say(f"moved the existing {name} hook to {backup}")
        path.write_text(_hook_script(*HOOKS[name]), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ui.say(f"installed {path}")
    return 0


def cmd_hook_uninstall(args) -> int:
    directory = hooks_dir(repo_root())
    removed = False
    for name in HOOKS:
        path = directory / name
        if not (path.is_file() and _ours(path)):
            continue
        path.unlink()
        removed = True
        ui.say(f"removed {path}")
        backup = path.with_name(name + BACKUP_SUFFIX)
        if backup.exists():
            backup.replace(path)
            ui.say(f"restored the previous {name} hook")
    if not removed:
        ui.say("no git-scribe hooks are installed")
    return 0


def cmd_hook_run(args) -> int:
    """prepare-commit-msg <file> [<source> [<sha>]]. Always exits 0 so it never blocks a commit."""
    if args.source:         # -m/-F, template, merge, squash, amend: a message already exists
        return 0
    try:
        root = repo_root()
        s = config.load(root)
        if not s.enabled or not has_staged(root):
            return 0
        path = Path(args.file)
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        comment = _comment_char(root)
        if cc.strip_comments(existing, comment):
            return 0        # something else already wrote a message
        try:
            with ui.Spinner(f"git-scribe: drafting a commit message with {s.model}"):
                d = commitmsg.draft(root, s, timeout=s.hook_timeout)
        except LLMError as exc:
            if s.hook_fallback != "heuristic":
                ui.say(f"{exc}; write the message yourself")
                return 0
            ui.say(f"{exc}; drafted from file heuristics instead")
            d = commitmsg.draft(root, s, use_llm=False)
        if d.warning:
            ui.say(f"warning: {d.warning}")
        # Git only adds its comment block when the editor will open (and comments get stripped);
        # with --no-edit our note would end up in the commit, so only add it next to git's.
        editing = any(line.startswith(comment) for line in existing.splitlines())
        note = f"{comment} git-scribe: {_about(d)}. Review it before saving.\n" if editing else ""
        path.write_text(f"{d.message}\n\n{note}{existing}", encoding="utf-8")
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - the hook must never get in the way of a commit
        ui.say(f"skipped drafting ({exc})")
    return 0


# --------------------------------------------------------------------------- diagnostics

def cmd_config(args) -> int:
    s = config.load(_root_or_none())
    for f in config.FIELDS:
        value = getattr(s, f.name)
        source = s.sources.get(f.name, "default")
        if f.name == "api_key":
            shown = "(set)" if value else "(not set)"
            if value and f.name not in s.sources:
                source = "OPENAI_API_KEY"
        elif isinstance(value, bool):
            shown = str(value).lower()
        elif isinstance(value, float):
            shown = f"{value:g}"
        elif isinstance(value, list):
            shown = ", ".join(value) or "(none)"
        else:
            shown = str(value) if value != "" else "(not set)"
        print(f"{config.git_key(f.name):<24} {source:<14} {shown}")
    print(f"{'(endpoint)':<24} {'':<14} {s.endpoint}")
    return 0


def cmd_doctor(args) -> int:
    root = _root_or_none()
    s = _settings(args, root)
    ok = True
    print(f"git-scribe {__version__}")
    if root:
        print(f"repository: {root} (branch {current_branch(root) or 'detached HEAD'})")
    else:
        print("repository: not inside a git work tree")
    print(f"model:      {s.model} via {s.provider} at {s.endpoint}")
    try:
        available = Client(s).models()
    except LLMError as exc:
        ok = False
        hint = "; start it with: ollama serve" if s.provider == "ollama" and exc.status is None else ""
        print(f"server:     {exc}{hint}")
    else:
        if s.model in available or (":" not in s.model and f"{s.model}:latest" in available):
            print("server:     reachable, model available")
        elif s.provider == "ollama":
            ok = False
            print(f"server:     reachable, but the model is missing; run: ollama pull {s.model}")
        else:
            print("server:     reachable; it does not list this model (some servers ignore the name)")
    if root:
        directory = hooks_dir(root)
        for name in HOOKS:
            path = directory / name
            state = "installed" if _ours(path) else "another hook is installed" if path.exists() else "not installed"
            print(f"{name + ':':<19} {state}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-scribe",
        description="Conventional Commit messages and release notes from a small local model.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    model = argparse.ArgumentParser(add_help=False)
    group = model.add_argument_group("model options (override git config scribe.*)")
    group.add_argument("--provider", choices=config.CHOICES["provider"])
    group.add_argument("--model", metavar="NAME")
    group.add_argument("--base-url", metavar="URL")
    group.add_argument("--timeout", type=float, metavar="SECONDS")
    group.add_argument("--temperature", type=float, metavar="T")
    group.add_argument("--language", metavar="NAME", help="write in this language (default: English)")

    drafting = argparse.ArgumentParser(add_help=False)
    group = drafting.add_argument_group("message options")
    group.add_argument("-c", "--context", metavar="TEXT", help="a short note about the intent of the change")
    group.add_argument("-t", "--type", help="use this commit type")
    group.add_argument("-s", "--scope", help="use this scope")
    group.add_argument("--body", choices=config.CHOICES["body"], help="when to write a body (default: auto)")
    group.add_argument("--offline", action="store_true", help="skip the model and draft from file paths only")
    group.add_argument("--fallback", action="store_true",
                       help="draft from file paths when the model is unavailable")
    group.add_argument("-v", "--verbose", action="store_true", help="report the model, timing and diff size")

    p = sub.add_parser("msg", parents=[drafting, model], help="print a commit message for the staged changes")
    p.set_defaults(func=cmd_msg)

    p = sub.add_parser("commit", parents=[drafting, model], help="draft a message, then run git commit")
    p.add_argument("-y", "--yes", action="store_true", help="commit without opening the editor")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("notes", parents=[model], help="write release notes (default: since the last tag)")
    p.add_argument("range", nargs="?", help="revision range, e.g. v1.0.0..v1.1.0")
    p.add_argument("--title", help="heading for the release (default: the tag, or Unreleased)")
    p.add_argument("-a", "--all", action="store_true",
                   help="also list internal changes: " + ", ".join(sorted(notes.INTERNAL)))
    p.add_argument("--offline", action="store_true", help="skip the model-written summary")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("lint", help="check a commit message from a file or stdin")
    p.add_argument("file", nargs="?", help="message file (default: stdin)")
    p.set_defaults(func=cmd_lint)

    hook = sub.add_parser("hook", help="install or remove the git hooks")
    actions = hook.add_subparsers(dest="action", metavar="<action>", required=True)
    p = actions.add_parser("install", help="install the prepare-commit-msg hook")
    p.add_argument("--lint", action="store_true", help="also install a commit-msg hook that runs lint")
    p.add_argument("--force", action="store_true", help="replace existing hooks (they are kept as backups)")
    p.set_defaults(func=cmd_hook_install)
    p = actions.add_parser("uninstall", help="remove the hooks installed by git-scribe")
    p.set_defaults(func=cmd_hook_uninstall)
    p = actions.add_parser("run", help="what the prepare-commit-msg hook runs")
    p.add_argument("file")
    p.add_argument("source", nargs="?")
    p.add_argument("sha", nargs="?")
    p.set_defaults(func=cmd_hook_run)

    p = sub.add_parser("config", help="show the effective settings and where they come from")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", parents=[model], help="check the repository, model server and hooks")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except (GitError, ConfigError, LLMError, NothingStaged, OSError) as exc:
        ui.say(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
