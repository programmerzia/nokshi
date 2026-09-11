"""`nokshi` command-line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from nokshi import __version__
from nokshi import memory as memory_mod
from nokshi.agents.providers import SYSTEM_PROMPTS, ProviderError, complete
from nokshi.context.builder import CONFIDENT_SCORE, build_context, changed_files_from_diff
from nokshi.context.summaries import summarize_files
from nokshi.core import tokens
from nokshi.core.config import Config, load_config, write_default_config
from nokshi.core.db import Database
from nokshi.core.graph import Graph
from nokshi.core.scanner import _ignored, index_repository
from nokshi.workspace.worktree import (
    WorkspaceError,
    apply_changes,
    apply_to_working_tree,
    create_workspace,
    detect_test_command,
    list_workspaces,
    parse_agent_output,
    remove_workspace,
    run_tests,
)

# When output is piped (not a TTY) Rich would wrap at 80 columns and truncate paths; use a wide layout.
console = Console(width=None if sys.stdout.isatty() else 160)
err = Console(stderr=True, width=None if sys.stderr.isatty() else 160)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _open(args) -> tuple[Config, Database]:
    cfg = load_config(Path(args.root).resolve() if getattr(args, "root", None) else None)
    if not cfg.db_path.exists() and getattr(args, "cmd", "") not in ("init", "analyze"):
        err.print(f"[red]No index found at {cfg.nokshi_dir}. Run [bold]nokshi analyze[/bold] first.[/red]")
        sys.exit(2)
    return cfg, Database(cfg.db_path)


def _ensure_fresh(cfg: Config, db: Database, quiet: bool = False) -> None:
    """Incrementally re-index before answering so results reflect the working tree."""
    stats = index_repository(cfg, db)
    if not quiet and (stats.added or stats.updated or stats.removed):
        err.print(f"[dim]index refreshed: +{stats.added} ~{stats.updated} -{stats.removed} files[/dim]")


def _copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


def _write_output(text: str, out: str | None, clip: bool, label: str) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8")
        err.print(f"[green]✓[/green] {label} written to [bold]{out}[/bold]")
    if clip:
        if _copy_to_clipboard(text):
            err.print(f"[green]✓[/green] {label} copied to clipboard")
        else:
            err.print("[yellow]No clipboard tool found (install wl-clipboard or xclip); printing instead.[/yellow]")
            console.print(text, markup=False, highlight=False)
    if not out and not clip:
        console.print(text, markup=False, highlight=False)


def _print_context_report(pkg) -> None:
    full = [s for s in pkg.selections if s.representation == "full"]
    regs = [s for s in pkg.selections if s.representation == "regions"]
    sigs = [s for s in pkg.selections if s.representation == "signatures"]
    maps = [s for s in pkg.selections if s.representation == "map"]
    err.print(f"[green]✓[/green] {pkg.considered} files considered")
    err.print(f"[green]✓[/green] {len(full)} full · {len(regs)} regions · {len(sigs)} signatures · {len(maps)} mapped")
    err.print(f"[green]✓[/green] Context: [bold]{pkg.tokens:,}[/bold] tokens "
              f"(budget {pkg.budget:,} incl. {cfg_reserve(pkg):,} reserve · {tokens.backend()})")
    if pkg.baseline_tokens:
        err.print(f"[green]✓[/green] Reduction vs whole repo: [bold]{pkg.savings_pct:.0f}%[/bold] "
                  f"({pkg.baseline_tokens:,} → {pkg.tokens:,})")
    if pkg.truncated:
        err.print("[yellow]! Context truncated to fit budget[/yellow]")
    if pkg.top_score < CONFIDENT_SCORE:
        why = ("[yellow]! No file matched this task[/yellow]" if not pkg.selections
               else f"[yellow]! Low confidence[/yellow] (best match scored {pkg.top_score:.2f}); "
                    "the files below are a weak guess")
        err.print(
            f"{why} — the task did not name anything Nokshi could find.\n"
            "  Name a file, class or module in the task (e.g. \"...in PaymentService\"), or force one in "
            "with -f <path>.\n"
            "  For a whole-repo question, try --signatures-only, or `nokshi status` for the module map."
        )


def cfg_reserve(pkg) -> int:
    return pkg.reserve


def _git(cfg: Config, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cfg.root, capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> None:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    cfg = load_config(root)
    cfg.root = root
    created = write_default_config(cfg)
    console.print(f"[green]✓[/green] {'Created' if created else 'Found'} {cfg.nokshi_dir}")
    console.print(f"  config: {cfg.config_path}\n  rules:  {cfg.rules_path}")
    console.print("Next: [bold]nokshi analyze[/bold]")


def cmd_analyze(args) -> None:
    cfg = load_config(Path(args.root).resolve() if args.root else None)
    write_default_config(cfg)
    db = Database(cfg.db_path)
    with console.status("[bold]Analyzing repository...[/bold]") as status:
        stats = index_repository(cfg, db, full=args.full, progress=lambda m: status.update(f"[bold]Analyzing...[/bold] {m}"))
    console.print(f"[green]✓[/green] Frameworks detected: {', '.join(stats.frameworks) or 'none'}")
    console.print(f"[green]✓[/green] {stats.discovered:,} files discovered "
                  f"(+{stats.added} new, ~{stats.updated} changed, -{stats.removed} removed, {stats.unchanged} unchanged)")
    sym = db.conn.execute("SELECT COUNT(*) n FROM symbols").fetchone()["n"]
    console.print(f"[green]✓[/green] {sym:,} symbols indexed")
    console.print(f"[green]✓[/green] {stats.deps:,} dependency edges")
    total = db.conn.execute("SELECT COALESCE(SUM(tokens),0) t FROM files").fetchone()["t"]
    console.print(f"[green]✓[/green] Repository size: ~{total:,} tokens ({tokens.backend()})  ·  {stats.seconds:.1f}s")
    if stats.languages:
        top = sorted(stats.languages.items(), key=lambda x: -x[1])[:8]
        console.print("  " + ", ".join(f"{l}: {n}" for l, n in top))


def cmd_status(args) -> None:
    cfg, db = _open(args)
    frameworks = json.loads(db.get_meta("frameworks", "[]") or "[]")
    last = float(db.get_meta("last_index", "0") or 0)
    n = db.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(tokens),0) t, COALESCE(SUM(loc),0) l FROM files").fetchone()
    sym = db.conn.execute("SELECT COUNT(*) n FROM symbols").fetchone()["n"]
    deps = db.conn.execute("SELECT COUNT(*) n FROM deps").fetchone()["n"]
    summ = db.conn.execute("SELECT COUNT(*) n FROM files WHERE summary IS NOT NULL AND summary_sha = sha256").fetchone()["n"]
    need = len(db.files_needing_summary(cfg.summaries.min_tokens))
    console.print(Panel.fit(
        f"[bold]{cfg.root.name}[/bold]\n"
        f"Frameworks: {', '.join(frameworks) or '—'}\n"
        f"Files: {n['n']:,}   LOC: {n['l']:,}   Tokens: ~{n['t']:,}\n"
        f"Symbols: {sym:,}   Dependency edges: {deps:,}   AI summaries: {summ:,} (stale/missing: {need})\n"
        f"Last indexed: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last)) if last else 'never'}\n"
        f"Provider: {cfg.provider.name} / {cfg.provider.model}   Token counter: {tokens.backend()}",
        title="Nokshi"))
    table = Table(title="Modules", show_lines=False)
    table.add_column("module"); table.add_column("files", justify="right"); table.add_column("tokens", justify="right")
    for r in db.conn.execute("SELECT module, COUNT(*) n, SUM(tokens) t FROM files GROUP BY module ORDER BY t DESC LIMIT 15"):
        table.add_row(escape(r["module"]), str(r["n"]), f"{r['t']:,}")
    console.print(table)


def cmd_summarize(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db, quiet=True)
    model = cfg.provider.cheap_model or cfg.provider.model
    if args.dry_run:
        stats = summarize_files(cfg, db, limit=args.limit, dry_run=True)
        rows = db.files_needing_summary(cfg.summaries.min_tokens)[: args.limit or None]
        est_in = sum(min(r["tokens"], 1500) + 200 for r in rows)
        console.print(f"{stats.candidates} files need summaries (≥ {cfg.summaries.min_tokens} tokens, changed or never summarized)")
        console.print(f"Estimated input ≈ {est_in:,} tokens on {cfg.provider.name}/{model}")
        for r in rows[:15]:
            console.print(f"  {escape(r['path'])}  [dim]{r['tokens']} tok[/dim]")
        if len(rows) > 15:
            console.print(f"  … {len(rows) - 15} more")
        return
    try:
        with err.status("[bold]Summarizing...[/bold]") as st:
            stats = summarize_files(cfg, db, limit=args.limit, progress=lambda m: st.update(f"[bold]Summarizing...[/bold] {m}"))
    except ProviderError as e:
        err.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[green]✓[/green] {stats.written}/{stats.candidates} summaries written with {cfg.provider.name}/{model}"
                  f"  ·  in {stats.input_tokens:,} / out {stats.output_tokens:,} tokens  ·  ${stats.cost_usd:.4f}")
    if stats.failed:
        console.print(f"[yellow]{stats.failed} failed[/yellow]: " + "; ".join(escape(e) for e in stats.errors))


def cmd_search(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db, quiet=True)
    q = args.query
    like = f"%{q}%"
    rows = db.conn.execute(
        """SELECT s.name, s.kind, s.signature, s.line_start, f.path FROM symbols s JOIN files f ON f.id = s.file_id
           WHERE s.name LIKE ? ORDER BY (s.name = ?) DESC, LENGTH(s.name), f.path LIMIT ?""",
        (like, q, args.limit)).fetchall()
    if not rows:
        files = db.conn.execute("SELECT path FROM files WHERE path LIKE ? LIMIT ?", (like, args.limit)).fetchall()
        if not files:
            console.print(f"No symbols or files matching [bold]{q}[/bold]")
            return
        for f in files:
            console.print(escape(f["path"]))
        return
    table = Table(show_header=True)
    table.add_column("symbol"); table.add_column("kind"); table.add_column("location"); table.add_column("signature")
    for r in rows:
        table.add_row(escape(r["name"]), r["kind"], escape(f"{r['path']}:{r['line_start']}"), escape(r["signature"][:90]))
    console.print(table)


def cmd_graph(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db, quiet=True)
    target = args.target
    row = db.file_by_path(target)
    if not row:
        rows = db.conn.execute(
            "SELECT DISTINCT f.* FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.name = ? "
            "AND s.kind IN ('class','interface','trait','enum','record','struct','function','type') LIMIT 5", (target,)).fetchall()
        if not rows:
            rows = db.conn.execute("SELECT f.* FROM files f WHERE f.path LIKE ? LIMIT 5", (f"%/{target}.%",)).fetchall()
        if not rows:
            rows = db.conn.execute("SELECT f.* FROM files f WHERE f.path LIKE ? LIMIT 5", (f"%{target}%",)).fetchall()
        if len(rows) != 1:
            if not rows:
                console.print(f"Nothing matches [bold]{target}[/bold]")
            else:
                console.print("Ambiguous — did you mean:")
                for r in rows:
                    console.print(f"  {escape(r['path'])}")
            return
        row = rows[0]
    g = Graph(db)
    fid = row["id"]
    by_id = {r["id"]: r["path"] for r in db.all_files()}
    console.print(f"[bold]{escape(row['path'])}[/bold]  ({row['language']}, {row['tokens']} tok)")
    out = sorted(g.out.get(fid, {}).items(), key=lambda x: -x[1])
    inc = sorted(g.inc.get(fid, {}).items(), key=lambda x: -x[1])
    console.print(f"\n[cyan]depends on ({len(out)})[/cyan]")
    for d, w in out[:args.limit]:
        console.print(f"  → {escape(by_id.get(d, '?'))}  [dim]{w:.1f}[/dim]")
    console.print(f"\n[magenta]depended on by ({len(inc)})[/magenta]")
    for s, w in inc[:args.limit]:
        console.print(f"  ← {escape(by_id.get(s, '?'))}  [dim]{w:.1f}[/dim]")
    if args.symbols:
        console.print("\n[bold]symbols[/bold]")
        for s in db.symbols_for(fid):
            console.print(f"  {'  ' if s['parent'] else ''}{escape(s['signature'][:100])}  [dim]L{s['line_start']}[/dim]")


def cmd_context(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    with err.status("[bold]Building context...[/bold]"):
        pkg = build_context(cfg, db, args.task, role=args.role, budget=args.budget,
                            explicit_files=args.files, include_tests=not args.no_tests, no_full=args.signatures_only)
    _print_context_report(pkg)
    if args.explain:
        table = Table(title="Ranking", show_lines=False)
        table.add_column("file"); table.add_column("rep"); table.add_column("score", justify="right"); table.add_column("signals")
        # sort by score, not by tier: the table is there to show what the ranker decided,
        # and tier order hides a high-scoring file that failed to load as source.
        ranked_sel = sorted(pkg.selections, key=lambda s: s.candidate.score, reverse=True)
        for s in ranked_sel[: args.explain_limit]:
            sig = " ".join(f"{k}={v:.2f}" for k, v in sorted(s.candidate.signals.items(), key=lambda x: -x[1]))
            table.add_row(escape(s.candidate.path), s.representation, f"{s.candidate.score:.2f}", sig)
        err.print(table)
    if args.json:
        _write_output(pkg.to_json(), args.out, args.clip, "Context JSON")
    else:
        _write_output(pkg.markdown, args.out, args.clip, "Context package")


def _pick_model(cfg: Config, args) -> str | None:
    if getattr(args, "model", None):
        return args.model
    if getattr(args, "cheap", False):
        return cfg.provider.cheap_model or cfg.provider.model
    return None


def _run_agent(cfg: Config, db: Database, role: str, task: str, user_prompt: str, dry_run: bool,
               model: str | None, out: str | None, clip: bool) -> None:
    provider = cfg.provider.name
    model = model or cfg.provider.model
    system = SYSTEM_PROMPTS[role if role in SYSTEM_PROMPTS else "ask"]
    prompt_tokens = tokens.count(system) + tokens.count(user_prompt)
    err.print(f"[green]✓[/green] Prompt: ~{prompt_tokens:,} tokens → {provider}/{model}")
    if dry_run or provider == "none":
        err.print("[yellow]dry run — not calling the model. Prompt follows.[/yellow]")
        _write_output(user_prompt, out, clip, "Prompt")
        return
    try:
        with err.status(f"[bold]{role.capitalize()} thinking...[/bold]"):
            result = complete(provider, model, system, user_prompt)
    except ProviderError as e:
        err.print(f"[red]Provider error:[/red] {e}")
        err.print("Tip: run again with [bold]--dry-run[/bold] to get the prompt and paste it anywhere.")
        sys.exit(1)
    db.log_usage(task=task[:500], role=role, provider=result.provider, model=result.model,
                 input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                 cache_read=result.cache_read, cost_usd=result.cost_usd)
    _write_output(result.text.strip() + "\n", out, clip, f"{role.capitalize()} output")
    err.print(f"\n[dim]Token report — input: {result.input_tokens:,}  output: {result.output_tokens:,}  "
              f"cache read: {result.cache_read:,}  est. cost: ${result.cost_usd:.4f}[/dim]")


def cmd_plan(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    pkg = build_context(cfg, db, args.task, role="planner", budget=args.budget, explicit_files=args.files)
    _print_context_report(pkg)
    _run_agent(cfg, db, "planner", args.task, pkg.markdown, args.dry_run, _pick_model(cfg, args), args.out, args.clip)


def cmd_ask(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    pkg = build_context(cfg, db, args.question, role="context", budget=args.budget, explicit_files=args.files)
    _print_context_report(pkg)
    prompt = pkg.markdown + f"\n# Question\n\n{args.question}\n"
    _run_agent(cfg, db, "ask", args.question, prompt, args.dry_run, _pick_model(cfg, args), args.out, args.clip)


def cmd_review(args) -> None:
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    if args.base:
        diff = _git(cfg, "diff", f"{args.base}...HEAD") or _git(cfg, "diff", args.base)
        scope = f"vs {args.base}"
    elif args.staged:
        diff = _git(cfg, "diff", "--cached")
        scope = "staged"
    else:
        diff = _git(cfg, "diff", "HEAD") or _git(cfg, "diff")
        untracked = [p for p in _git(cfg, "ls-files", "--others", "--exclude-standard").splitlines() if p.strip()]
        for p in untracked:
            if not _ignored(p, cfg) and (cfg.root / p).is_file() and (cfg.root / p).stat().st_size < 200_000:
                diff += _git(cfg, "diff", "--no-index", "--", "/dev/null", p)
        scope = "working tree"
    if not diff.strip():
        console.print(f"[yellow]No changes to review ({scope}).[/yellow]")
        return
    changed = changed_files_from_diff(diff)
    task = args.task or f"Review the {scope} changes touching: {', '.join(changed[:8])}"
    diff_tokens = tokens.count(diff)
    budget = args.budget or cfg.budgets.reviewer
    if diff_tokens > budget * 0.7:
        err.print(f"[yellow]! Diff is {diff_tokens:,} tokens; trimming surrounding context.[/yellow]")
    ctx_budget = max(1500, budget - diff_tokens)
    # Surrounding context: signatures of changed files' neighbours; never re-send full changed files.
    pkg = build_context(cfg, db, task, role="reviewer", budget=ctx_budget, explicit_files=changed, no_full=True)
    _print_context_report(pkg)
    prompt = pkg.markdown + f"\n# Diff ({scope}, {len(changed)} files)\n\n```diff\n{diff.rstrip()}\n```\n"
    _run_agent(cfg, db, "reviewer", task, prompt, args.dry_run, _pick_model(cfg, args), args.out, args.clip)


def _state(label: str) -> None:
    err.print(f"[bold cyan]▶ {label}[/bold cyan]")


def _complete_or_exit(cfg: Config, db: Database, role: str, task: str, prompt: str, model: str | None):
    provider, model = cfg.provider.name, model or cfg.provider.model
    try:
        label = {"developer": "Weaver", "fix": "Weaver (retry)"}.get(role, role.capitalize())
        with err.status(f"[bold]{label} working...[/bold]"):
            result = complete(provider, model, SYSTEM_PROMPTS[role], prompt, max_tokens=8000)
    except ProviderError as e:
        err.print(f"[red]Provider error:[/red] {e}")
        raise
    db.log_usage(task=task[:500], role="developer", provider=result.provider, model=result.model,
                 input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                 cache_read=result.cache_read, cost_usd=result.cost_usd)
    err.print(f"[dim]  tokens in {result.input_tokens:,} / out {result.output_tokens:,} · ${result.cost_usd:.4f}[/dim]")
    return result


def cmd_run(args) -> None:
    """Developer agent in an isolated worktree: context → patch → tests → human approval."""
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    task = args.task

    _state("RETRIEVING_CONTEXT")
    pkg = build_context(cfg, db, task, role="developer", budget=args.budget, explicit_files=args.files)
    _print_context_report(pkg)
    if args.dry_run or cfg.provider.name == "none":
        err.print("[yellow]dry run — developer prompt follows; nothing executed.[/yellow]")
        _write_output(pkg.markdown, args.out, args.clip, "Developer prompt")
        return

    _state("CREATING_WORKSPACE")
    try:
        ws = create_workspace(cfg, task, cfg.run.symlink)
    except WorkspaceError as e:
        err.print(f"[red]{e}[/red]")
        sys.exit(1)
    err.print(f"[green]✓[/green] worktree {escape(str(ws.path.relative_to(cfg.root)))} on branch [bold]{ws.branch}[/bold]")

    frameworks = json.loads(db.get_meta("frameworks", "[]") or "[]")
    test_cmd = None if args.no_tests else (args.test or cfg.run.test_command or detect_test_command(cfg.root, frameworks))

    _state("EXECUTING")
    try:
        _run_loop(cfg, db, args, task, pkg, ws, test_cmd)
    except (ProviderError, WorkspaceError, KeyboardInterrupt) as e:
        ws.remove()
        err.print(f"[dim]worktree removed ({type(e).__name__})[/dim]")
        sys.exit(1)


def _run_loop(cfg: Config, db: Database, args, task: str, pkg, ws, test_cmd: str | None) -> None:
    result = _complete_or_exit(cfg, db, "developer", task, pkg.markdown, _pick_model(cfg, args))
    changes = parse_agent_output(result.text)
    transcript = [result.text]
    outcome = None
    attempt = 0
    while True:
        if changes.empty:
            err.print("[yellow]Agent returned no code changes.[/yellow]")
            if "Needs context" in result.text:
                err.print("It asked for more context — re-run with [bold]-f <file>[/bold] for the files it listed.")
            console.print(changes.summary or result.text, markup=False)
            ws.remove()
            return
        applied = apply_changes(ws, changes)
        if applied.ok:
            err.print(f"[green]✓[/green] patch applied: {', '.join(escape(p) for p in applied.applied)}")
        else:
            err.print(f"[red]✗ patch failed for:[/red] {', '.join(escape(p) for p in applied.failed)}")
            err.print(f"[dim]{escape(applied.errors[:600])}[/dim]")
        tests = None
        if applied.ok and test_cmd:
            _state("TESTING")
            err.print(f"[dim]$ {test_cmd}[/dim]")
            with err.status("[bold]Running tests...[/bold]"):
                tests = run_tests(ws, test_cmd, cfg.run.test_timeout)
            if tests.passed:
                err.print(f"[green]✓[/green] tests passed ({tests.seconds:.1f}s)")
            elif tests.returncode in (126, 127):
                err.print(f"[yellow]! test command not runnable here ({escape(tests.tail(3))}) — treating as not run[/yellow]")
                tests = None
                test_cmd = None
            else:
                err.print(f"[red]✗ tests failed[/red] (exit {tests.returncode}, {tests.seconds:.1f}s)")
                err.print(f"[dim]{escape(tests.tail(25))}[/dim]")
        ok = applied.ok and (tests is None or tests.passed)
        if ok or attempt >= (args.retries if args.retries is not None else cfg.run.max_retries):
            outcome = (applied, tests)
            break
        # ---- RETRYING: give the agent the failure plus current file contents ----
        attempt += 1
        _state(f"RETRYING ({attempt})")
        affected = applied.failed or (list(changes.files) + changed_files_from_diff(changes.diff))
        current = []
        for p in dict.fromkeys(affected):
            fp = ws.path / p
            if fp.is_file() and fp.stat().st_size < 60_000:
                current.append(f"\n## {p} (current content)\n\n```\n{fp.read_text(encoding='utf-8', errors='replace')}\n```\n")
        failure = applied.errors if not applied.ok else (tests.tail(60) if tests else "")
        prompt = (f"# Task\n\n{task}\n\n# Your previous output\n\n{transcript[-1][:12000]}\n\n"
                  f"# Failure\n\n```\n{failure}\n```\n" + "".join(current))
        result = _complete_or_exit(cfg, db, "fix", task, prompt, _pick_model(cfg, args))
        changes = parse_agent_output(result.text)
        transcript.append(result.text)
        # reset the worktree to its base before re-applying
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=ws.path, capture_output=True)
        subprocess.run(["git", "clean", "-fdq", "-e", "vendor", "-e", "node_modules", "-e", ".venv"], cwd=ws.path, capture_output=True)

    applied, tests = outcome
    diff = ws.diff()
    if getattr(args, "review", False) and applied.ok and diff.strip():
        _state("REVIEWING")
        changed = changed_files_from_diff(diff)
        rpkg = build_context(cfg, db, task, role="reviewer", budget=max(1500, cfg.budgets.reviewer - tokens.count(diff)),
                             explicit_files=changed, no_full=True)
        rprompt = rpkg.markdown + f"\n# Diff (proposed by Weaver, {len(changed)} files)\n\n```diff\n{diff.rstrip()}\n```\n"
        try:
            with err.status("[bold]Reviewer working...[/bold]"):
                rres = complete(cfg.provider.name, _pick_model(cfg, args) or cfg.provider.model,
                                SYSTEM_PROMPTS["reviewer"], rprompt, max_tokens=3000)
            db.log_usage(task=task[:500], role="reviewer", provider=rres.provider, model=rres.model,
                         input_tokens=rres.input_tokens, output_tokens=rres.output_tokens,
                         cache_read=rres.cache_read, cost_usd=rres.cost_usd)
            console.print(Panel(rres.text.strip()[:6000], title="Reviewer"))
        except ProviderError as e:
            err.print(f"[yellow]review skipped: {e}[/yellow]")
    _state("WAITING_FOR_APPROVAL")
    console.print(Panel(changes.summary[:3000] or "(no summary)", title="Weaver summary"))
    if args.show_diff:
        console.print(diff, markup=False, highlight=False)
    files = changed_files_from_diff(diff)
    status = "[green]tests passed[/green]" if (tests and tests.passed) else ("[yellow]tests not run[/yellow]" if tests is None else "[red]tests FAILED[/red]")
    err.print(f"{len(files)} file(s) changed · {tokens.count(diff):,} diff tokens · {status}")

    can_apply = applied.ok and files
    if not applied.ok:
        err.print("[red]The patch did not apply cleanly — applying to your working tree is disabled. "
                  "Keep the branch to inspect, or discard.[/red]")
    choice = "apply" if args.apply else "keep" if args.keep else "discard" if args.discard else None
    if choice == "apply" and not can_apply:
        choice = "keep" if files else "discard"
    if choice is None:
        if not sys.stdin.isatty():
            choice = "keep" if files else "discard"
        else:
            opts = ("[a]pply to working tree · " if can_apply else "") + "[k]eep branch for inspection · [d]iscard · [s]how diff"
            console.print("\n" + opts)
            while choice is None:
                ans = input("> ").strip().lower()[:1]
                if ans == "s":
                    console.print(diff, markup=False, highlight=False)
                elif ans == "a" and can_apply:
                    choice = "apply"
                elif ans in ("k", "d"):
                    choice = {"k": "keep", "d": "discard"}[ans]
    if choice == "apply":
        ok, msg = apply_to_working_tree(ws)
        if ok:
            err.print(f"[green]✓[/green] {msg}. Review with `git diff`, then commit.")
            ws.remove()
            _state("COMPLETED")
        else:
            err.print(f"[red]Could not apply cleanly:[/red] {escape(msg)}")
            err.print(f"Branch kept: [bold]{ws.branch}[/bold]  (`git merge {ws.branch}` or inspect {escape(str(ws.path))})")
    elif choice == "keep":
        sha = ws.commit(f"nokshi: {task[:60]}")
        err.print(f"[green]✓[/green] committed {sha} on [bold]{ws.branch}[/bold] · worktree {escape(str(ws.path))}")
        err.print(f"  merge:   git merge {ws.branch}\n  discard: git worktree remove --force {escape(str(ws.path))} && git branch -D {ws.branch}")
    else:
        ws.remove()
        err.print("[dim]discarded[/dim]")


def cmd_task(args) -> None:
    """Flagship pipeline: context → Planner → confirm → Weaver in worktree → tests → Reviewer → approve."""
    cfg, db = _open(args)
    _ensure_fresh(cfg, db)
    task = args.task
    if not args.skip_plan:
        _state("PLANNING")
        pkg = build_context(cfg, db, task, role="planner", budget=None, explicit_files=args.files)
        _print_context_report(pkg)
        if args.dry_run or cfg.provider.name == "none":
            _write_output(pkg.markdown, args.out, args.clip, "Planner prompt")
            return
        try:
            with err.status("[bold]Planner working...[/bold]"):
                plan = complete(cfg.provider.name, _pick_model(cfg, args) or cfg.provider.model,
                                SYSTEM_PROMPTS["planner"], pkg.markdown, max_tokens=3000)
        except ProviderError as e:
            err.print(f"[red]Provider error:[/red] {e}")
            sys.exit(1)
        db.log_usage(task=task[:500], role="planner", provider=plan.provider, model=plan.model,
                     input_tokens=plan.input_tokens, output_tokens=plan.output_tokens,
                     cache_read=plan.cache_read, cost_usd=plan.cost_usd)
        console.print(Panel(plan.text.strip()[:8000], title="Plan"))
        if "Needs context" in plan.text and not args.yes:
            err.print("[yellow]The planner asked for more context. Add files with -f and re-run.[/yellow]")
            return
        if not args.yes:
            if not sys.stdin.isatty():
                err.print("[yellow]Non-interactive: pass --yes to proceed from plan to implementation.[/yellow]")
                return
            ans = input("Proceed with implementation? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                err.print("[dim]stopped after planning[/dim]")
                return
        # hand the plan to the Weaver as part of the task statement
        task = f"{task}\n\nFollow this plan:\n{plan.text.strip()[:6000]}"
    args.task = task
    args.review = not args.no_review
    cmd_run(args)


def cmd_workspaces(args) -> None:
    cfg, _db = _open(args)
    spaces = list_workspaces(cfg)
    if not spaces:
        console.print("No nokshi worktrees.")
        return
    for path, branch, sha in spaces:
        console.print(f"[bold]{branch}[/bold]  {sha}  {escape(path)}")
        if args.prune:
            remove_workspace(cfg, path, branch)
            console.print("  [dim]removed[/dim]")


def cmd_remember(args) -> None:
    cfg, db = _open(args)
    memory_mod.remember(db, args.kind, args.text)
    console.print(f"[green]✓[/green] remembered ({args.kind}): {escape(args.text)}")


def cmd_memory(args) -> None:
    cfg, db = _open(args)
    if args.forget is not None:
        n = memory_mod.forget(db, args.forget)
        console.print(f"[green]✓[/green] removed {n} memory" if n else f"[yellow]no memory #{args.forget}[/yellow]")
        return
    mems = memory_mod.memories(db)
    if memory_mod.rules_text(cfg):
        console.print(Panel(memory_mod.rules_text(cfg), title=f"rules — {cfg.rules_path.relative_to(cfg.root)}"))
    if not mems:
        console.print("No memories yet. Add one: [bold]nokshi remember \"...\"[/bold]")
        return
    table = Table(title="Project memory")
    table.add_column("#", justify="right"); table.add_column("kind"); table.add_column("when"); table.add_column("body")
    for m in mems:
        table.add_row(str(m["id"]), m["kind"], time.strftime("%Y-%m-%d", time.localtime(m["created_at"])), escape(m["body"]))
    console.print(table)


def cmd_cost(args) -> None:
    cfg, db = _open(args)
    since = time.time() - args.days * 86400
    s = db.usage_summary(since)
    t, c = s["totals"], s["context"]
    console.print(Panel.fit(
        f"[bold]Last {args.days} days[/bold]\n"
        f"Model calls: {t['n']}   input: {t['i']:,}   output: {t['o']:,}   est. cost: ${t['cost']:.4f}\n"
        f"Context packages: {c['n']}   avg files selected: {c['sel']:.1f} of {c['cons']:.1f} considered\n"
        f"Context tokens sent: {c['t']:,}   naive baseline (whole repo each time): {c['b']:,}\n"
        f"Estimated context savings: [bold]{(c['b'] - c['t']):,}[/bold] tokens "
        f"({(100 * (1 - c['t'] / c['b'])) if c['b'] else 0:.0f}%)",
        title="Nokshi cost & savings"))
    if s["by_role"]:
        table = Table(title="By agent / model")
        for col in ("role", "model", "calls", "input", "output", "cost"):
            table.add_column(col, justify="right" if col not in ("role", "model") else "left")
        for r in s["by_role"]:
            table.add_row(r["role"], r["model"], str(r["n"]), f"{r['i']:,}", f"{r['o']:,}", f"${r['cost']:.4f}")
        console.print(table)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nokshi", description="Nokshi — Intelligent Context for AI Engineering  ·  by CoreBari")
    p.add_argument("--version", action="version", version=f"nokshi {__version__} — Intelligent Context for AI Engineering (by CoreBari)")
    p.add_argument("--root", help="repository root (default: auto-detect from cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .nokshi/ config and rules file"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("analyze", aliases=["index"], help="index the repository (incremental)")
    s.add_argument("--full", action="store_true", help="drop the index and rebuild from scratch"); s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("status", help="show index summary"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("summarize", help="write AI summaries for large files with the cheap model (cached by hash)")
    s.add_argument("--limit", type=int, help="summarize at most N files")
    s.add_argument("--dry-run", action="store_true", help="show what would be summarized and the estimated cost")
    s.set_defaults(fn=cmd_summarize)

    s = sub.add_parser("search", help="find symbols or files by name")
    s.add_argument("query"); s.add_argument("--limit", type=int, default=25); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("graph", help="show dependencies of a file or symbol")
    s.add_argument("target"); s.add_argument("--limit", type=int, default=25)
    s.add_argument("--symbols", action="store_true", help="also list the file's symbols"); s.set_defaults(fn=cmd_graph)

    def io_args(sp, budget_help):
        sp.add_argument("--budget", type=int, help=budget_help)
        sp.add_argument("-f", "--files", nargs="*", help="files to force into context")
        sp.add_argument("-o", "--out", help="write result to this file")
        sp.add_argument("-c", "--clip", action="store_true", help="copy result to clipboard")

    s = sub.add_parser("context", aliases=["ctx"], help="build a token-budgeted context package for a task (no model call)")
    s.add_argument("task"); io_args(s, "token budget (default: [budget].context)")
    s.add_argument("--role", choices=["context", "planner", "developer", "reviewer"], default="context")
    s.add_argument("--no-tests", action="store_true", help="exclude test files")
    s.add_argument("--signatures-only", action="store_true", help="never include full source")
    s.add_argument("--json", action="store_true", help="emit selection as JSON instead of Markdown")
    s.add_argument("--explain", action="store_true", help="show ranking signals per file")
    s.add_argument("--explain-limit", type=int, default=20); s.set_defaults(fn=cmd_context)

    def agent_args(sp):
        sp.add_argument("--dry-run", action="store_true", help="print the prompt instead of calling the model")
        sp.add_argument("--model", help="override configured model")
        sp.add_argument("--cheap", action="store_true", help="use [provider].cheap_model for this call")

    s = sub.add_parser("plan", help="planner agent: context + execution plan")
    s.add_argument("task"); io_args(s, "token budget (default: [budget].planner)"); agent_args(s); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("ask", help="answer a question about the codebase")
    s.add_argument("question"); io_args(s, "token budget (default: [budget].context)"); agent_args(s); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("review", help="reviewer agent over git diff (working tree, --staged or --base)")
    s.add_argument("--task", help="what the change is supposed to do")
    s.add_argument("--staged", action="store_true"); s.add_argument("--base", help="branch/commit to diff against")
    io_args(s, "total token budget incl. diff (default: [budget].reviewer)"); agent_args(s); s.set_defaults(fn=cmd_review)

    s = sub.add_parser("run", help="implement directly (no plan step): Weaver in an isolated worktree → tests → approve")
    s.add_argument("task"); io_args(s, "context budget (default: [budget].developer)"); agent_args(s)
    s.add_argument("--test", help="test command to run in the worktree (auto-detected if omitted)")
    s.add_argument("--no-tests", action="store_true")
    s.add_argument("--retries", type=int, help="automatic fix attempts (default: [run].max_retries)")
    s.add_argument("--show-diff", action="store_true", help="print the resulting diff")
    s.add_argument("--review", action="store_true", help="run the Reviewer on the result before asking for approval")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply to working tree without asking")
    g.add_argument("--keep", action="store_true", help="commit on the forge/ branch and keep the worktree")
    g.add_argument("--discard", action="store_true", help="throw the result away (useful for evaluation)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("task", help="full pipeline (start here): plan → confirm → implement in worktree → tests → review → approve")
    s.add_argument("task"); io_args(s, "context budget for the Weaver (default: [budget].developer)"); agent_args(s)
    s.add_argument("-y", "--yes", action="store_true", help="don't ask before implementing")
    s.add_argument("--skip-plan", action="store_true", help="go straight to implementation")
    s.add_argument("--no-review", action="store_true", help="skip the Reviewer pass")
    s.add_argument("--test", help="test command to run in the worktree (auto-detected if omitted)")
    s.add_argument("--no-tests", action="store_true")
    s.add_argument("--retries", type=int, help="automatic fix attempts (default: [run].max_retries)")
    s.add_argument("--show-diff", action="store_true", help="print the resulting diff")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true"); g.add_argument("--keep", action="store_true"); g.add_argument("--discard", action="store_true")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("workspaces", aliases=["ws"], help="list nokshi worktrees (--prune removes them all)")
    s.add_argument("--prune", action="store_true"); s.set_defaults(fn=cmd_workspaces)

    s = sub.add_parser("remember", help="store a project decision / note / bug / pattern")
    s.add_argument("text"); s.add_argument("--kind", choices=["decision", "note", "bug", "pattern"], default="note")
    s.set_defaults(fn=cmd_remember)
    s = sub.add_parser("memory", help="show rules and memories")
    s.add_argument("--forget", type=int, metavar="ID", help="delete memory by id"); s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("cost", help="token usage, cost and savings report")
    s.add_argument("--days", type=int, default=30); s.set_defaults(fn=cmd_cost)
    return p


QUICKSTART = """[bold]Nokshi[/bold] — Intelligent Context for AI Engineering

  nokshi init && nokshi analyze        index this repository (once; incremental afterwards)
  nokshi context "task" -c             build a token-budgeted context package → clipboard
  nokshi task "task"                   plan → implement in a worktree → tests → review → approve
  nokshi review                        review your current diff before committing
  nokshi cost                          tokens, dollars and savings

  nokshi <command> -h                  help for any command · nokshi --help for the full list
"""


def main(argv: list[str] | None = None) -> None:
    if not (argv if argv is not None else sys.argv[1:]):
        console.print(QUICKSTART)
        cfg = load_config()
        if not cfg.db_path.exists():
            console.print(f"[dim]No index in {escape(str(cfg.root))} yet — start with [bold]nokshi init && nokshi analyze[/bold][/dim]")
        return
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        err.print("\n[dim]interrupted[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    main()
