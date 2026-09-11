"""Context package builder (roadmap §5–§7).

Progressive loading: the highest-ranked files are included as full source,
the next tier as symbol signatures ("Level 2 — Symbols"), and the tail as a
one-line map ("Level 1 — Metadata"). Everything is fitted into an explicit
token budget *before* any model sees it, with a reserve for the prompt itself.
The result is Markdown you can paste into any assistant or pipe to a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from nokshi.context.ranking import Candidate, rank_files, tokenize
from nokshi.core import tokens
from nokshi.core.config import Config
from nokshi.core.db import Database
from nokshi.core.parsers import split_identifier

FULL_TIER_MAX_FILES = 12          # never dump more than this many whole files
FULL_SCORE_RATIO = 0.35           # a file must score ≥ 35% of the best to earn full source
FULL_TIER_SHARE = 0.70            # full/region tier may use at most this share of the content budget
LARGE_FILE_TOKENS = 2500          # above this, prefer relevant regions over the whole file
SIGNATURE_TIER_MAX = 30
SIGNATURE_MAX_LINES = 60
MAP_TIER_MAX = 40


@dataclass
class Selection:
    candidate: Candidate
    representation: str            # full | signatures | map
    tokens: int


@dataclass
class ContextPackage:
    task: str
    role: str
    budget: int
    markdown: str
    tokens: int
    considered: int
    selections: list[Selection] = field(default_factory=list)
    baseline_tokens: int = 0       # tokens if the whole repo were sent
    considered_tokens: int = 0     # tokens of all candidate files
    truncated: bool = False
    reserve: int = 0

    @property
    def files_full(self) -> list[str]:
        return [s.candidate.path for s in self.selections if s.representation in ("full", "regions")]

    @property
    def savings_pct(self) -> float:
        return 100.0 * (1 - self.tokens / self.baseline_tokens) if self.baseline_tokens else 0.0

    def to_json(self) -> str:
        return json.dumps({
            "task": self.task, "role": self.role, "budget": self.budget, "tokens": self.tokens,
            "considered": self.considered, "baseline_tokens": self.baseline_tokens,
            "savings_pct": round(self.savings_pct, 1),
            "files": [{"path": s.candidate.path, "representation": s.representation, "tokens": s.tokens,
                       "score": round(s.candidate.score, 3), "signals": {k: round(v, 3) for k, v in s.candidate.signals.items()}}
                      for s in self.selections],
        }, indent=2)


def _regions(cfg: Config, db: Database, cand: Candidate, query: set[str], text: str) -> str | None:
    """Level 3: only the symbol bodies relevant to the task, plus the enclosing declaration lines."""
    row = db.file_by_path(cand.path)
    if not row:
        return None
    syms = db.symbols_for(row["id"])
    lines = text.splitlines()
    picked = []
    for s in syms:
        if s["kind"] in ("property", "const"):
            continue
        toks = set(split_identifier(s["name"]))
        if s["name"] in cand.matched_symbols or (toks & query):
            picked.append(s)
    if not picked:
        return None
    picked.sort(key=lambda s: s["line_start"])
    # merge overlapping ranges, cap each region
    ranges: list[tuple[int, int, str]] = []
    for s in picked:
        a, b = s["line_start"], min(s["line_end"], s["line_start"] + 120)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], b), ranges[-1][2])
        else:
            ranges.append((a, b, s["name"]))
    parents = {s["parent"] for s in picked if s["parent"]}
    header = [f"{s['signature']}    # L{s['line_start']}" for s in syms if s["name"] in parents and s["kind"] != "method"]
    out = []
    if header:
        out.append("\n".join(header) + "\n    # ...")
    for a, b, _name in ranges[:8]:
        out.append(f"# --- L{a}-{b} ---\n" + "\n".join(lines[a - 1:b]))
    return "\n\n".join(out)


def _summary_line(db: Database, cand: Candidate, ai_only: bool = False) -> str:
    row = db.file_by_path(cand.path)
    if not row:
        return ""
    if row["summary"]:
        return row["summary"]
    if ai_only:
        return ""
    from nokshi.context.summaries import structural_summary
    return structural_summary(db, row)


def _signatures(db: Database, cand: Candidate) -> str:
    row = db.file_by_path(cand.path)
    if not row:
        return ""
    lines = []
    syms = db.symbols_for(row["id"])
    for s in syms[:SIGNATURE_MAX_LINES]:
        indent = "  " if s["parent"] else ""
        sig = s["signature"] or s["name"]
        if s["kind"] in ("property", "const") and len(sig) > 80:
            sig = sig[:77] + "..."
        lines.append(f"{indent}{sig}    # L{s['line_start']}-{s['line_end']}" if s["line_end"] != s["line_start"]
                     else f"{indent}{sig}    # L{s['line_start']}")
    if len(syms) > SIGNATURE_MAX_LINES:
        lines.append(f"# ... {len(syms) - SIGNATURE_MAX_LINES} more symbols")
    return "\n".join(lines)


def _fence(language: str) -> str:
    return {"python": "python", "php": "php", "javascript": "javascript", "typescript": "typescript",
            "vue": "vue", "csharp": "csharp", "json": "json", "yaml": "yaml", "sql": "sql",
            "markdown": "markdown", "blade": "blade", "razor": "razor", "html": "html", "css": "css",
            "shell": "bash", "toml": "toml", "xml": "xml"}.get(language, "")


_SECRET_RX = re.compile(
    r"""(?ix)
    ((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|refresh[_-]?token|
        password|passwd|pwd|private[_-]?key|connection[_-]?string|dsn|secret|token|bearer)\b
     [\w\-\.\[\]"']{0,32}?\s*[:=]\s*)
    (["'])(?P<val>[^"'\n]{8,})\2
    """)
_KEY_LITERAL_RX = re.compile(r"\b(sk-[A-Za-z0-9_\-]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|"
                             r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,})\b")


_CONN_RX = re.compile(r"(?i)((?:password|pwd|secret|token|api[_-]?key)=)([^;&\s\"']{3,})")


def redact(text: str) -> str:
    """Mask credential-looking values before they leave the machine. Deterministic and conservative:
    only quoted values after secret-ish keys, plus well-known key formats."""
    text = _SECRET_RX.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***{m.group(2)}", text)
    text = _CONN_RX.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
    return _KEY_LITERAL_RX.sub("***REDACTED***", text)


def _read(cfg: Config, path: str) -> str:
    try:
        return redact((cfg.root / path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _header(cfg: Config, db: Database, task: str, role: str) -> str:
    frameworks = json.loads(db.get_meta("frameworks", "[]") or "[]")
    langs = db.conn.execute(
        "SELECT language, COUNT(*) n FROM files GROUP BY language ORDER BY n DESC LIMIT 6").fetchall()
    modules = db.conn.execute(
        "SELECT module, COUNT(*) n FROM files WHERE is_test = 0 GROUP BY module ORDER BY n DESC LIMIT 14").fetchall()
    parts = [f"# Task\n\n{task.strip()}\n", "# Repository\n"]
    parts.append(f"- Root: `{cfg.root.name}`")
    if frameworks:
        parts.append(f"- Frameworks: {', '.join(frameworks)}")
    parts.append("- Languages: " + ", ".join(f"{r['language']} ({r['n']})" for r in langs))
    parts.append("- Main modules: " + ", ".join(f"`{r['module']}`" for r in modules))
    from nokshi.memory import memory_block
    block = memory_block(cfg, db)
    if block:
        parts.append("\n# Project rules and memory\n")
        parts.append(block)
    role_note = {
        "planner": "Produce an execution plan: steps, files to touch, risks, and what to verify. Do not write code yet.",
        "developer": "Implement the task in the files shown. Follow the project rules. Return changes as unified diffs plus a short structured summary (files_changed, decisions, tests).",
        "reviewer": "Review the change against the task and rules. Report critical issues, warnings, suggestions and a score /10.",
        "context": "Use only the context below; ask for a specific file if something essential is missing.",
    }.get(role, "")
    if role_note:
        parts.append(f"\n# Instructions\n\n{role_note}")
    return "\n".join(parts) + "\n"


def build_context(cfg: Config, db: Database, task: str, role: str = "context", budget: int | None = None,
                  explicit_files: list[str] | None = None, include_tests: bool = True,
                  no_full: bool = False) -> ContextPackage:
    budget = budget or getattr(cfg.budgets, role, cfg.budgets.context)
    ranked = rank_files(cfg, db, task, explicit_files, include_tests)
    header = _header(cfg, db, task, role)
    used = tokens.count(header) + cfg.budgets.reserve
    selections: list[Selection] = []
    body_parts: list[str] = []
    pending_sig: list[Candidate] = []
    pending_map: list[Candidate] = []
    best = ranked[0].score if ranked else 0.0

    # Tier 1: full source (small files) or relevant regions (large files) for the strongest candidates
    query = set(tokenize(task))
    content_budget = budget - used
    tier1_limit = used + int(content_budget * FULL_TIER_SHARE)
    full_count = 0
    for cand in ranked:
        if full_count >= FULL_TIER_MAX_FILES or no_full or cand.score < best * FULL_SCORE_RATIO:
            pending_sig.append(cand)
            continue
        text = _read(cfg, cand.path)
        if not text.strip():
            continue
        placed = False
        if cand.tokens <= LARGE_FILE_TOKENS:
            block = f"\n## {cand.path}  (full source)\n\n```{_fence(cand.language)}\n{text.rstrip()}\n```\n"
            cost = tokens.count(block)
            if used + cost <= tier1_limit:
                body_parts.append(block); selections.append(Selection(cand, "full", cost))
                used += cost; full_count += 1; placed = True
        if not placed:
            regions = _regions(cfg, db, cand, query, text)
            if regions:
                block = f"\n## {cand.path}  (relevant regions of {cand.tokens} tokens)\n\n```{_fence(cand.language)}\n{regions}\n```\n"
                cost = tokens.count(block)
                if used + cost <= tier1_limit:
                    body_parts.append(block); selections.append(Selection(cand, "regions", cost))
                    used += cost; full_count += 1; placed = True
        if not placed:
            pending_sig.insert(0, cand)  # keep priority, fall back to signatures

    # Tier 2: summary + signatures
    for cand in pending_sig[:SIGNATURE_TIER_MAX]:
        sigs = _signatures(db, cand)
        if not sigs:
            pending_map.append(cand)
            continue
        summary = _summary_line(db, cand)
        block = f"\n## {cand.path}  (signatures only)\n\n" + (f"{summary}\n\n" if summary else "") + f"```\n{sigs}\n```\n"
        cost = tokens.count(block)
        if used + cost <= budget:
            body_parts.append(block)
            selections.append(Selection(cand, "signatures", cost))
            used += cost
        else:
            pending_map.append(cand)
    pending_map.extend(pending_sig[SIGNATURE_TIER_MAX:])

    # Tier 3: file map
    truncated = False
    if pending_map:
        lines = []
        for cand in pending_map[:MAP_TIER_MAX]:
            summary = _summary_line(db, cand, ai_only=True)
            hint = f" — {summary[:160]}" if summary else (f" — {', '.join(cand.matched_symbols)}" if cand.matched_symbols else "")
            line = f"- `{cand.path}` ({cand.tokens} tok){hint}"
            cost = tokens.count(line) + 1
            if used + cost > budget:
                truncated = True
                break
            lines.append(line)
            selections.append(Selection(cand, "map", cost))
            used += cost
        if lines:
            body_parts.append("\n## Other possibly relevant files (not loaded — request if needed)\n\n" + "\n".join(lines) + "\n")
        if len(pending_map) > MAP_TIER_MAX:
            truncated = True

    markdown = header + "".join(body_parts)
    if truncated:
        markdown += f"\n_Context truncated to fit a {budget}-token budget._\n"
    total_tokens = tokens.count(markdown)
    baseline = db.conn.execute("SELECT COALESCE(SUM(tokens),0) t FROM files").fetchone()["t"]
    considered_tokens = sum(c.tokens for c in ranked)
    pkg = ContextPackage(task=task, role=role, budget=budget, markdown=markdown, tokens=total_tokens,
                         considered=len(ranked), selections=selections, baseline_tokens=baseline,
                         considered_tokens=considered_tokens, truncated=truncated,
                         reserve=cfg.budgets.reserve)
    db.log_context_run(task=task[:500], role=role, budget=budget, considered=len(ranked),
                       selected=len([s for s in selections if s.representation != "map"]),
                       tokens=total_tokens, baseline_tokens=baseline)
    return pkg


def changed_files_from_diff(diff: str) -> list[str]:
    paths = []
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", diff, re.M):
        paths.append(m.group(2))
    return paths
