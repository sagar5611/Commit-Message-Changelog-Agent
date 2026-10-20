# git-scribe

Conventional Commit messages from your staged diff and release notes from your history, written by a small local model.

git-scribe reads what you have staged, filters and redacts the diff, and asks a model on your machine (Ollama with `qwen2.5-coder:3b` by default) for a commit message that follows [Conventional Commits](https://www.conventionalcommits.org/). It can also turn the commits since your last tag into Markdown release notes. Apart from git, it only needs the Python standard library.

## Features

- Commit messages that follow the types, scopes and style your repository already uses.
- Cleanup tuned for small models: `<think>` blocks, code fences and trailing explanations are stripped, aliases such as `feature` and `bugfix` become `feat` and `fix`, the subject length is enforced and the body is wrapped at 72 columns. A reply that still isn't a valid message gets one self-correction round.
- An offline fallback that drafts a message from file paths and the branch name.
- Release notes grouped by type with breaking changes first, issue and commit links for GitHub and GitLab, and an optional summary written by the model.
- Conventional Commits linting: header format, allowed types and scopes, subject length, trailing period and the blank line before the body.
- Works with Ollama or any server with an OpenAI-compatible chat completions API.

## Privacy

If the model runs locally, your code never leaves your machine. In addition:

- The model never sees the contents of lockfiles, binary files, assets (images, fonts, media, PDFs, archives), generated files (minified bundles, source maps, protobuf output, snapshots, anything under `dist/`, `vendor/` or `node_modules/`), sensitive files (`.env`, `.env.*`, `*.pem`, `*.key`, keystores, SSH keys, `.npmrc`, `.pypirc`, `.netrc`) or files matching `scribe.exclude`. It only gets their names and line counts.
- Common secret formats in the rest of the diff are replaced with `[REDACTED]` before the prompt is built: private key blocks, AWS access key IDs, GitHub and Slack tokens, `sk-` API keys, Google API keys, and quoted values assigned to names like `password`, `secret`, `token` or `api_key`. This is pattern matching, so it won't catch every secret.
- A `.gitscribe` file committed to a repository can only change style settings. It can't set the provider, server URL or API key, so a cloned repository can't send your diffs to a different server.
- Requests to `localhost`, `127.0.0.1` and `::1` never go through `HTTP_PROXY` or `HTTPS_PROXY`.

## Requirements

- Python 3.9 or newer
- git
- [Ollama](https://ollama.com), or another server with an OpenAI-compatible API

## Installation

From a checkout of this repository:

```sh
pipx install .        # or: pip install .
ollama pull qwen2.5-coder:3b
```

This installs the `git-scribe` command; `python -m git_scribe` runs the same entry point. Ollama needs to be running (`ollama serve`) for anything that uses the model.

<!-- TODO: command reference. src/git_scribe/cli.py, which provides main(), is not in the repository yet. -->

## Configuration

Settings are read from these sources, highest priority first:

1. Command-line flags
2. `SCRIBE_*` environment variables
3. git config keys under `scribe.`, from any scope: `git config --global scribe.model qwen2.5-coder:7b` applies everywhere, `git config scribe.scopes "api cli"` only in the current repository
4. A `.gitscribe` file at the repository root (style settings only, see [Per-repository settings](#per-repository-settings))
5. Built-in defaults

| git config key | Default | Description |
| --- | --- | --- |
| `scribe.provider` | `ollama` | `ollama`, or `openai` for any OpenAI-compatible server |
| `scribe.model` | `qwen2.5-coder:3b` | Model name |
| `scribe.baseUrl` | see [Model server](#model-server) | Server URL |
| `scribe.apiKey` | empty | Bearer token for OpenAI-compatible servers; falls back to `OPENAI_API_KEY` |
| `scribe.timeout` | `90` | Seconds to wait for the model |
| `scribe.hookTimeout` | `45` | Seconds to wait for the model inside the git hook |
| `scribe.hookFallback` | `none` | What the hook does when the model is unavailable: `none`, or `heuristic` for the offline draft |
| `scribe.temperature` | `0.2` | Sampling temperature |
| `scribe.contextWindow` | `8192` | Context window requested from Ollama (`num_ctx`) |
| `scribe.maxDiffChars` | `12000` | Characters of diff sent to the model. Small files are kept whole; large ones share what's left and are truncated |
| `scribe.maxSubjectLength` | `72` | Maximum length of the first line |
| `scribe.body` | `auto` | `auto` lets the model add a short body when the change isn't obvious from the first line, `always` asks for 1-4 bullets, `never` keeps only the first line and any `BREAKING CHANGE:` footer |
| `scribe.types` | `feat fix docs style refactor perf test build ci chore revert` | Allowed commit types; custom types are fine |
| `scribe.scopes` | empty | Allowed scopes. When set, the model is asked to use one of them, any other scope in its reply is dropped, and the linter rejects other scopes |
| `scribe.language` | empty (English) | Language for the description, body and release-notes summary; type and scope stay in English |
| `scribe.exclude` | empty | Glob patterns, matched against the path and the file name, whose diff is never sent |
| `scribe.enabled` | `true` | On/off switch |
| `scribe.remote` | `origin` | Remote used for links in release notes |

Each environment variable is `SCRIBE_` plus the setting name in upper snake case, so `scribe.maxDiffChars` becomes `SCRIBE_MAX_DIFF_CHARS`. Lists take commas or spaces; booleans take `true`/`false`, `yes`/`no`, `on`/`off` or `1`/`0`. Invalid values are reported with their source, for example `invalid value 'maybe' for scribe.enabled (environment)`.

### Model server

With the `ollama` provider, the server URL comes from `OLLAMA_HOST` if it's set, otherwise `http://localhost:11434`. `OLLAMA_HOST` can be a bare host or `host:port`: the scheme defaults to `http`, the port to `11434`, and `0.0.0.0` is rewritten to `127.0.0.1`.

With the `openai` provider, the default is `http://localhost:8080/v1`. Point `scribe.baseUrl` at your server, including the `/v1` part:

```sh
git config --global scribe.provider openai
git config --global scribe.baseUrl http://localhost:8000/v1
git config --global scribe.model your-model-name
export OPENAI_API_KEY=...    # only if the server needs a key
```

Keep API keys in the environment rather than in `scribe.apiKey`: git config files are plain text and often end up in dotfiles repositories.

### Per-repository settings

A `.gitscribe` file at the repository root uses git config syntax. Commit it so everyone on the project gets the same conventions:

```ini
[scribe]
    types = feat, fix, docs, refactor, test, chore
    scopes = api, cli, docs
    maxSubjectLength = 72
    exclude = *.generated.ts, fixtures/*
```

Only `model`, `types`, `scopes`, `maxSubjectLength`, `body`, `language`, `exclude` and `maxDiffChars` are read from this file. Anything else, such as `provider`, `baseUrl` or `apiKey`, is ignored. Your own git config and environment still take priority over it.

## How it works

### Commit messages

1. git-scribe reads the staged changes (`git diff --cached` with rename detection) and stops if nothing is staged.
2. It hides and redacts content as described under [Privacy](#privacy), then fits the rest of the diff into `maxDiffChars`.
3. The prompt contains your types and rules, the branch name, up to six recent Conventional Commit subjects, the staged files with line counts, the diff and, if you give one, a note about the intent of the change.
4. The reply is cleaned up and checked. If the first line isn't a valid header or is too long, the model sees its reply and what's wrong with it and gets one more try. If the second reply is a sensible sentence without a type, the type and scope come from the offline draft and a warning is shown. A header that's still too long is cut at a word boundary.

The offline draft only looks at paths and the branch name:

| Staged changes | Draft |
| --- | --- |
| New `src/widgets/export.py` | `feat(widgets): add export.py` |
| Edits to `src/widgets/cli.py` and `src/widgets/tpl.py` on branch `fix/empty-config` | `fix(widgets): update cli.py and tpl.py` |
| Only `package.json` and `package-lock.json` | `chore(deps): update dependencies` |
| New `README.md` | `docs: add README.md` |
| New `tests/test_api.py` | `test: add test_api.py` |

If every file is documentation, the type is `docs`; the same goes for tests (`test`), CI files (`ci`) and build files (`build`). Anything else takes its type from a branch prefix such as `feat/` or `fix/`, then `feat` if a new source file was added, then `chore`. The scope is the first directory that isn't a generic name like `src`, `lib`, `app` or `packages`, if all files share it.

### Release notes

- By default the notes cover the latest tag up to `HEAD`. If `HEAD` itself is tagged, they cover the previous tag up to `HEAD`, so you can tag a release first and then write its notes. Any revision range, such as `v1.2.0..v1.3.0`, works too.
- Merge commits and `fixup!`, `squash!` and `amend!` commits are skipped. Repeated subjects, such as cherry-picks, are listed once.
- Subjects are parsed leniently, so `Feature: ...` and `bugfix(api): ...` land in the right section. `Revert "..."` commits go under Reverts; anything else that isn't conventional goes under Other changes.
- Breaking changes (`!` or a `BREAKING CHANGE:` footer) come first, with the footer text under the entry.
- `refactor`, `docs`, `build`, `ci`, `test`, `style` and `chore` commits count as internal and are left out unless you ask for all sections. Custom types from `scribe.types` are always included.
- A trailing `(#123)`, as left by squash merges, becomes an issue link. For GitHub and GitLab remotes, commit hashes and the compare range are linked too. Credentials in the remote URL are never copied into links.
- The title is the one you pass, else the tag at the end of the range, else `Unreleased`. The date is the date of the newest commit.
- When a model is available, it adds a one- to three-sentence summary under the title. If it can't be reached, you get the notes without the summary.

Example output, generated without a model:

```markdown
## v1.3.0 (2026-10-20)

### Breaking changes

- **api:** drop the v1 endpoints ([839949a](https://github.com/acme/widgets/commit/839949ac634721752bcc66e223c9d644860d05c4))
  - the /v1 routes are gone; use /v2 instead.

### Features

- **api:** add pagination to the list endpoint ([#41](https://github.com/acme/widgets/issues/41)) ([76bc6a6](https://github.com/acme/widgets/commit/76bc6a6eb435bbc5fc9fd794254681c88a6dc4d6))

### Bug fixes

- **cli:** handle empty config files ([f199d7c](https://github.com/acme/widgets/commit/f199d7c94ac8f211fa0ff469d89ba2bf67775977))

### Other changes

- Fix typo in error message ([1126ad5](https://github.com/acme/widgets/commit/1126ad51be4e6a1f20dc7124b9eb9d444152527a))

Full changelog: [v1.2.0...v1.3.0](https://github.com/acme/widgets/compare/v1.2.0...v1.3.0)
```

## Python API

The modules can be used directly:

```python
from git_scribe import commitmsg, config, notes
from git_scribe.gitutil import repo_root

root = repo_root()
settings = config.load(root)    # keyword arguments override settings, e.g. model="qwen2.5-coder:7b"

draft = commitmsg.draft(root, settings, context="needed for the S3 export")
print(draft.message)            # draft.source is "model" or "heuristic"; check draft.warning too

offline = commitmsg.draft(root, settings, use_llm=False)

release = notes.build(root, settings, "v1.2.0..v1.3.0")
print(notes.render(release))
```

`commitmsg.draft()` also accepts `type_hint`, `scope_hint`, `timeout` and `temperature`, and raises `llm.LLMError` if the model can't be reached. `notes.build()` accepts `title`, `include_all`, `use_llm` and `timeout`; pass the same `include_all` to `notes.render()`. Other failures raise `commitmsg.NothingStaged`, `gitutil.GitError` or `config.ConfigError`.

## Project layout

```text
src/git_scribe/
├── commitmsg.py      file triage, redaction, prompts and the commit-message pipeline
├── notes.py          release notes for a range of commits
├── conventional.py   Conventional Commits parsing, reply cleanup and linting
├── config.py         settings and where they come from
├── llm.py            stdlib HTTP clients for Ollama and OpenAI-compatible APIs
├── gitutil.py        thin wrappers around the git CLI
└── ui.py             stderr messages and the progress spinner
```

## License

MIT. See [LICENSE](LICENSE).
