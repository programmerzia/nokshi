"""Repository scanner and incremental indexer (roadmap §4, §8).

- Discovery honours .gitignore (via `git ls-files`) when the repo is a git
  checkout, plus built-in and configured ignore lists.
- Indexing is incremental: files are hashed and only changed/new files are
  re-parsed; deleted files are removed. The dependency graph is rebuilt after
  each index pass because edges depend on the global symbol table.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from nokshi.core import tokens
from nokshi.core.config import Config
from nokshi.core.db import Database
from nokshi.core.parsers import STRUCTURAL_LANGUAGES, language_for, parser_for

TEST_PATTERNS = (
    r"(^|/)(tests?|__tests__|spec|specs)/", r"\.(test|spec)\.[jt]sx?$", r"(^|/)test_[^/]+\.py$",
    r"_test\.py$", r"Tests?\.php$", r"Tests?\.cs$", r"\.Tests?/", r"(^|/)e2e/", r"(^|/)cypress/",
)
CONFIG_PATTERNS = (
    r"(^|/)(config|configs|settings)/", r"(^|/)appsettings.*\.json$", r"(^|/)(\.env.*|composer\.json|package\.json)$",
    r"(^|/)(tsconfig|jsconfig|vite\.config|next\.config|tailwind\.config|webpack\.config|phpunit\.xml|pytest\.ini|pyproject\.toml)",
    r"\.csproj$", r"(^|/)docker-compose.*\.ya?ml$", r"(^|/)Dockerfile$",
)
_TEST_RX = [re.compile(p) for p in TEST_PATTERNS]
_CONFIG_RX = [re.compile(p) for p in CONFIG_PATTERNS]


@dataclass
class ScanStats:
    discovered: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    symbols: int = 0
    imports: int = 0
    deps: int = 0
    seconds: float = 0.0
    frameworks: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)


def is_test_path(path: str) -> bool:
    return any(rx.search(path) for rx in _TEST_RX)


def is_config_path(path: str) -> bool:
    return any(rx.search(path) for rx in _CONFIG_RX)


def module_of(path: str) -> str:
    """Coarse module label: first two path segments (e.g. app/Services, src/components)."""
    parts = PurePosixPath(path).parts
    if len(parts) <= 1:
        return "(root)"
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _git_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _ignored(rel: str, cfg: Config) -> bool:
    parts = PurePosixPath(rel).parts
    for i in range(len(parts) - 1):
        seg = parts[i]
        if seg in cfg.ignore_dirs or "/".join(parts[: i + 1]) in cfg.ignore_dirs:
            return True
        if any(fnmatch.fnmatch(seg, pat) for pat in cfg.ignore_dirs if "*" in pat):
            return True
    name = parts[-1]
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in cfg.ignore_files)


def discover(cfg: Config) -> list[str]:
    root = cfg.root
    candidates = _git_files(root)
    if candidates is None:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if d not in cfg.ignore_dirs and not d.startswith(".")
                           and f"{rel_dir}/{d}".lstrip("/") not in cfg.ignore_dirs]
            for fn in filenames:
                candidates.append(f"{rel_dir}/{fn}".lstrip("/"))
    result = []
    for rel in candidates:
        if _ignored(rel, cfg):
            continue
        if language_for(rel) is None:
            continue
        if not (root / rel).is_file():
            continue
        result.append(rel)
    result.sort()
    return result


# ---------------------------------------------------------------------------
# Framework detection (deterministic, roadmap §4)
# ---------------------------------------------------------------------------

def detect_frameworks(root: Path) -> list[str]:
    found: list[str] = []

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    composer = root / "composer.json"
    if composer.is_file():
        try:
            data = json.loads(composer.read_text(encoding="utf-8", errors="replace"))
            req = {**data.get("require", {}), **data.get("require-dev", {})}
            if "laravel/framework" in req:
                found.append("Laravel")
            if any(k.startswith("symfony/") for k in req) and "laravel/framework" not in req:
                found.append("Symfony")
            if "livewire/livewire" in req:
                found.append("Livewire")
            if "filament/filament" in req:
                found.append("Filament")
            if "phpunit/phpunit" in req:
                found.append("PHPUnit")
            if "pestphp/pest" in req:
                found.append("Pest")
        except json.JSONDecodeError:
            found.append("PHP/Composer")

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for key, label in [("next", "Next.js"), ("react", "React"), ("vue", "Vue"), ("nuxt", "Nuxt"),
                               ("@nestjs/core", "NestJS"), ("express", "Express"), ("fastify", "Fastify"),
                               ("@angular/core", "Angular"), ("svelte", "Svelte"), ("vite", "Vite"),
                               ("typescript", "TypeScript"), ("jest", "Jest"), ("vitest", "Vitest"),
                               ("@playwright/test", "Playwright"), ("cypress", "Cypress"),
                               ("tailwindcss", "Tailwind"), ("prisma", "Prisma"), ("@inertiajs/vue3", "Inertia")]:
                if key in deps and label not in found:
                    found.append(label)
        except json.JSONDecodeError:
            found.append("Node")

    csproj = list(root.glob("*.csproj")) + list(root.glob("*/*.csproj")) + list(root.glob("*/*/*.csproj"))
    if csproj:
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in csproj[:20])
        found.append("ASP.NET Core" if "Microsoft.AspNetCore" in text or "Microsoft.NET.Sdk.Web" in text else ".NET")
        if "EntityFrameworkCore" in text:
            found.append("EF Core")
        if "xunit" in text.lower():
            found.append("xUnit")
        if "Blazor" in text or list(root.rglob("*.razor")):
            found.append("Blazor")

    pyproj = root / "pyproject.toml"
    reqs = root / "requirements.txt"
    pytext = ""
    if pyproj.is_file():
        pytext += pyproj.read_text(encoding="utf-8", errors="replace").lower()
    if reqs.is_file():
        pytext += reqs.read_text(encoding="utf-8", errors="replace").lower()
    if pytext:
        for key, label in [("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"),
                           ("sqlalchemy", "SQLAlchemy"), ("pytest", "pytest"), ("celery", "Celery"),
                           ("langchain", "LangChain"), ("pydantic", "Pydantic")]:
            if key in pytext:
                found.append(label)
    if has("go.mod"):
        found.append("Go")
    if has("Cargo.toml"):
        found.append("Rust")
    if has("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
        found.append("Docker")
    return found


# ---------------------------------------------------------------------------
# Content terms (cheap lexical index, roadmap "basic semantic retrieval")
# ---------------------------------------------------------------------------

_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_TERM_STOP = {
    "the", "and", "for", "this", "that", "with", "from", "return", "function", "public", "private", "protected",
    "static", "class", "const", "let", "var", "new", "null", "true", "false", "void", "int", "string", "bool",
    "array", "self", "import", "export", "default", "async", "await", "def", "not", "none", "else", "elif",
    "use", "namespace", "using", "get", "set", "value", "type", "name", "data", "key", "item", "list", "dict",
    "object", "result", "response", "request", "error", "exception", "throw", "try", "catch", "finally",
    "foreach", "while", "break", "continue", "case", "switch", "extends", "implements", "interface", "abstract",
    "final", "override", "virtual", "readonly", "var", "param", "params", "args", "kwargs", "http", "https",
    "com", "org", "www", "html", "div", "span", "className", "props", "state", "this", "console", "log",
}
TERMS_PER_FILE = 400


def content_terms(text: str) -> dict[str, int]:
    from nokshi.core.parsers import split_identifier

    counts: dict[str, int] = {}
    for m in _IDENT_RX.finditer(text):
        word = m.group(0)
        for part in split_identifier(word):
            if len(part) < 3 or part in _TERM_STOP or part.isdigit():
                continue
            counts[part] = counts.get(part, 0) + 1
        if "_" in word or any(ch.isupper() for ch in word[1:]):
            low = word.lower()
            if low not in _TERM_STOP:
                counts[low] = counts.get(low, 0) + 1
    if len(counts) > TERMS_PER_FILE:
        counts = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:TERMS_PER_FILE])
    return counts


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index_repository(cfg: Config, db: Database, full: bool = False,
                     progress: Callable[[str], None] | None = None) -> ScanStats:
    from nokshi.core.graph import build_dependency_graph  # local import to avoid cycle

    t0 = time.time()
    stats = ScanStats()
    log = progress or (lambda _msg: None)

    files = discover(cfg)
    stats.discovered = len(files)
    known = {} if full else db.file_hashes()
    if full:
        with db.tx() as c:
            c.execute("DELETE FROM files")
    present = set(files)
    removed = [p for p in known if p not in present]
    if removed:
        db.delete_files(removed)
        stats.removed = len(removed)

    max_bytes = cfg.max_file_kb * 1024
    with db.tx() as c:
        for i, rel in enumerate(files, 1):
            abs_path = cfg.root / rel
            try:
                raw = abs_path.read_bytes()
            except OSError:
                continue
            digest = _sha256(raw)
            lang = language_for(rel) or "text"
            stats.languages[lang] = stats.languages.get(lang, 0) + 1
            if known.get(rel) == digest:
                stats.unchanged += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            loc = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            symbols, imports, namespace = [], [], None
            if lang in STRUCTURAL_LANGUAGES and len(raw) <= max_bytes:
                parser = parser_for(lang)
                if parser:
                    try:
                        res = parser.parse(text)
                        symbols, imports, namespace = res.symbols, res.imports, res.namespace
                    except Exception:  # parser bugs must never abort an index pass
                        pass
            file_id = db.upsert_file(
                c, path=rel, language=lang, module=module_of(rel), size=len(raw), loc=loc,
                tokens=tokens.count(text) if len(raw) <= max_bytes else len(raw) // 4,
                sha256=digest, mtime=abs_path.stat().st_mtime, is_test=int(is_test_path(rel)),
                is_config=int(is_config_path(rel)), namespace=namespace,
            )
            db.replace_symbols(c, file_id, symbols, imports)
            db.replace_terms(c, file_id, content_terms(text) if len(raw) <= max_bytes else {})
            stats.symbols += len(symbols)
            stats.imports += len(imports)
            if rel in known:
                stats.updated += 1
            else:
                stats.added += 1
            if i % 200 == 0:
                log(f"{i}/{len(files)} files")

    stats.frameworks = detect_frameworks(cfg.root)
    db.set_meta("frameworks", json.dumps(stats.frameworks))
    db.set_meta("root", str(cfg.root))
    db.set_meta("last_index", str(time.time()))
    db.set_meta("token_backend", tokens.backend())

    if stats.added or stats.updated or stats.removed or full:
        log("building dependency graph")
        stats.deps = build_dependency_graph(cfg, db)
    else:
        stats.deps = len(db.all_deps())
    stats.seconds = time.time() - t0
    return stats
