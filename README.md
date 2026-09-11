# Nokshi — Intelligent Context for AI Engineering

`nokshi` v0.3 · by [CoreBari](https://corebari.com) · MIT

**Deterministic repository intelligence, token-budgeted context packages, and agents that run in isolated worktrees.**

*Nokshi* (নকশি) — as in *nokshi kantha*, the Bengali quilt stitched from carefully chosen pieces into
one pattern. Nokshi does that to a codebase: only the right pieces, stitched into a context package.

`nokshi` indexes a repository deterministically (no LLM), builds a graph of files, symbols and
dependencies, and for any task produces a *token-budgeted context package* containing only what an
AI model needs to make the right decision. It also runs a Planner and a Reviewer agent on top of that
context, and tracks every token and dollar spent.

This is Phases 0–5 and 7 of the ForgeMind roadmap (the project's original working name) as a single, installable daily-use tool
(see *Roadmap mapping* below), including safe agent execution in git worktrees (`nokshi run`).

```
$ nokshi context "Add refund support to the payment flow" -c
✓ 15 files considered
✓ 3 full · 1 regions · 4 signatures · 6 mapped
✓ Context: 5,120 tokens (budget 12,000 incl. 800 reserve)
✓ Reduction vs whole repo: 99% (574,659 → 5,120)
✓ Context package copied to clipboard
```

Tested on laravel/framework (3,233 files, 40k symbols — indexes in ~10s, context in ~1s),
fastapi/fastapi, dotnet/eShop and vercel/commerce.

---

## Install (Ubuntu)

```bash
# Python 3.11+ required
pipx install ./nokshi            # recommended — isolated, gives you the `nokshi` command
# publishing: `pip install build && python -m build` → upload to PyPI (name `nokshi` is unclaimed)
# or:  pip install --user ./nokshi
# optional: exact token counts (otherwise a calibrated heuristic is used)
pipx inject nokshi tiktoken
# optional: clipboard support for -c / --clip
sudo apt install wl-clipboard       # Wayland   (or: xclip for X11)
```

Set an API key only if you want `nokshi plan / review / ask` to call a model:

```bash
export ANTHROPIC_API_KEY=...        # default provider
# or OPENAI_API_KEY / OPENROUTER_API_KEY, or a local model — see Providers
```

## 60-second start

```bash
cd ~/code/corebari
nokshi init                # creates .nokshi/config.toml + rules.md (commit these)
nokshi analyze             # index: frameworks, symbols, dependency graph, token sizes
nokshi status              # what nokshi knows about the repo
```

Edit `.nokshi/rules.md` with your real project rules — they go into every context package and review.

---

## Daily workflow

### 1. Get the right context for a task (no model call, free)

```bash
nokshi context "Add Stripe refund support to PaymentService" -c        # → clipboard
nokshi context "..." -o /tmp/ctx.md                                     # → file
nokshi ctx "..." --budget 20000 --role developer                        # `ctx` alias; bigger budget, developer instructions
nokshi context "..." -f app/Services/PaymentService.php -f config/payments.php   # force files in
nokshi context "..." --explain                                          # see WHY each file was chosen
nokshi context "..." --signatures-only                                  # API-shape only, very cheap
nokshi context "..." --json                                             # machine-readable selection
```

Paste the package into Claude, Claude Code, Cursor, ChatGPT — anything. The package contains: the task,
repo summary (frameworks, modules), your rules and memories, then files in three representations:

| Tier | Representation | When |
|------|----------------|------|
| 1 | **full source** | strongest matches, file ≤ ~2.5k tokens |
| 1 | **relevant regions** | strongest matches but large file → only the matching methods/classes with line numbers |
| 2 | **signatures only** | related files: class/method signatures with line ranges |
| 3 | **file map** | possibly relevant, listed with token size so the model can ask for them |

Everything fits an explicit budget, enforced *before* anything is sent anywhere.

### 2. Pipe straight into Claude Code

```bash
nokshi context "Add refund support" --role developer | claude -p "Implement this. Return diffs."
nokshi context "Why does invoice status not update?" | claude -p "Answer using only this context."
```

### 3. Plan before you code

```bash
nokshi plan "Add Stripe refund support"           # Planner agent (budget: [budget].planner, default 6k)
nokshi plan "..." --dry-run                       # just print the prompt, call nothing
```

### 4. Review your diff before committing

```bash
nokshi review                                     # working tree incl. untracked files
nokshi review --staged --task "Refund endpoint"   # what's staged, with intent
nokshi review --base main                         # whole branch vs main
```

The reviewer gets the diff, your rules, and *signatures of the surrounding code* (never re-sends the
changed files in full — the diff already has them). Output: critical issues, warnings, suggestions, score.

### 4b. Let the Developer agent implement it — safely (`nokshi run`)

```bash
nokshi run "Add Stripe refund support to PaymentService"
nokshi run "..." --test "vendor/bin/pest --filter=Refund"     # custom test command
nokshi run "..." --keep                                        # commit on a forge/ branch, don't ask
nokshi run "..." --show-diff --retries 2
nokshi run "..." --dry-run                                     # only print the developer prompt
```

State machine (roadmap §12): `RETRIEVING_CONTEXT → CREATING_WORKSPACE → EXECUTING → TESTING →
(RETRYING) → WAITING_FOR_APPROVAL → COMPLETED`.

1. Builds a developer-role context package (budget `[budget].developer`, default 16k).
2. Creates a **git worktree** on a fresh `nokshi/<task>-<timestamp>` branch under
   `.nokshi/worktrees/`. Your uncommitted work is carried over and snapshotted so the agent sees
   what you see; `vendor/`, `node_modules/`, `.venv/` are symlinked (no reinstall); `.env` is copied.
3. The agent returns unified diffs + `FILE:` blocks for new files. They're applied **only in the
   worktree** (`git apply`, then 3-way, then a conservative `patch --fuzz=1`).
4. Runs your tests there (auto-detected: pest/phpunit/vitest/jest/pytest/dotnet test, or
   `[run].test_command`). On failure or a non-applying patch, one retry sends the agent the exact
   error and the current file contents.
5. You choose: **[a]pply** to your working tree (unstaged, ready for `git diff`), **[k]eep** the
   branch and worktree to inspect / `git merge`, or **[d]iscard**. A patch that did not apply cleanly
   can never be applied to your tree — only kept or discarded.

```bash
nokshi workspaces            # list nokshi worktrees
nokshi workspaces --prune    # remove them all
```

### 4a. The one-command pipeline: `nokshi task`

```bash
nokshi task "Add Stripe refund support to PaymentService"
```

`PLANNING` (Planner writes the plan, you confirm) → `CREATING_WORKSPACE` → `EXECUTING` (the **Weaver**
implements it) → `TESTING` → `REVIEWING` (Reviewer critiques the diff) → `WAITING_FOR_APPROVAL`.
Same flags as `run`, plus `-y` to skip the confirmation and `--skip-plan` / `--no-review`.
`plan`, `run` and `review` remain available as standalone primitives.

### 4c. AI summaries for large files (cheap model, cached)

```bash
nokshi summarize --dry-run          # how many files, estimated tokens/cost
nokshi summarize --limit 200        # summarize the 200 largest un-summarized source files
nokshi summarize                    # everything ≥ [summaries].min_tokens; re-runs only touch changed files
```

Summaries are written by `[provider].cheap_model` (default Haiku), cached by file hash, and used in
three places: as ranking evidence, as the header of a file's signatures block, and instead of a bare
path in the file map. Without AI summaries, a free structural summary (types, operations, imports)
is generated from the index. Rough cost: ~1.2k input tokens per file — a 1,500-file codebase is a
few dollars on Haiku, once.

**Model routing:** add `--cheap` to `plan`, `ask`, `review` or `run` to use the cheap model for that
call; `--model X` overrides explicitly.

### 5. Ask questions about the codebase

```bash
nokshi ask "Where is invoice status changed and by what?"
```

### 6. Navigate deterministically (instant, offline)

```bash
nokshi search charge                  # symbols named like this, with locations
nokshi graph PaymentService           # what it depends on, what depends on it
nokshi graph app/Models/Invoice.php --symbols
```

### 7. Project memory

```bash
nokshi remember "Refunds must be idempotent by charge id" --kind decision
nokshi remember "Invoice::recalculate() is slow on >10k lines" --kind bug
nokshi memory                         # show rules + memories
nokshi memory --forget 3
```

Memories and `rules.md` are injected into every package and also boost files they mention.

### 8. See what you spend and save

```bash
nokshi cost               # last 30 days: calls, tokens, $ by agent/model, context savings vs naive
nokshi cost --days 7
```

---

## How ranking works (`--explain`)

Every file gets a deterministic score, a weighted sum of:

- **lexical** — BM25 over path words (×3), symbol names (×2), imports, namespace, and the file's top
  identifiers (camelCase/snake_case split), so "yield cleanup" finds the file that *contains* those words
- **explicit_reference** — the task names a path or an identifier-looking symbol (`PaymentService`,
  `user_repo`); plain English words like "config" don't count
- **dependency** — 1–2 hop expansion from the top seeds via import + reference edges, with a
  hub penalty (files everything touches carry little signal) and a cap
- **module_match**, **git_recency**, **test_relevance** (tests of the selected source files),
  **memory** (files mentioned in rules/memories)
- Priors: docs/config files ×0.55; unrelated tests ×0.6 unless the task is about tests

Weights live in `.nokshi/config.toml [ranking]`. If nokshi keeps picking the wrong files for a
repo, run `--explain`, then tune weights or add rules that name the right modules.

## Dependency graph

| Language | Import edges | Notes |
|----------|--------------|-------|
| PHP | `use` (incl. group use) via composer PSR-4; fallback by unique class name | Laravel/Symfony |
| TS/JS/Vue | relative paths, `tsconfig paths` aliases, `@/` convention, `require()`, dynamic `import()` | probes .ts/.tsx/.js/.vue/index.* |
| C# | `using Namespace` → files declaring that namespace (weight 0.6) | file-scoped and block namespaces |
| Python | absolute and relative imports, package `__init__` | via stdlib `ast` |
| all | **reference edges**: file mentions a type/function uniquely defined elsewhere | catches DI/container resolution |

## Providers

`.nokshi/config.toml`:

```toml
[provider]
name = "anthropic"                        # anthropic | openai | openrouter | local | none
model = "claude-sonnet-4-6"
cheap_model = "claude-haiku-4-5-20251001"
```

- `NOKSHI_PROVIDER` / `NOKSHI_MODEL` env vars override per shell.
- `local` = any OpenAI-compatible server (Ollama, LM Studio, vLLM). Default base
  `http://localhost:11434/v1`; override with `NOKSHI_API_BASE`. Cost is logged as $0.
- `NOKSHI_API_BASE` also redirects anthropic/openai/openrouter through a proxy or gateway.
- Prices are in `nokshi/providers.py::PRICING` (USD per 1M tokens) — update when they change.

## Safety

- `.env*`, keys, certificates, `secrets.*`, credentials files are never indexed.
- Content leaving the machine passes through `redact()`: quoted values after `password/secret/token/
  api_key`-style keys, `Password=…` in connection strings, and well-known key formats (`sk-…`,
  `sk_live_…`, `ghp_…`, `AKIA…`, JWTs) become `***REDACTED***`.
- `nokshi context` and `--dry-run` never contact a network. Only `plan / review / ask / run` without
  `--dry-run` call a model, and each call is logged with tokens and cost.
- `nokshi run` never writes to your working tree until you approve; agent file paths are validated
  (no `..`, no absolute paths); `.rej`/`.orig` leftovers are removed; provider errors clean up the worktree.
- The index is a single SQLite file at `.nokshi/index.db` (git-ignored automatically).

## Config reference

```toml
[index]
ignore_dirs = ["storage/framework"]   # added to sensible defaults (vendor, node_modules, dist, bin/obj…)
ignore_files = ["*.generated.cs"]
max_file_kb = 512                      # bigger files: metadata only

[budget]
planner = 6000
developer = 16000
reviewer = 10000
context = 12000                        # default for `nokshi context`
reserve = 800                          # kept free for the system prompt / question

[summaries]
min_tokens = 600                       # files smaller than this are never summarized
concurrency = 4

[run]
test_command = ""                      # empty = auto-detect
test_timeout = 600
symlink = ["vendor", "node_modules", ".venv"]
max_retries = 1

[ranking]
lexical = 1.0
explicit_reference = 3.0
dependency = 0.6
git_recency = 0.3
module_match = 0.4
test_relevance = 0.5
memory = 0.5
```

`.nokshiignore` in the repo root (one pattern per line, `dir/` for directories) is also honoured, and
`.gitignore` is respected automatically inside git repositories.

## Development

See [docs/GUIDE.md](docs/GUIDE.md) for the quick guide, [docs/RELEASING.md](docs/RELEASING.md)
for the release process, and [CHANGELOG.md](CHANGELOG.md) for release notes.

```bash
pip install -e ".[dev]"
ruff check nokshi tests
pytest -q                    # 35 tests: parsers, graph, incremental index, ranking, budgets, redaction, worktrees, summaries, CLI
```

Layout (one distribution, subpackages named after the architecture):

```
nokshi/
├── core/        config, SQLite index (schema mirrors the roadmap), token counting, scanner,
│                parsers/ (python via ast; php, js/ts/vue, csharp via tolerant regex + brace matching), graph
├── context/     ranking (all signals), builder (tiers + budgets + redaction), summaries
├── agents/      providers (Anthropic / OpenAI / OpenRouter / local), system prompts for Planner, Weaver, Reviewer
├── memory/      rules.md + remembered decisions
├── workspace/   git worktrees, patch apply, test runner
└── cli.py       the `nokshi` command
```

## Roadmap mapping

| Roadmap section | Status in v0.1 |
|-----------------|----------------|
| §4 Repository intelligence, framework detection, symbols, deps | ✅ deterministic, incremental |
| §5 Multi-level retrieval (metadata / symbols / regions / expansion) | ✅ 3 tiers + dependency expansion |
| §6 Token budgeting, ranking signals | ✅ enforced pre-call; all 8 signals |
| §7 Representations (full / regions / summaries / signatures) | ✅ incl. cached AI summaries |
| §8 Project memory, incremental invalidation | ✅ rules.md + `nokshi remember`; hash-based re-index |
| §10 Planner / Weaver (developer) / Reviewer agents, orchestrated by `nokshi task` | ✅ (Test agent: tests are run deterministically; generation is part of the Developer contract) |
| §11 Safe worktree execution, §12 state machine | ✅ `nokshi run`: worktree → patch → tests → retry → human approval |
| §13 Stack | SQLite instead of Postgres/pgvector/Redis — right size for a per-developer tool; schema is portable |
| §16 Token savings 1–12 | ✅ (routing is explicit via `--cheap` / `cheap_model` for summaries) |
| §17 Cost dashboard | ✅ CLI (`nokshi cost`) |
| §18 MVP checklist | ✅ complete |

Deliberate deviations: regex+brace parsers instead of tree-sitter (zero native deps, handles the four
stacks you use; the interface is designed for tree-sitter to slot in per language), BM25 + content
terms instead of embeddings (no model call at index time; embeddings can be added as one more signal).

---

Nokshi is built and maintained by [CoreBari](https://corebari.com). It never requires a CoreBari account
and works fully offline except for the model calls you explicitly make.
