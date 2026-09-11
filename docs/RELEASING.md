# Releasing Nokshi

Status: **0.3.0 is published** — https://pypi.org/project/nokshi/

Publishing is automated. [`.github/workflows/publish.yml`](../.github/workflows/publish.yml)
builds and uploads to PyPI whenever a `v*` tag is pushed, authenticating via PyPI
**Trusted Publishing** — there is no API token stored anywhere.

---

## One-time setup (done)

- [x] **PyPI pending publisher** — Account → Publishing, pointing at
      owner `programmerzia`, repo `nokshi`, workflow `publish.yml`, environment `pypi`.
- [x] **GitHub environment** named exactly `pypi` (Settings → Environments).
      `publish.yml` references it; the job fails without it.

## Cutting a release

1. Bump the version in [`nokshi/__init__.py`](../nokshi/__init__.py) — the single source
   of truth. `pyproject.toml` reads it via `[tool.setuptools.dynamic]`; never edit it there.
2. Move the `[Unreleased]` items in [`CHANGELOG.md`](../CHANGELOG.md) under the new version
   with today's date, and add fresh compare links at the bottom.
3. Verify locally:
   ```bash
   pip install -e ".[dev]"
   ruff check nokshi tests
   pytest -q                       # 35 tests
   ```
4. Commit, then tag and push:
   ```bash
   git commit -am "Release 0.4.0"
   git push
   git tag -a v0.4.0 -m "v0.4.0"
   git push --tags
   ```
5. Watch the **Actions** tab. On success, confirm from the outside:
   ```bash
   pipx install nokshi && nokshi --version
   ```
6. Create a **GitHub Release** from the tag and paste the CHANGELOG entry.

> **PyPI versions are immutable.** A version can never be re-uploaded — only yanked and
> replaced by a new one. If you want to rehearse, build locally and install the wheel into
> a throwaway venv first:
> ```bash
> rm -rf dist && python -m build && twine check dist/*
> python3 -m venv /tmp/t && /tmp/t/bin/pip install dist/nokshi-*.whl && /tmp/t/bin/nokshi --version
> ```

## Version numbers

Pre-1.0, the middle number carries breaking changes:

| Bump | Trigger |
|---|---|
| `0.3.x` patch | bug fixes, parser fixes, ranking changes on the same inputs |
| `0.4.0` minor | new command or flag, new language parser, added config keys |
| `0.4.0` breaking | a flag or config key renamed/removed, output format changed |
| `1.0.0` | you are willing to promise the CLI and `config.toml` schema are stable |

**Before tagging 1.0**, add a `schema_version` row to `.nokshi/index.db` and, on mismatch,
tell the user to re-run `nokshi analyze` rather than crashing on a missing column. The index
is a rebuildable cache, so this is cheap — and it decouples DB changes from the version
number entirely. The two things that are genuinely expensive to change after 1.0 are the
ones living in users' repos: **`.nokshi/config.toml` key names** and the **index schema**.

## Remaining polish

- [ ] Repo description + topics: `ai`, `context`, `developer-tools`, `llm`, `code-intelligence`
- [ ] Demo GIF at the top of the README — 20-30s of `nokshi ctx "..." --explain` on a real
      repo (`asciinema` + `agg`, or `vhs`). The pitch is a 99% reduction claim; people
      believe that by watching it, not reading it.
- [ ] Branch protection on `main` requiring the `test` check
- [ ] `schema_version` handling (see above)
