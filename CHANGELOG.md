# Changelog

All notable changes to Nokshi are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-09-11

First public release.

### Added

- **Deterministic index** — SQLite index of files, symbols and dependency edges at
  `.nokshi/index.db`, built with no model call. Incremental on re-run.
- **Parsers** — Python (stdlib `ast`), PHP, JavaScript/TypeScript/Vue and C#
  (tolerant regex + brace matching).
- **Dependency graph** — import edges per language (PSR-4, tsconfig paths, `using`
  namespaces, Python absolute/relative) plus reference edges for types resolved
  through DI or a container.
- **Token-budgeted context packages** — `nokshi context` / `ctx` selects files by a
  weighted deterministic score and emits them in four tiers (full source, relevant
  regions, signatures, file map) under an explicit budget. `--explain` shows why each
  file was chosen.
- **Agents** — `nokshi plan`, `review`, `ask`, and `run` (Developer agent executing in
  an isolated git worktree with test runs and an apply/keep/discard gate), plus
  `nokshi task` as the one-command plan → implement → test → review pipeline.
- **AI file summaries** — `nokshi summarize`, written by the cheap model and cached by
  file hash; used as ranking evidence and in signature/file-map headers.
- **Project memory** — `rules.md` and `nokshi remember` decisions, injected into every
  package and boosting files they mention.
- **Providers** — Anthropic, OpenAI, OpenRouter, and any OpenAI-compatible local server
  (Ollama, LM Studio, vLLM). Per-call token and cost logging via `nokshi cost`.
- **Safety** — secrets never indexed, `redact()` over outbound content, and no network
  access from `context`, `search`, `graph` or any `--dry-run`.

[Unreleased]: https://github.com/programmerzia/nokshi/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/programmerzia/nokshi/releases/tag/v0.3.0
