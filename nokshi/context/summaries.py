"""File summaries (roadmap §7 "AI summaries", §16.7, §16.11 model routing).

Two layers:
- structural_summary(): free, deterministic, always available — built from the symbol index.
- summarize_files(): 2–3 sentence AI summaries via the configured *cheap* model, cached by file
  hash so a file is only re-summarized when its content changes.

Summaries are used (a) as terms in ranking, (b) as the header of a file's signatures-tier block,
(c) instead of a bare path in the file-map tier — all of which let large files ride in the
package at a fraction of their token cost.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from nokshi.agents.providers import SYSTEM_PROMPTS, Completion, ProviderError, complete
from nokshi.core.config import Config
from nokshi.core.db import Database

EXCERPT_LINES = 80


def structural_summary(db: Database, file_row) -> str:
    syms = db.symbols_for(file_row["id"])
    types = [s["name"] for s in syms if s["kind"] in ("class", "interface", "trait", "enum", "record", "struct")]
    funcs = [s for s in syms if s["kind"] in ("method", "function", "constructor")]
    imports = [i["raw"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for i in db.imports_for(file_row["id"])]
    parts = []
    if types:
        parts.append("Defines " + ", ".join(types[:4]) + (f" (+{len(types) - 4} more)" if len(types) > 4 else ""))
    if funcs:
        pub = [f["name"] for f in funcs if "public" in f["signature"] or f["kind"] == "function"
               or not any(k in f["signature"] for k in ("private", "protected"))]
        names = pub[:6] or [f["name"] for f in funcs[:6]]
        parts.append(f"{len(funcs)} operations incl. " + ", ".join(names))
    if imports:
        parts.append("uses " + ", ".join(dict.fromkeys(imports[:6])))
    return "; ".join(parts) + "." if parts else ""


@dataclass
class SummarizeStats:
    candidates: int = 0
    written: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


def _prompt_for(cfg: Config, db: Database, row) -> str:
    sigs = "\n".join(s["signature"] for s in db.symbols_for(row["id"]))[:3000]
    try:
        text = (cfg.root / row["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    from nokshi.context.builder import redact  # local import: context imports summaries
    excerpt = redact("\n".join(text.splitlines()[:EXCERPT_LINES]))[:5000]
    return f"Path: {row['path']}\nLanguage: {row['language']}\n\nSignatures:\n{sigs}\n\nExcerpt:\n{excerpt}\n"


def summarize_files(cfg: Config, db: Database, limit: int | None = None, dry_run: bool = False,
                    progress: Callable[[str], None] | None = None) -> SummarizeStats:
    stats = SummarizeStats()
    rows = db.files_needing_summary(cfg.summaries.min_tokens)
    if limit:
        rows = rows[:limit]
    stats.candidates = len(rows)
    if not rows or dry_run:
        return stats
    provider, model = cfg.provider.name, cfg.provider.cheap_model or cfg.provider.model
    if provider == "none":
        raise ProviderError("provider is 'none' — set [provider].name to summarize with a model")

    def work(row, prompt: str) -> tuple[object, Completion | None, str | None]:
        try:
            return row, complete(provider, model, SYSTEM_PROMPTS["summarizer"], prompt, max_tokens=200), None
        except ProviderError as e:
            return row, None, str(e)

    # SQLite connections are thread-bound: build prompts here, only HTTP happens in workers.
    prompts = [(r, _prompt_for(cfg, db, r)) for r in rows]
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.summaries.concurrency)) as pool:
        futures = [pool.submit(work, r, pr) for r, pr in prompts]
        for fut in as_completed(futures):
            row, result, error = fut.result()
            done += 1
            if error or result is None:
                stats.failed += 1
                if len(stats.errors) < 5:
                    stats.errors.append(f"{row['path']}: {error}")
                if stats.failed >= 5 and stats.written == 0:
                    for f in futures:
                        f.cancel()
                    break
                continue
            summary = " ".join(result.text.split())[:600]
            db.set_summary(row["id"], summary, row["sha256"])
            db.log_usage(task="summarize", role="summarizer", provider=result.provider, model=result.model,
                         input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                         cache_read=result.cache_read, cost_usd=result.cost_usd)
            stats.written += 1
            stats.input_tokens += result.input_tokens
            stats.output_tokens += result.output_tokens
            stats.cost_usd += result.cost_usd
            if progress and done % 10 == 0:
                progress(f"{done}/{len(rows)}")
    return stats
