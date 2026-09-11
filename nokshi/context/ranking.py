"""Relevance ranking (roadmap §6 ranking signals).

Each file gets a score that is a weighted sum of:
  lexical            BM25 over path, symbol names and import names (camelCase-split)
  explicit_reference task names a file path or a symbol exactly
  dependency         1–2 hop expansion from the strongest lexical/explicit seeds
  git_recency        recently changed files are more likely to be relevant
  module_match       same module as the top seeds
  test_relevance     tests attached to selected source files
  memory             project rules / memories mention the file
All signals are deterministic; no model call happens here.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from nokshi.core.config import Config
from nokshi.core.db import Database
from nokshi.core.graph import Graph
from nokshi.core.parsers import STRUCTURAL_LANGUAGES as STRUCTURAL
from nokshi.core.parsers import split_identifier

STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "in", "on", "and", "or", "with", "add", "create", "make", "new",
    "update", "fix", "change", "implement", "support", "should", "when", "that", "this", "is", "are", "be",
    "it", "into", "from", "by", "as", "at", "so", "we", "i", "use", "using", "via", "our", "my",
    "file", "files", "code", "please", "need", "want", "can", "feature", "bug", "issue", "error",
    "where", "does", "doesn", "do", "not", "no", "if", "then", "than", "but", "also", "only", "all",
    "any", "some", "how", "what", "which", "who", "why", "will", "would", "could", "have", "has", "had",
    "been", "was", "were", "get", "set", "run", "runs", "work", "works", "working", "correctly", "properly",
    "currently", "still", "after", "before", "instead", "like", "just", "very", "more", "less", "same",
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text):
        parts = split_identifier(w)
        out.extend(p for p in parts if len(p) > 1 and p not in STOPWORDS)
        if len(parts) > 1 and w.lower() not in STOPWORDS:
            out.append(w.lower())
    return out


@dataclass
class Candidate:
    file_id: int
    path: str
    language: str
    module: str
    tokens: int
    is_test: bool
    is_config: bool
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    matched_symbols: list[str] = field(default_factory=list)

    def add(self, name: str, value: float) -> None:
        if value:
            self.signals[name] = self.signals.get(name, 0.0) + value
            self.score += value


class BM25:
    def __init__(self, docs: dict[int, list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {fid: Counter(toks) for fid, toks in docs.items()}
        self.len = {fid: len(toks) for fid, toks in docs.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 1.0
        df: Counter[str] = Counter()
        for tf in self.tf.values():
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, fid: int, query: list[str]) -> float:
        tf, dl = self.tf[fid], self.len[fid]
        s = 0.0
        for q in query:
            f = tf.get(q)
            if not f:
                continue
            idf = self.idf.get(q, 0.0)
            s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
        return s


def git_recency(root, limit: int = 300) -> dict[str, float]:
    """path -> score in (0, 1], most recently touched files highest."""
    try:
        out = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-n{limit}"],
            cwd=root, capture_output=True, text=True, timeout=20,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    seen: dict[str, float] = {}
    rank = 0
    for line in out.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen[line] = 1.0 / (1 + rank / 25)
            rank += 1
    return seen


def rank_files(cfg: Config, db: Database, task: str, explicit_files: list[str] | None = None,
               include_tests: bool = True) -> list[Candidate]:
    w = cfg.weights
    rows = db.all_files()
    if not rows:
        return []
    cands = {r["id"]: Candidate(r["id"], r["path"], r["language"], r["module"], r["tokens"],
                                bool(r["is_test"]), bool(r["is_config"])) for r in rows}

    # --- documents for lexical search ------------------------------------
    sym_names: dict[int, list[str]] = defaultdict(list)
    for s in db.all_symbols():
        sym_names[s["file_id"]].append(s["name"])
    imports_by_file: dict[int, list[str]] = defaultdict(list)
    for i in db.all_imports():
        imports_by_file[i["file_id"]].append(i["raw"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1])
    terms_by_file: dict[int, list[str]] = defaultdict(list)
    for t in db.all_terms():
        terms_by_file[t["file_id"]].extend([t["term"]] * (1 + min(int(math.log2(t["count"])), 3)))
    docs: dict[int, list[str]] = {}
    for r in rows:
        toks = tokenize(r["path"]) * 3                      # path words are strong evidence
        toks += tokenize(" ".join(sym_names.get(r["id"], []))) * 2
        toks += tokenize(" ".join(imports_by_file.get(r["id"], [])))
        if r["namespace"]:
            toks += tokenize(r["namespace"])
        toks += [t for t in terms_by_file.get(r["id"], []) if t not in STOPWORDS]
        if r["summary"]:
            toks += tokenize(r["summary"]) * 2                # AI summaries are high-signal
        docs[r["id"]] = toks
    bm25 = BM25(docs, b=0.35)
    query = tokenize(task)
    raw_words = set(_WORD.findall(task))

    # --- lexical --------------------------------------------------------
    lexical = {fid: bm25.score(fid, query) for fid in cands}
    max_lex = max(lexical.values(), default=0.0) or 1.0
    for fid, c in cands.items():
        c.add("lexical", w.lexical * lexical[fid] / max_lex)
        hits = [n for n in sym_names.get(fid, []) if n in raw_words or n.lower() in query]
        c.matched_symbols = sorted(set(hits))[:6]

    # --- explicit references (paths and exact symbol names) --------------
    lowered = task.lower()
    def _looks_like_identifier(word: str) -> bool:
        # PaymentService, user_repo, IRefundService, ORDER_STATUS — but not "background" or "config"
        return (any(ch.isupper() for ch in word[1:]) or "_" in word or any(ch.isdigit() for ch in word)) and len(word) >= 5
    definition_kinds = ("class", "interface", "trait", "enum", "record", "struct", "type", "function")
    defs: dict[int, list[str]] = defaultdict(list)
    for s in db.all_symbols():
        if s["kind"] in definition_kinds:
            defs[s["file_id"]].append(s["name"])
    for fid, c in cands.items():
        name = PurePosixPath(c.path).name
        stem = PurePosixPath(name).stem.split(".")[0]
        if c.path.lower() in lowered or (len(name) > 6 and name.lower() in lowered):
            c.add("explicit_reference", w.explicit_reference)
        elif stem in raw_words and _looks_like_identifier(stem):
            c.add("explicit_reference", w.explicit_reference * 0.8)
        elif any(n in raw_words and _looks_like_identifier(n) for n in defs.get(fid, [])):
            c.add("explicit_reference", w.explicit_reference * 0.6)
    for p in explicit_files or []:
        for c in cands.values():
            if c.path == p or c.path.endswith("/" + p) or PurePosixPath(c.path).name == p:
                c.add("explicit_reference", w.explicit_reference * 1.5)

    # --- memory ---------------------------------------------------------
    memory_text = " ".join(m["body"] for m in db.memories())
    if cfg.rules_path.is_file():
        memory_text += " " + cfg.rules_path.read_text(encoding="utf-8", errors="replace")
    if memory_text.strip():
        mem_words = set(_WORD.findall(memory_text))
        for c in cands.values():
            stem = PurePosixPath(c.path).stem.split(".")[0]
            if c.path in memory_text or (len(stem) >= 6 and stem in mem_words and stem in raw_words):
                c.add("memory", w.memory)

    # --- file-type prior: code first, docs/config second ---------------
    for c in cands.values():
        if c.language in ("markdown", "text", "json", "yaml", "toml", "xml", "ini", "env", "html", "css"):
            c.score *= 0.55
            for k in c.signals:
                c.signals[k] *= 0.55
        elif c.language not in STRUCTURAL:
            c.score *= 0.8
            for k in c.signals:
                c.signals[k] *= 0.8

    # --- dependency expansion from seeds ---------------------------------
    seeds = sorted(cands.values(), key=lambda c: c.score, reverse=True)
    seeds = [c for c in seeds[:8] if c.score > 0.25 * seeds[0].score] if seeds and seeds[0].score > 0 else []
    graph = Graph(db)
    best_seed = seeds[0].score if seeds else 0.0
    dep_cap = w.dependency * best_seed  # a file can never out-rank the best seed on dependency alone

    def hub_penalty(fid: int) -> float:
        # Files that everything touches (Application, base Controller, helpers) carry little signal.
        deg = len(graph.inc.get(fid, {})) + len(graph.out.get(fid, {}))
        return 1.0 / (1.0 + math.log1p(max(deg - 3, 0)))

    dep_acc: dict[int, float] = defaultdict(float)
    for seed in seeds:
        base = seed.score
        for n1, w1 in graph.neighbors(seed.file_id).items():
            if n1 in cands:
                dep_acc[n1] += w.dependency * base * w1 * 0.5 * hub_penalty(n1)
            for n2, w2 in graph.neighbors(n1).items():
                if n2 in cands and n2 != seed.file_id:
                    dep_acc[n2] += w.dependency * base * w1 * w2 * 0.15 * hub_penalty(n2) * hub_penalty(n1)
    for fid, v in dep_acc.items():
        cands[fid].add("dependency", min(v, dep_cap))

    # --- module match ---------------------------------------------------
    seed_modules = Counter(s.module for s in seeds)
    for c in cands.values():
        if c.module in seed_modules and c.module != "(root)":
            c.add("module_match", w.module_match * seed_modules[c.module] / max(len(seeds), 1))

    # --- git recency ----------------------------------------------------
    recency = git_recency(cfg.root)
    for c in cands.values():
        if c.path in recency and c.score > 0:
            c.add("git_recency", w.git_recency * recency[c.path])

    # --- tests ----------------------------------------------------------
    task_wants_tests = any(k in lowered for k in ("test", "spec", "coverage", "regression"))
    for c in cands.values():
        if not c.is_test:
            continue
        if not include_tests:
            c.score, c.signals = 0.0, {}
            continue
        stem = PurePosixPath(c.path).stem
        subject = re.sub(r"(Test|Tests|Spec|_test|test_|\.test|\.spec)", "", stem)
        generic = subject.lower() in {"utils", "util", "helpers", "helper", "index", "base", "main", "app",
                                      "common", "types", "models", "conftest", "setup", "config", "", "test"}
        related = [] if generic or len(subject) < 5 else \
            [s for s in seeds if not s.is_test and PurePosixPath(s.path).stem.split(".")[0] == subject]
        if related:
            c.add("test_relevance", w.test_relevance * (1.5 if task_wants_tests else 1.0))
        elif not task_wants_tests:
            c.score *= 0.6  # de-emphasise unrelated tests
            for k in c.signals:
                c.signals[k] *= 0.6

    ranked = [c for c in cands.values() if c.score > 0]
    ranked.sort(key=lambda c: (-c.score, c.path))
    return ranked
