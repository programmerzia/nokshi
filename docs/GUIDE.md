# Nokshi — Quick Guide

*Intelligent Context for AI Engineering · by CoreBari*

---

## Part 1 — Using Nokshi

### Install (once)

```bash
cd Nokshi/nokshi-0.3.0          # the folder that contains pyproject.toml
pipx install .
pipx inject nokshi tiktoken     # optional: exact token counts
nokshi --version
```

No pipx? `sudo apt install pipx && pipx ensurepath`, then open a new terminal.

### Set up a repository (once per repo)

```bash
cd ~/code/corebari
nokshi init          # creates .nokshi/config.toml and .nokshi/rules.md  → commit both
nokshi analyze       # indexes the repo (~10 s for 3,000 files; incremental afterwards)
nokshi status        # what Nokshi knows: frameworks, files, symbols, edges
```

Open `.nokshi/rules.md` and write 5–10 real rules, e.g.
`- Business logic lives in Services; controllers only validate and delegate.`
They go into every context package and every review.

### The 5 commands you'll actually use

| Command | What it does | Needs API key? |
|---|---|---|
| `nokshi ctx "task" -c` | Builds the context package for a task and copies it to the clipboard. Paste into Cursor, Claude, ChatGPT, a local model — anything. | No |
| `nokshi task "task"` | Full pipeline: plan → you confirm → implemented in an isolated git worktree → tests → review → you approve. | Yes |
| `nokshi review` | Reviews your current uncommitted diff against the task and your rules. | Yes |
| `nokshi search Foo` / `nokshi graph Foo` | Find symbols; see what a file depends on and what depends on it. | No |
| `nokshi cost` | Tokens, dollars, and savings vs sending the whole repo. | No |

Useful flags (same on every command that takes them):

```
-c            copy to clipboard        --budget 20000   token budget
-o file.md    write to file            --explain        show why each file was chosen
-f path       force a file in          --dry-run        print the prompt, call nothing
--cheap       use the cheap model      --model X        use a specific model
```

### Typical day

```bash
nokshi ctx "Add refund endpoint to PaymentController" --explain -c   # check the picks, paste
# ...work in Cursor / Claude...
nokshi review                                                         # before committing
nokshi remember "Refunds are idempotent by charge id" --kind decision # keep decisions
```

### Choosing a model

Edit `.nokshi/config.toml`:

```toml
[provider]
name = "anthropic"        # anthropic | openai | openrouter | local | none
model = "claude-sonnet-4-6"
cheap_model = "claude-haiku-4-5-20251001"
```

Keys come from the environment: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.
Local models (Ollama, LM Studio): `name = "local"`, then `NOKSHI_API_BASE=http://localhost:11434/v1`.
Override per shell with `NOKSHI_PROVIDER=... NOKSHI_MODEL=...`.

### If the context picks the wrong files

1. Run with `--explain` and read the signals.
2. Name the file or class in the task ("…in PaymentService") — explicit references score ×3.
3. Force files with `-f`.
4. Tune weights under `[ranking]` in `config.toml`.
5. Run `nokshi summarize --dry-run` then `nokshi summarize` — AI summaries improve ranking on large files (cheap model, cached).

### Safety notes

- `.env*`, keys, certificates are never indexed; credential-looking values are redacted before anything leaves the machine.
- `ctx`, `search`, `graph` and any `--dry-run` never touch the network.
- Agents work in `.nokshi/worktrees/`; your working tree changes only when you press **a**pply.
- `nokshi ws --prune` removes leftover worktrees.

---

## Part 2 — Publishing Nokshi

### A. GitHub

```bash
cd Nokshi/nokshi-0.3.0
git init && git add -A && git commit -m "Nokshi 0.3.0 — Intelligent Context for AI Engineering"
gh repo create nokshi --public --source=. --push        # or create the repo in the browser and:
# git remote add origin git@github.com:<you-or-corebari>/nokshi.git && git push -u origin main
```

Recommended repo settings: description = the tagline; topics `ai`, `context`, `developer-tools`, `llm`, `code-intelligence`; add a 20–30 s GIF of `nokshi ctx "…" --explain` to the README.

### B. PyPI (so anyone can `pipx install nokshi`)

1. Create accounts at pypi.org and test.pypi.org; enable 2FA; create an **API token** (scope: entire account for the first upload).
2. Bump the version in `nokshi/__init__.py` — that is the single source of truth;
   `pyproject.toml` reads it via `[tool.setuptools.dynamic]`.
3. Build and upload:

```bash
pip install --upgrade build twine
rm -rf dist && python -m build                 # creates dist/nokshi-0.3.0.tar.gz + .whl
twine check dist/*
twine upload --repository testpypi dist/*      # dry run on TestPyPI
pipx install --index-url https://test.pypi.org/simple/ --pip-args="--extra-index-url https://pypi.org/simple/" nokshi
twine upload dist/*                            # real PyPI
```

Twine asks for a username (`__token__`) and the token as password. After this, `pipx install nokshi` works for everyone.

4. Tag the release: `git tag v0.3.0 && git push --tags`, then create a GitHub Release from the tag and attach `dist/*`.

### C. Automate later (optional)

Add `.github/workflows/publish.yml` using PyPI **Trusted Publishing** (no token stored): on `push` of a `v*` tag → `python -m build` → `pypa/gh-action-pypi-publish`. Also a `test.yml` running `pytest -q` and `ruff check` on every PR.

### D. Release checklist

- [ ] `pytest -q` and `ruff check nokshi tests` pass
- [ ] version bumped in `nokshi/__init__.py`; `nokshi --version` shows it
- [ ] README "Roadmap mapping" updated
- [ ] `CHANGELOG.md` entry (add this file on the first public release)
- [ ] TestPyPI install works in a fresh venv
- [ ] tag + GitHub Release

### Versioning rule of thumb

`0.x.y` while you're the only user. Tag `1.0.0` after a week of daily use with no surprises — that's the version you'd mention in interviews.
